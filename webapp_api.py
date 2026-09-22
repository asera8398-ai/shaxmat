"""
webapp_api.py — Telegram Mini App (WebApp) uchun HTTP API.

MUHIM: mijoz (brauzer) tomonidagi hech qanday ma'lumotga ISHONILMAYDI.
Ball, ELO, mukofot, narxlar — hammasi SERVER tomonida hisoblanadi.
Foydalanuvchi kimligi Telegram `initData` imzosi (HMAC-SHA256) orqali
tekshiriladi — uni soxtalashtirib bo'lmaydi.

Marshrutlar:
    GET  /                 → Mini App (index.html)
    POST /api/init         → profil + sozlama + e'lon + turnir + reyting
    POST /api/profile      → taxta/dona/ism saqlash
    POST /api/game         → o'yin natijasi (mukofot serverda hisoblanadi)
    POST /api/help         → o'yin ichidagi yordam sotib olish
    POST /api/unlock       → taxta/dona to'plamini ochish
    POST /api/top          → reyting va turnir jadvali
"""
from __future__ import annotations

import os
import re
import json
import hmac
import time
import hashlib
import logging
from urllib.parse import parse_qsl
from datetime import datetime, timezone

from aiohttp import web

from matchmaking import MatchmakingManager

logger = logging.getLogger("webapp_api")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
INITDATA_TTL = 24 * 60 * 60          # initData amal qilish muddati (1 kun)
MIN_GAME_SECONDS = 15                # bundan tez "g'alaba" — shubhali
RATE_LIMIT = {}                      # user_id -> [timestamps]


# ────────────────────────── IMZO TEKSHIRUVI ──────────────────────────
def verify_init_data(init_data: str) -> dict | None:
    """Telegram initData imzosini tekshiradi. Muvaffaqiyatli bo'lsa
    `user` lug'atini qaytaradi, aks holda None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > INITDATA_TTL:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not user.get("id"):
            return None
        user["_start_param"] = pairs.get("start_param", "")
        return user
    except Exception as e:
        logger.warning("initData tekshirishda xato: %s", e)
        return None


def rate_ok(user_id: int, limit: int = 40, window: int = 60) -> bool:
    now = time.time()
    arr = [t for t in RATE_LIMIT.get(user_id, []) if now - t < window]
    arr.append(now)
    RATE_LIMIT[user_id] = arr
    return len(arr) <= limit


def full_name(u: dict) -> str:
    return (f"{u.get('first_name','')} {u.get('last_name','')}").strip() or "O'yinchi"


def setup_webapp_api(app: web.Application, db, bot=None, admin_id: int = 0, notify=None):
    """API marshrutlarini mavjud aiohttp ilovasiga ulaydi."""

    # ───────── yordamchilar ─────────
    async def S(key, default=0, cast=int):
        v = await db.get_setting(key)
        try:
            return cast(v) if v is not None else cast(default)
        except Exception:
            return cast(default)

    async def auth(request):
        """So'rovni tekshirib, (user_row, tg_user) qaytaradi."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        tg = verify_init_data(data.get("initData") or request.headers.get("X-Init-Data", ""))
        if not tg:
            raise web.HTTPUnauthorized(text=json.dumps({"ok": False, "error": "auth"}),
                                       content_type="application/json")
        uid = int(tg["id"])
        if not rate_ok(uid):
            raise web.HTTPTooManyRequests(text=json.dumps({"ok": False, "error": "rate"}),
                                          content_type="application/json")
        row = await db.get_user(uid)
        if not row:
            ref = None
            sp = tg.get("_start_param", "")
            if sp.isdigit() and int(sp) != uid:
                ref = int(sp)
            row = await db.add_user(uid, full_name(tg), tg.get("username", ""),
                                    tg.get("photo_url", ""), ref)
            if ref:
                bonus = await S("referral_points", 100)
                await db.add_points(ref, bonus)
                await db.log_transaction(ref, "referral", bonus, f"ref:{uid}", "ball")
                if notify:
                    await notify(ref, f"🎁 Sizning havolangiz orqali yangi o'yinchi qo'shildi! +{bonus} ball")
        if row["is_blocked"]:
            raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": "blocked"}),
                                    content_type="application/json")
        await db.touch(uid)
        return row, tg, data

    async def public_config() -> dict:
        s = await db.all_settings()
        g = lambda k, d: int(s.get(k, d) or d)
        return {
            "prices": {"undo": g("price_undo", 40), "king": g("price_king", 400),
                       "extra": g("price_extra", 120), "hint": g("price_hint", 25)},
            "rewards": [g("win_reward_easy", 5), g("win_reward_mid", 15), g("win_reward_hard", 40)],
            "themePrice": g("price_theme", 300), "skinPrice": g("price_skin", 250),
            "eloWin": g("elo_win", 12), "eloLose": g("elo_lose", 10),
            "minTopup": g("min_topup", 5000),
            "maintenance": g("maintenance", 0),
            "botUser": s.get("bot_username", ""),
        }

    def user_json(u, rank=0):
        try:
            unl = json.loads(u["unlocked"])
        except Exception:
            unl = {"t": [0, 1, 2], "s": [0, 1]}
        return {"id": u["user_id"], "name": u["fullname"], "username": u["username"],
                "photo": u["photo_url"], "points": u["points"], "balance": u["balance"],
                "elo": u["elo"], "w": u["wins"], "l": u["losses"], "d": u["draws"],
                "theme": u["theme"], "skin": u["skin"], "unl": unl, "rank": rank}

    async def tournament_json(uid):
        t = await db.active_tournament()
        if not t:
            return None
        top = await db.tournament_top(t["id"], 10)
        return {
            "id": t["id"], "title": t["title"], "descr": t["descr"],
            "prize": t["prize_text"], "ends": t["ends_at"].isoformat(),
            "endsTs": int(t["ends_at"].timestamp()),
            "place": await db.tournament_place(t["id"], uid),
            "top": [{"name": r["fullname"], "photo": r["photo_url"],
                     "score": r["score"], "wins": r["wins"], "me": r["user_id"] == uid} for r in top],
        }

    async def announces_json():
        rows = await db.announcements()
        return [{"id": r["id"], "title": r["title"], "text": r["text"],
                 "kind": r["kind"], "emoji": r["emoji"], "pinned": r["pinned"]} for r in rows]

    async def packages_json():
        rows = await db.packages()
        return [{"id": r["id"], "title": r["title"], "points": r["points"],
                 "price": r["price"], "badge": r["badge"]} for r in rows]

    # ───────── marshrutlar ─────────
    async def api_init(request):
        u, tg, _ = await auth(request)
        # Telegram profilidagi yangilangan ism/rasmni sinxronlaymiz
        if tg.get("photo_url") or full_name(tg) != u["fullname"]:
            await db.add_user(u["user_id"], full_name(tg), tg.get("username", ""), tg.get("photo_url", ""))
            u = await db.get_user(u["user_id"])
        rank = await db.my_rank(u["user_id"])
        return web.json_response({
            "ok": True,
            "user": user_json(u, rank),
            "cfg": await public_config(),
            "ann": await announces_json(),
            "tour": await tournament_json(u["user_id"]),
            "packs": await packages_json(),
            "top": await top_rows("all", u["user_id"]),
        })

    async def top_rows(period, uid):
        rows = await db.leaderboard(period, 20)
        return [{"name": r["fullname"] or "O'yinchi", "photo": r["photo_url"], "elo": r["elo"],
                 "wins": r["wins"], "score": r["score"], "me": r["user_id"] == uid} for r in rows]

    async def api_top(request):
        u, _, data = await auth(request)
        period = data.get("period", "all")
        return web.json_response({"ok": True, "top": await top_rows(period, u["user_id"]),
                                  "tour": await tournament_json(u["user_id"])})

    async def api_profile(request):
        u, _, data = await auth(request)
        kw = {}
        if isinstance(data.get("theme"), int):
            kw["theme"] = max(0, min(99, data["theme"]))
        if isinstance(data.get("skin"), int):
            kw["skin"] = max(0, min(99, data["skin"]))
        if kw:
            await db.set_profile(u["user_id"], **kw)
        return web.json_response({"ok": True})

    async def api_game(request):
        """O'yin natijasi. Mukofot FAQAT shu yerda hisoblanadi."""
        u, _, data = await auth(request)
        uid = u["user_id"]
        result = data.get("result")
        if result not in ("win", "lose", "draw"):
            return web.json_response({"ok": False, "error": "result"}, status=400)
        level = int(data.get("level", 1)) if str(data.get("level", 1)).isdigit() else 1
        level = max(0, min(2, level))
        mode = "bot" if data.get("mode") != "human" else "human"
        duration = int(data.get("duration", 0) or 0)
        moves = int(data.get("moves", 0) or 0)

        # ── Anti-chit: juda tez "g'alaba" va kunlik limit
        cheat = result == "win" and (duration < MIN_GAME_SECONDS or moves < 6)
        limit = await S("daily_limit_bot", 60)
        over_limit = await db.games_today(uid) >= limit

        base = [await S("win_reward_easy", 5), await S("win_reward_mid", 15),
                await S("win_reward_hard", 40)][level]
        if result == "win":
            pts = 0 if (cheat or over_limit) else base
            elo = await S("elo_win", 12)
        elif result == "draw":
            div = max(1, await S("draw_divider", 3))
            pts = 0 if over_limit else base // div
            elo = 0
        else:
            pts, elo = 0, -(await S("elo_lose", 10))

        row = await db.apply_result(uid, result, pts, elo)
        await db.add_game(uid, mode, level, result, pts, elo, moves, duration)
        if pts:
            await db.log_transaction(uid, "win", pts, f"{mode}:{level}", "ball")

        # ── Turnir ochkolari
        tour_msg = None
        t = await db.active_tournament()
        if t and result in ("win", "draw") and not cheat and not over_limit:
            sc = await S("tournament_win_score", 10) if result == "win" else await S("tournament_draw_score", 3)
            await db.add_tournament_score(t["id"], uid, sc, result == "win")
            tour_msg = {"score": sc, "place": await db.tournament_place(t["id"], uid)}

        return web.json_response({
            "ok": True, "points": row["points"], "elo": row["elo"],
            "reward": pts, "eloDelta": elo, "tour": tour_msg,
            "limited": over_limit, "flagged": cheat,
        })

    async def api_help(request):
        """O'yin ichidagi yordam (undo/king/extra/hint) sotib olish."""
        u, _, data = await auth(request)
        kind = data.get("kind")
        key = {"undo": "price_undo", "king": "price_king", "extra": "price_extra", "hint": "price_hint"}.get(kind)
        if not key:
            return web.json_response({"ok": False, "error": "kind"}, status=400)
        cost = await S(key, 0)
        if u["points"] < cost:
            return web.json_response({"ok": False, "error": "points", "need": cost})
        pts = await db.add_points(u["user_id"], -cost)
        await db.log_transaction(u["user_id"], "help", -cost, kind, "ball")
        return web.json_response({"ok": True, "points": pts, "cost": cost})

    async def api_unlock(request):
        """Taxta yoki dona to'plamini ball evaziga ochish."""
        u, _, data = await auth(request)
        kind, idx = data.get("kind"), data.get("index")
        if kind not in ("theme", "skin") or not isinstance(idx, int) or not (0 <= idx < 40):
            return web.json_response({"ok": False, "error": "arg"}, status=400)
        cost = await S("price_theme" if kind == "theme" else "price_skin", 300)
        try:
            unl = json.loads(u["unlocked"])
        except Exception:
            unl = {"t": [0, 1, 2], "s": [0, 1]}
        key = "t" if kind == "theme" else "s"
        if idx in unl.get(key, []):
            return web.json_response({"ok": True, "points": u["points"], "unl": unl})
        if u["points"] < cost:
            return web.json_response({"ok": False, "error": "points", "need": cost})
        unl.setdefault(key, []).append(idx)
        pts = await db.add_points(u["user_id"], -cost)
        await db.set_profile(u["user_id"], unlocked=json.dumps(unl),
                             **({"theme": idx} if kind == "theme" else {"skin": idx}))
        await db.log_transaction(u["user_id"], "unlock", -cost, f"{kind}:{idx}", "ball")
        return web.json_response({"ok": True, "points": pts, "unl": unl})

    # ───────── statik fayllar ─────────
    async def index(request):
        return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))

    # ───────── real-vaqtli PvP (WebSocket) ─────────
    mm = MatchmakingManager(db, verify_init_data, S)
    app["matchmaking"] = mm
    app.router.add_get("/ws", mm.handle_ws)

    app.router.add_post("/api/init", api_init)
    app.router.add_post("/api/top", api_top)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/game", api_game)
    app.router.add_post("/api/help", api_help)
    app.router.add_post("/api/unlock", api_unlock)
    app.router.add_get("/", index)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    if os.path.isdir(WEBAPP_DIR):
        app.router.add_static("/static/", WEBAPP_DIR)
    return app
