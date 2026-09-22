"""
game_engine.py — Rus shashkasi qoidalarining server tomonidagi (Python)
nusxasi. webapp/index.html dagi JS dvigateli bilan bir xil mantiq —
shu tufayli ikki real o'yinchi o'rtasidagi (PvP) har bir xod SERVERDA
tekshiriladi: mijoz "g'alaba qozondim" deb yolg'on aytolmaydi.

Qoidalar (standart, o'zgarmas): urish majburiy, orqaga urish bor,
damka uzoq masofaga uchib yuradi va uzoqdan uradi, ketma-ket urish
oxirigacha davom etadi.
"""
from __future__ import annotations

DIRS = [(-1, -1), (-1, 1), (1, -1), (1, 1)]


def init_board():
    b = [[None] * 8 for _ in range(8)]
    for r in range(8):
        for c in range(8):
            if (r + c) % 2:
                if r < 3:
                    b[r][c] = {"c": "b", "k": False}
                elif r > 4:
                    b[r][c] = {"c": "w", "k": False}
    return b


def in_b(r, c):
    return 0 <= r < 8 and 0 <= c < 8


def fwd(col):
    return -1 if col == "w" else 1


def _has_j(b, r, c, king, col, done):
    for dr, dc in DIRS:
        a, e = r + dr, c + dc
        if king:
            while in_b(a, e) and b[a][e] is None:
                a += dr
                e += dc
        if not in_b(a, e):
            continue
        t = b[a][e]
        if not t or t["c"] == col or (a, e) in done:
            continue
        x, y = a + dr, e + dc
        if in_b(x, y) and b[x][y] is None:
            return True
    return False


def _seqs(b, r, c, king, col, done, path, out, o):
    any_ = False
    for dr, dc in DIRS:
        a, e = r + dr, c + dc
        if king:
            while in_b(a, e) and b[a][e] is None:
                a += dr
                e += dc
        if not in_b(a, e):
            continue
        t = b[a][e]
        if not t or t["c"] == col or (a, e) in done:
            continue
        L = []
        x, y = a + dr, e + dc
        while in_b(x, y) and b[x][y] is None:
            L.append((x, y))
            if not king:
                break
            x += dr
            y += dc
        if not L:
            continue
        any_ = True
        nd = done + [(a, e)]

        def pk(u):
            return king or (u == 0 if col == "w" else u == 7)

        cont = [p for p in L if _has_j(b, p[0], p[1], pk(p[0]), col, nd)]
        targets = cont if cont else L
        for (u, v) in targets:
            _seqs(b, u, v, pk(u), col, nd, path + [(u, v)], out, o)
    if not any_ and done:
        out.append({"fr": o[0], "fc": o[1], "tr": r, "tc": c, "caps": done, "path": path, "pk": king})


def moves(b, col):
    """Berilgan rang uchun barcha qonuniy xodlar (urish majburiy bo'lsa,
    faqat urish xodlari qaytadi)."""
    cp = []
    pieces = [(r, c, b[r][c]) for r in range(8) for c in range(8) if b[r][c] and b[r][c]["c"] == col]
    for r, c, p in pieces:
        b[r][c] = None
        _seqs(b, r, c, p["k"], col, [], [], cp, (r, c))
        b[r][c] = p
    if cp:
        return cp
    mv = []
    for r, c, p in pieces:
        for dr, dc in DIRS:
            if not p["k"] and dr != fwd(col):
                continue
            a, e = r + dr, c + dc
            while in_b(a, e) and b[a][e] is None:
                mv.append({"fr": r, "fc": c, "tr": a, "tc": e, "caps": [], "path": [(a, e)]})
                if not p["k"]:
                    break
                a += dr
                e += dc
    return mv


def apply_move(b, m):
    n = [[dict(cell) if cell else None for cell in row] for row in b]
    p = n[m["fr"]][m["fc"]]
    n[m["fr"]][m["fc"]] = None
    for (r, c) in m["caps"]:
        n[r][c] = None
    n[m["tr"]][m["tc"]] = p
    if m.get("pk") or (p["c"] == "w" and m["tr"] == 0) or (p["c"] == "b" and m["tr"] == 7):
        p["k"] = True
    return n


def find_move(legal, fr, fc, tr, tc, caps):
    """Mijoz yuborgan xod (fr,fc,tr,tc,caps) qonuniy xodlar ro'yxatida
    bormi — bo'lsa o'sha to'liq move obyektini qaytaradi (aks holda None).
    caps — [[r,c], ...] ro'yxati (JSON dan kelgan)."""
    try:
        caps_t = tuple(sorted(tuple(x) for x in (caps or [])))
    except Exception:
        return None
    for m in legal:
        if m["fr"] == fr and m["fc"] == fc and m["tr"] == tr and m["tc"] == tc and tuple(sorted(m["caps"])) == caps_t:
            return m
    return None


def piece_count(b, col):
    return sum(1 for row in b for p in row if p and p["c"] == col)


def winner_after(b, turn):
    """Xoddan keyingi holatni tekshiradi: 'w'|'b' — kim yutdi, None — davom etadi."""
    if piece_count(b, "b") == 0:
        return "w"
    if piece_count(b, "w") == 0:
        return "b"
    if not moves(b, turn):
        return "b" if turn == "w" else "w"
    return None
