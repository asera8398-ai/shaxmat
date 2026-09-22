"""
matchmaking.py — "Odam bilan" rejimi uchun HAQIQIY real-vaqtli PvP.

Avvalgi versiyada "Tasodifiy raqib" rejimi faqat bot bilan o'ynatib,
natijani "human" deb belgilab qo'yardi — bu chalg'ituvchi edi. Endi:

  1. Ikki real foydalanuvchi WebSocket orqali navbatga turadi;
  2. Topilishi bilan bittasi oq, bittasi qora bo'lib bog'lanadi;
  3. Har bir xod SERVERDA (game_engine.py) tekshiriladi — mijoz
     signalini qalbakilashtirib bo'lmaydi;
  4. Vaqt serverda hisoblanadi (soniyada bir marta ikkala tomonga
     yuboriladi);
  5. G'alaba/mag'lubiyat/durang uchun ball va ELO faqat shu yerda,
     serverda beriladi.

Cheklovlar (MVP, keyingi versiyalarda kengaytiriladi):
  - Moslik xotirada (RAM) saqlanadi — server qayta ishga tushsa,
    davom etayotgan o'yinlar yo'qoladi (tugallangan natijalar bazada
    saqlangani uchun ball/ELO yo'qolmaydi, faqat o'sha bitta partiya).
  - Uzilib qolgan o'yinchiga 15 soniya kutish beriladi, qaytmasa
    raqibiga g'alaba beriladi.
"""
from __future__ import annotations

import json
import time
import random
import asyncio
import secrets
import logging

from aiohttp import web

import game_engine as ge

logger = logging.getLogger("matchmaking")

DISCONNECT_GRACE = 15  # soniya


def _full_name(u: dict) -> str:
    return (f"{u.get('first_name', '')} {u.get('last_name', '')}").strip() or "O'yinchi"


class MatchmakingManager:
    def __init__(self, db, verify_init_data, get_setting_int):
        self.db = db
        self.verify = verify_init_data
        self.S = get_setting_int          # async fn(key, default) -> int
        self.queue: list[dict] = []
        self.conns: dict[int, web.WebSocketResponse] = {}
        self.matches: dict[str, dict] = {}
        self.player_match: dict[int, str] = {}
        self.lock = asyncio.Lock()

    # ─────────────────────────── WS KIRISH NUQTASI ───────────────────────────
    async def handle_ws(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        uid = None
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")

                if t == "auth":
                    u = self.verify(data.get("initData", ""))
                    if not u:
                        await ws.send_json({"type": "error", "error": "auth"})
                        continue
                    uid = int(u["id"])
                    row = await self.db.get_user(uid)
                    if not row:
                        row = await self.db.add_user(uid, _full_name(u), u.get("username", ""), u.get("photo_url", ""))
                    self.conns[uid] = ws
                    await ws.send_json({"type": "ready", "uid": uid})
                    # Uzilgandan keyin 15 soniya ichida qaytgan bo'lsa — o'yin davom etadi
                    mid = self.player_match.get(uid)
                    if mid and mid in self.matches:
                        await self._send_state(mid, uid)

                elif t == "queue" and uid:
                    await self.enqueue(uid, ws)

                elif t == "cancel" and uid:
                    await self.dequeue(uid)

                elif t == "move" and uid:
                    await self.handle_move(uid, data)

                elif t == "resign" and uid:
                    await self.handle_resign(uid)

                elif t == "ping":
                    await ws.send_json({"type": "pong"})
        finally:
            if uid is not None and self.conns.get(uid) is ws:
                self.conns.pop(uid, None)
                await self.dequeue(uid)
                asyncio.create_task(self._grace_then_forfeit(uid))
        return ws

    # ─────────────────────────── NAVBAT ───────────────────────────
    async def enqueue(self, uid: int, ws: web.WebSocketResponse):
        if uid in self.player_match:
            return
        async with self.lock:
            self.queue = [q for q in self.queue if q["uid"] != uid]
            row = await self.db.get_user(uid)
            entry = {"uid": uid, "ws": ws, "name": row["fullname"] or "O'yinchi",
                     "photo": row["photo_url"] or "", "elo": row["elo"]}
            opponent = None
            for i, q in enumerate(self.queue):
                if q["uid"] != uid:
                    opponent = self.queue.pop(i)
                    break
            if opponent:
                await self._create_match(entry, opponent)
                return
            self.queue.append(entry)
        try:
            await ws.send_json({"type": "queued"})
        except Exception:
            pass

    async def dequeue(self, uid: int):
        async with self.lock:
            self.queue = [q for q in self.queue if q["uid"] != uid]

    # ─────────────────────────── MOSLIK ───────────────────────────
    async def _create_match(self, a: dict, b: dict):
        mid = secrets.token_hex(8)
        white, black = (a, b) if random.random() < 0.5 else (b, a)
        minutes = await self.S("minutes_pvp", 5)
        m = {
            "id": mid, "white": white["uid"], "black": black["uid"],
            "ws": {white["uid"]: white["ws"], black["uid"]: black["ws"]},
            "info": {white["uid"]: white, black["uid"]: black},
            "board": ge.init_board(), "turn": "w",
            "t": {white["uid"]: minutes * 60, black["uid"]: minutes * 60},
            "ended": False, "moves": 0, "created": time.time(),
        }
        self.matches[mid] = m
        self.player_match[white["uid"]] = mid
        self.player_match[black["uid"]] = mid
        for me, opp, color in ((white, black, "w"), (black, white, "b")):
            try:
                await me["ws"].send_json({
                    "type": "matched", "matchId": mid, "color": color, "minutes": minutes,
                    "opponent": {"id": opp["uid"], "name": opp["name"], "photo": opp["photo"], "elo": opp["elo"]},
                })
            except Exception:
                pass
        asyncio.create_task(self._clock_loop(mid))

    async def _clock_loop(self, mid: str):
        while True:
            await asyncio.sleep(1)
            m = self.matches.get(mid)
            if not m or m["ended"]:
                return
            uid = m["white"] if m["turn"] == "w" else m["black"]
            m["t"][uid] -= 1
            payload = {"type": "time", "t": {str(k): v for k, v in m["t"].items()}}
            for ws in list(m["ws"].values()):
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass
            if m["t"][uid] <= 0:
                winner = m["black"] if uid == m["white"] else m["white"]
                await self._finish(mid, winner, "timeout")
                return

    async def _send_state(self, mid: str, uid: int):
        m = self.matches.get(mid)
        if not m:
            return
        color = "w" if uid == m["white"] else "b"
        opp_uid = m["black"] if uid == m["white"] else m["white"]
        opp = m["info"][opp_uid]
        board_flat = [[cell for cell in row] for row in m["board"]]
        try:
            await m["ws"][uid].send_json({
                "type": "resync", "matchId": mid, "color": color, "turn": m["turn"],
                "board": board_flat, "t": {str(k): v for k, v in m["t"].items()},
                "opponent": {"name": opp["name"], "photo": opp["photo"], "elo": opp["elo"]},
            })
        except Exception:
            pass

    # ─────────────────────────── XODLAR ───────────────────────────
    async def handle_move(self, uid: int, data: dict):
        mid = self.player_match.get(uid)
        if not mid:
            return
        m = self.matches.get(mid)
        if not m or m["ended"]:
            return
        color = "w" if uid == m["white"] else "b"
        if m["turn"] != color:
            return
        legal = ge.moves(m["board"], color)
        mv = ge.find_move(legal, data.get("fr"), data.get("fc"), data.get("tr"), data.get("tc"), data.get("caps", []))
        if not mv:
            try:
                await m["ws"][uid].send_json({"type": "invalid"})
            except Exception:
                pass
            return
        m["board"] = ge.apply_move(m["board"], mv)
        m["turn"] = "b" if color == "w" else "w"
        m["moves"] += 1
        opp_uid = m["black"] if uid == m["white"] else m["white"]
        payload = {"type": "opponent_move",
                   "move": {"fr": mv["fr"], "fc": mv["fc"], "tr": mv["tr"], "tc": mv["tc"],
                            "caps": [list(x) for x in mv["caps"]], "path": [list(x) for x in mv["path"]],
                            "pk": bool(mv.get("pk"))}}
        opp_ws = m["ws"].get(opp_uid)
        if opp_ws:
            try:
                await opp_ws.send_json(payload)
            except Exception:
                pass
        win = ge.winner_after(m["board"], m["turn"])
        if win:
            winner_uid = m["white"] if win == "w" else m["black"]
            await self._finish(mid, winner_uid, "normal")

    async def handle_resign(self, uid: int):
        mid = self.player_match.get(uid)
        if not mid:
            return
        m = self.matches.get(mid)
        if not m or m["ended"]:
            return
        winner = m["black"] if uid == m["white"] else m["white"]
        await self._finish(mid, winner, "resign")

    async def _grace_then_forfeit(self, uid: int):
        mid = self.player_match.get(uid)
        if not mid:
            return
        await asyncio.sleep(DISCONNECT_GRACE)
        m = self.matches.get(mid)
        if not m or m["ended"]:
            return
        if uid in self.conns:  # qaytib ulandi
            return
        winner = m["black"] if uid == m["white"] else m["white"]
        await self._finish(mid, winner, "disconnect")

    # ─────────────────────────── YAKUN ───────────────────────────
    async def _finish(self, mid: str, winner_uid: int, reason: str):
        m = self.matches.get(mid)
        if not m or m["ended"]:
            return
        m["ended"] = True
        loser_uid = m["black"] if winner_uid == m["white"] else m["white"]
        self.player_match.pop(winner_uid, None)
        self.player_match.pop(loser_uid, None)

        elo_win = await self.S("elo_win", 12)
        elo_lose = await self.S("elo_lose", 10)
        reward = await self.S("win_reward_human", 25)

        await self.db.apply_result(winner_uid, "win", reward, elo_win)
        await self.db.log_transaction(winner_uid, "win", reward, "human", "ball")
        await self.db.add_game(winner_uid, "human", 0, "win", reward, elo_win, m["moves"], int(time.time() - m["created"]))

        await self.db.apply_result(loser_uid, "lose", 0, -elo_lose)
        await self.db.add_game(loser_uid, "human", 0, "lose", 0, -elo_lose, m["moves"], int(time.time() - m["created"]))

        for uid in (winner_uid, loser_uid):
            ws = self.conns.get(uid)
            if not ws:
                continue
            try:
                u = await self.db.get_user(uid)
                await ws.send_json({
                    "type": "ended", "winner": uid == winner_uid, "reason": reason,
                    "points": u["points"], "elo": u["elo"],
                    "reward": reward if uid == winner_uid else 0,
                    "eloDelta": elo_win if uid == winner_uid else -elo_lose,
                })
            except Exception:
                pass
        self.matches.pop(mid, None)

    def stats(self) -> dict:
        return {"queue": len(self.queue), "matches": len(self.matches)}
