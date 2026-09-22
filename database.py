"""
database.py — Shashka Arena (Telegram Mini App) uchun PostgreSQL qatlami.

Deploy qayta qilinganda ma'lumotlar O'CHMAYDI.

.env:
    DATABASE_URL=postgresql://user:password@host:5432/dbname

Eslatma: foydalanuvchida IKKI xil "pul" bor:
    balance — SO'M (karta orqali to'ldiriladi)
    points  — BALL (o'yin ichidagi valyuta, so'mga sotib olinadi)
"""
from __future__ import annotations

import os
import json
import asyncpg
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/shashka").strip()


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def init(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        await self._create_tables()
        await self._seed()

    # ─────────────────────────── JADVALLAR ───────────────────────────
    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    tartib_id       SERIAL PRIMARY KEY,
                    user_id         BIGINT UNIQUE NOT NULL,
                    fullname        TEXT DEFAULT '',
                    username        TEXT DEFAULT '',
                    photo_url       TEXT DEFAULT '',
                    phone           TEXT DEFAULT '',
                    balance         BIGINT DEFAULT 0,      -- so'm
                    points          BIGINT DEFAULT 0,      -- ball
                    total_deposited BIGINT DEFAULT 0,
                    elo             INT DEFAULT 1200,
                    wins            INT DEFAULT 0,
                    losses          INT DEFAULT 0,
                    draws           INT DEFAULT 0,
                    theme           INT DEFAULT 0,
                    skin            INT DEFAULT 0,
                    unlocked        TEXT DEFAULT '{"t":[0,1,2],"s":[0,1]}',
                    referrer_id     BIGINT,
                    is_blocked      BOOLEAN DEFAULT FALSE,
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    last_active     TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    type       TEXT NOT NULL,      -- topup|package|admin|win|help|unlock|prize|referral
                    currency   TEXT DEFAULT 'sum', -- sum | ball
                    amount     BIGINT NOT NULL,
                    note       TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions (created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions (user_id)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)
            # Avtomatik karta to'lovlari (humo_listener / tolov_api bilan bir xil sxema)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS auto_payments (
                    id           SERIAL PRIMARY KEY,
                    user_id      BIGINT NOT NULL,
                    base_amount  BIGINT NOT NULL,
                    final_amount BIGINT NOT NULL,
                    target_card  TEXT DEFAULT '',
                    status       TEXT DEFAULT 'pending',
                    card_used    TEXT DEFAULT '',
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    expires_at   TIMESTAMPTZ NOT NULL,
                    completed_at TIMESTAMPTZ
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ap_status ON auto_payments (status, final_amount)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_payments (
                    pay_id     TEXT PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    amount     BIGINT NOT NULL,
                    fullname   TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Ball paketlari — admin panelda to'liq boshqariladi
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS packages (
                    id       SERIAL PRIMARY KEY,
                    title    TEXT NOT NULL,
                    points   BIGINT NOT NULL,
                    price    BIGINT NOT NULL,
                    bonus    INT DEFAULT 0,
                    badge    TEXT DEFAULT '',
                    active   BOOLEAN DEFAULT TRUE,
                    sort     INT DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    mode       TEXT DEFAULT 'bot',
                    level      INT DEFAULT 1,
                    result     TEXT NOT NULL,   -- win|lose|draw
                    points     BIGINT DEFAULT 0,
                    elo_delta  INT DEFAULT 0,
                    moves      INT DEFAULT 0,
                    duration   INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_games_user ON games (user_id, created_at)")

            # Chempionat / turnirlar
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tournaments (
                    id          SERIAL PRIMARY KEY,
                    title       TEXT NOT NULL,
                    descr       TEXT DEFAULT '',
                    prize_text  TEXT DEFAULT '',
                    prizes      TEXT DEFAULT '[]',   -- [{place, amount}]
                    starts_at   TIMESTAMPTZ DEFAULT NOW(),
                    ends_at     TIMESTAMPTZ NOT NULL,
                    status      TEXT DEFAULT 'active', -- active|finished
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tournament_scores (
                    tournament_id INT NOT NULL,
                    user_id       BIGINT NOT NULL,
                    score         BIGINT DEFAULT 0,
                    wins          INT DEFAULT 0,
                    updated_at    TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (tournament_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payouts (
                    id            SERIAL PRIMARY KEY,
                    tournament_id INT,
                    user_id       BIGINT NOT NULL,
                    place         INT DEFAULT 0,
                    amount        BIGINT DEFAULT 0,
                    status        TEXT DEFAULT 'pending', -- pending|paid
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # E'lonlar — Mini App ichida banner/karta ko'rinishida chiqadi
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id         SERIAL PRIMARY KEY,
                    title      TEXT DEFAULT '',
                    text       TEXT NOT NULL,
                    kind       TEXT DEFAULT 'info',  -- info|prize|warning
                    emoji      TEXT DEFAULT '📣',
                    active     BOOLEAN DEFAULT TRUE,
                    pinned     BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Majburiy obuna kanallari
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id           SERIAL PRIMARY KEY,
                    chat_id      BIGINT,
                    username     TEXT DEFAULT '',
                    title        TEXT DEFAULT '',
                    invite_link  TEXT DEFAULT '',
                    active       BOOLEAN DEFAULT TRUE,
                    sort         INT DEFAULT 0,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Qo'shimcha adminlar (asosiy ADMIN_ID .env'da, bular botning o'zidan qo'shiladi)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id    BIGINT PRIMARY KEY,
                    fullname   TEXT DEFAULT '',
                    added_by   BIGINT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Pulni kartaga yechish so'rovlari
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id           SERIAL PRIMARY KEY,
                    user_id      BIGINT NOT NULL,
                    amount       BIGINT NOT NULL,
                    card         TEXT NOT NULL,
                    status       TEXT DEFAULT 'pending',   -- pending|paid|rejected
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    resolved_at  TIMESTAMPTZ
                )
            """)

    async def _seed(self):
        """Birinchi ishga tushirishda standart paketlar va sozlamalar."""
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM packages")
            if not n:
                await conn.executemany(
                    "INSERT INTO packages (title, points, price, bonus, badge, sort) VALUES ($1,$2,$3,$4,$5,$6)",
                    [
                        ("Boshlang'ich", 200, 10000, 0, "", 1),
                        ("Mashhur", 550, 22000, 10, "+10%", 2),
                        ("Eng foydali", 1875, 55000, 25, "+25%", 3),
                        ("Chempion", 5000, 120000, 40, "TOP", 4),
                    ],
                )
        defaults = {
            "win_reward_easy": "5", "win_reward_mid": "15", "win_reward_hard": "40", "win_reward_human": "25",
            "minutes_pvp": "5",
            "draw_divider": "3", "elo_win": "12", "elo_lose": "10",
            "price_undo": "40", "price_king": "400", "price_extra": "120", "price_hint": "25",
            "price_theme": "300", "price_skin": "250",
            "start_points": "100", "referral_points": "100",
            "auto_pay_enabled": "1", "auto_pay_offset": "50", "auto_pay_expiry": "30",
            "min_topup": "5000", "min_withdraw": "20000", "maintenance": "0",
            "tournament_win_score": "10", "tournament_draw_score": "3",
            "daily_limit_bot": "60",
        }
        for k, v in defaults.items():
            if await self.get_setting(k) is None:
                await self.set_setting(k, v)

    # ─────────────────────────── SOZLAMALAR ───────────────────────────
    async def get_setting(self, key: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)

    async def set_setting(self, key: str, value):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1,$2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, str(value))

    async def all_settings(self) -> dict:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    # ─────────────────────────── FOYDALANUVCHI ───────────────────────────
    async def add_user(self, user_id: int, fullname="", username="", photo_url="", referrer_id=None):
        start_points = int(await self.get_setting("start_points") or 0)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO users (user_id, fullname, username, photo_url, points, referrer_id)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (user_id) DO UPDATE
                     SET fullname = COALESCE(NULLIF(EXCLUDED.fullname,''), users.fullname),
                         username = COALESCE(NULLIF(EXCLUDED.username,''), users.username),
                         photo_url = COALESCE(NULLIF(EXCLUDED.photo_url,''), users.photo_url),
                         last_active = NOW()
                   RETURNING *, (xmax = 0) AS created""",
                user_id, fullname, username, photo_url, start_points, referrer_id)
        return row

    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

    async def touch(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_active = NOW() WHERE user_id = $1", user_id)

    async def update_balance(self, user_id: int, delta: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET balance = balance + $2, "
                "total_deposited = total_deposited + GREATEST($2, 0) WHERE user_id = $1", user_id, delta)

    async def set_balance(self, user_id: int, value: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = $2 WHERE user_id = $1", user_id, max(0, value))

    async def add_points(self, user_id: int, delta: int) -> int:
        """Ball qo'shadi/ayiradi va yangi qiymatni qaytaradi (0 dan pastga tushmaydi)."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "UPDATE users SET points = GREATEST(points + $2, 0) WHERE user_id = $1 RETURNING points",
                user_id, delta)

    async def set_points(self, user_id: int, value: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET points = $2 WHERE user_id = $1", user_id, max(0, value))

    async def set_profile(self, user_id: int, **kw):
        allowed = {"theme", "skin", "unlocked", "fullname", "phone", "elo"}
        sets, vals = [], []
        for i, (k, v) in enumerate((k, v) for k, v in kw.items() if k in allowed):
            sets.append(f"{k} = ${i + 2}")
            vals.append(v)
        if not sets:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = $1", user_id, *vals)

    async def set_blocked(self, user_id: int, flag: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blocked = $2 WHERE user_id = $1", user_id, flag)

    async def apply_result(self, user_id: int, result: str, points: int, elo_delta: int):
        col = {"win": "wins", "lose": "losses", "draw": "draws"}[result]
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                f"UPDATE users SET {col} = {col} + 1, points = GREATEST(points + $2, 0), "
                f"elo = GREATEST(elo + $3, 100) WHERE user_id = $1 RETURNING points, elo", 
                user_id, points, elo_delta)

    async def find_users(self, q: str, limit: int = 15):
        async with self.pool.acquire() as conn:
            if q.isdigit():
                return await conn.fetch("SELECT * FROM users WHERE user_id = $1", int(q))
            return await conn.fetch(
                "SELECT * FROM users WHERE fullname ILIKE $1 OR username ILIKE $1 "
                "ORDER BY last_active DESC LIMIT $2", f"%{q}%", limit)

    async def all_user_ids(self, only_active=True):
        async with self.pool.acquire() as conn:
            sql = "SELECT user_id FROM users"
            if only_active:
                sql += " WHERE is_blocked = FALSE"
            return [r["user_id"] for r in await conn.fetch(sql)]

    async def stats(self) -> dict:
        async with self.pool.acquire() as conn:
            return dict(await conn.fetchrow("""
                SELECT
                  (SELECT COUNT(*) FROM users) AS users,
                  (SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '1 day') AS new_today,
                  (SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '1 day') AS active_today,
                  (SELECT COALESCE(SUM(balance),0) FROM users) AS balance_sum,
                  (SELECT COALESCE(SUM(points),0) FROM users) AS points_sum,
                  (SELECT COUNT(*) FROM games) AS games,
                  (SELECT COUNT(*) FROM games WHERE created_at > NOW() - INTERVAL '1 day') AS games_today,
                  (SELECT COALESCE(SUM(amount),0) FROM transactions
                     WHERE type='topup' AND created_at > NOW() - INTERVAL '1 day') AS topup_today,
                  (SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='topup') AS topup_all,
                  (SELECT COALESCE(SUM(amount),0) FROM transactions
                     WHERE type='package' AND created_at > NOW() - INTERVAL '1 day') AS sales_today
            """))

    # ─────────────────────────── TRANZAKSIYA ───────────────────────────
    async def log_transaction(self, user_id: int, type_: str, amount: int, note: str = "", currency: str = "sum"):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO transactions (user_id, type, currency, amount, note) VALUES ($1,$2,$3,$4,$5)",
                user_id, type_, currency, amount, note)

    async def user_history(self, user_id: int, limit: int = 15):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM transactions WHERE user_id = $1 ORDER BY id DESC LIMIT $2", user_id, limit)

    # ─────────────────────────── PAKETLAR ───────────────────────────
    async def packages(self, only_active=True):
        async with self.pool.acquire() as conn:
            sql = "SELECT * FROM packages"
            if only_active:
                sql += " WHERE active = TRUE"
            return await conn.fetch(sql + " ORDER BY sort, price")

    async def get_package(self, pid: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM packages WHERE id = $1", pid)

    async def add_package(self, title, points, price, bonus=0, badge=""):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO packages (title, points, price, bonus, badge, sort) "
                "VALUES ($1,$2,$3,$4,$5,(SELECT COALESCE(MAX(sort),0)+1 FROM packages)) RETURNING id",
                title, points, price, bonus, badge)

    async def update_package(self, pid: int, **kw):
        allowed = {"title", "points", "price", "bonus", "badge", "active", "sort"}
        sets, vals = [], []
        for i, (k, v) in enumerate((k, v) for k, v in kw.items() if k in allowed):
            sets.append(f"{k} = ${i + 2}")
            vals.append(v)
        if not sets:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(f"UPDATE packages SET {', '.join(sets)} WHERE id = $1", pid, *vals)

    async def del_package(self, pid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM packages WHERE id = $1", pid)

    # ─────────────────────────── O'YINLAR ───────────────────────────
    async def add_game(self, user_id, mode, level, result, points, elo_delta, moves=0, duration=0):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO games (user_id, mode, level, result, points, elo_delta, moves, duration) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                user_id, mode, level, result, points, elo_delta, moves, duration)

    async def games_today(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM games WHERE user_id = $1 AND created_at > NOW() - INTERVAL '1 day'", user_id)

    async def leaderboard(self, period: str = "all", limit: int = 20):
        async with self.pool.acquire() as conn:
            if period == "week":
                return await conn.fetch("""
                    SELECT u.user_id, u.fullname, u.username, u.photo_url, u.elo,
                           COUNT(*) FILTER (WHERE g.result='win') AS wins,
                           COALESCE(SUM(g.elo_delta),0) AS score
                    FROM games g JOIN users u ON u.user_id = g.user_id
                    WHERE g.created_at > NOW() - INTERVAL '7 days' AND u.is_blocked = FALSE
                    GROUP BY u.user_id ORDER BY score DESC, wins DESC LIMIT $1""", limit)
            return await conn.fetch(
                "SELECT user_id, fullname, username, photo_url, elo, wins, elo AS score "
                "FROM users WHERE is_blocked = FALSE ORDER BY elo DESC, wins DESC LIMIT $1", limit)

    async def my_rank(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*)+1 FROM users WHERE is_blocked = FALSE AND elo > "
                "(SELECT elo FROM users WHERE user_id = $1)", user_id) or 0

    # ─────────────────────────── TURNIRLAR ───────────────────────────
    async def create_tournament(self, title, descr, prize_text, prizes, ends_at):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO tournaments (title, descr, prize_text, prizes, ends_at) "
                "VALUES ($1,$2,$3,$4,$5) RETURNING id",
                title, descr, prize_text, json.dumps(prizes), ends_at)

    async def active_tournament(self):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM tournaments WHERE status='active' AND ends_at > NOW() "
                "ORDER BY id DESC LIMIT 1")

    async def tournaments(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tournaments ORDER BY id DESC LIMIT $1", limit)

    async def finish_tournament(self, tid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tournaments SET status='finished' WHERE id = $1", tid)

    async def add_tournament_score(self, tid: int, user_id: int, score: int, win: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tournament_scores (tournament_id, user_id, score, wins)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (tournament_id, user_id) DO UPDATE
                  SET score = tournament_scores.score + EXCLUDED.score,
                      wins  = tournament_scores.wins + EXCLUDED.wins,
                      updated_at = NOW()""", tid, user_id, score, 1 if win else 0)

    async def tournament_top(self, tid: int, limit: int = 20):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT s.user_id, s.score, s.wins, u.fullname, u.username, u.photo_url
                FROM tournament_scores s JOIN users u ON u.user_id = s.user_id
                WHERE s.tournament_id = $1 AND u.is_blocked = FALSE
                ORDER BY s.score DESC, s.wins DESC LIMIT $2""", tid, limit)

    async def tournament_place(self, tid: int, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT COUNT(*)+1 FROM tournament_scores
                WHERE tournament_id = $1 AND score > COALESCE(
                  (SELECT score FROM tournament_scores WHERE tournament_id=$1 AND user_id=$2), -1)""",
                tid, user_id) or 0

    async def add_payout(self, tid, user_id, place, amount):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO payouts (tournament_id, user_id, place, amount) VALUES ($1,$2,$3,$4) RETURNING id",
                tid, user_id, place, amount)

    async def payouts(self, status=None, limit=30):
        async with self.pool.acquire() as conn:
            if status:
                return await conn.fetch(
                    "SELECT * FROM payouts WHERE status=$1 ORDER BY id DESC LIMIT $2", status, limit)
            return await conn.fetch("SELECT * FROM payouts ORDER BY id DESC LIMIT $1", limit)

    async def mark_payout_paid(self, pid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payouts SET status='paid' WHERE id=$1", pid)

    # ─────────────────────────── E'LONLAR ───────────────────────────
    async def add_announcement(self, title, text, kind="info", emoji="📣", pinned=False):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO announcements (title, text, kind, emoji, pinned) "
                "VALUES ($1,$2,$3,$4,$5) RETURNING id", title, text, kind, emoji, pinned)

    async def announcements(self, only_active=True, limit=10):
        async with self.pool.acquire() as conn:
            sql = "SELECT * FROM announcements"
            if only_active:
                sql += " WHERE active = TRUE"
            return await conn.fetch(sql + " ORDER BY pinned DESC, id DESC LIMIT $1", limit)

    async def toggle_announcement(self, aid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE announcements SET active = NOT active WHERE id = $1", aid)

    async def del_announcement(self, aid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM announcements WHERE id = $1", aid)

    # ─────────────────── AVTOMATIK TO'LOV (humo/tolov moduli bilan mos) ───────────────────
    async def get_reserved_amounts(self, target_card: str = ""):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT final_amount FROM auto_payments WHERE status IN ('pending','matching') "
                "AND expires_at > NOW() AND ($1 = '' OR target_card = $1)", target_card)
        return {r["final_amount"] for r in rows}

    async def get_pending_auto_payments(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM auto_payments WHERE status='pending' AND expires_at > NOW() ORDER BY id")

    async def get_user_pending_auto_payments(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM auto_payments WHERE user_id=$1 AND status='pending' AND expires_at > NOW() "
                "ORDER BY id DESC", user_id)

    async def add_auto_payment(self, user_id, base_amount, final_amount, expires_at, target_card=""):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO auto_payments (user_id, base_amount, final_amount, expires_at, target_card) "
                "VALUES ($1,$2,$3,$4,$5) RETURNING id",
                user_id, base_amount, final_amount, expires_at, target_card)

    async def find_matching_auto_payment(self, amount: int, card_last: str = ""):
        """FOR UPDATE SKIP LOCKED — bitta to'lov ikki marta hisoblanmaydi."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("""
                UPDATE auto_payments SET status='matching'
                WHERE id = (
                    SELECT id FROM auto_payments
                    WHERE status='pending' AND final_amount = $1 AND expires_at > NOW()
                      AND ($2 = '' OR target_card = '' OR target_card = $2)
                    ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *""", amount, card_last)

    async def complete_auto_payment(self, payment_id: int, card_used: str = ""):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auto_payments SET status='completed', completed_at=NOW(), card_used=$2 WHERE id=$1",
                payment_id, card_used)

    async def cancel_auto_payment(self, payment_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auto_payments SET status='expired' WHERE id=$1 AND status IN ('pending','matching')",
                payment_id)

    async def expire_old_auto_payments(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auto_payments SET status='expired' WHERE status IN ('pending','matching') AND expires_at < NOW()")

    # ─────────────────────────── QO'LDA TO'LOV ───────────────────────────
    async def add_pending_payment(self, pay_id, user_id, amount, fullname=""):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pending_payments (pay_id, user_id, amount, fullname) VALUES ($1,$2,$3,$4)",
                pay_id, user_id, amount, fullname)

    async def pop_pending_payment(self, pay_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("DELETE FROM pending_payments WHERE pay_id=$1 RETURNING *", pay_id)

    # ─────────────────────────── MAJBURIY OBUNA KANALLARI ───────────────────────────
    async def add_required_channel(self, username, title, invite_link, chat_id=None):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO required_channels (chat_id, username, title, invite_link, sort) "
                "VALUES ($1,$2,$3,$4,(SELECT COALESCE(MAX(sort),0)+1 FROM required_channels)) RETURNING id",
                chat_id, username, title, invite_link)

    async def list_required_channels(self, only_active: bool = True):
        async with self.pool.acquire() as conn:
            sql = "SELECT * FROM required_channels"
            if only_active:
                sql += " WHERE active = TRUE"
            return await conn.fetch(sql + " ORDER BY sort")

    async def get_required_channel(self, cid: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM required_channels WHERE id = $1", cid)

    async def toggle_required_channel(self, cid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE required_channels SET active = NOT active WHERE id = $1", cid)

    async def delete_required_channel(self, cid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM required_channels WHERE id = $1", cid)

    # ─────────────────────────── QO'SHIMCHA ADMINLAR ───────────────────────────
    async def add_admin(self, user_id: int, fullname: str, added_by: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO admins (user_id, fullname, added_by) VALUES ($1,$2,$3) "
                "ON CONFLICT (user_id) DO NOTHING", user_id, fullname, added_by)

    async def remove_admin(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)

    async def list_admins(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM admins ORDER BY created_at")

    # ─────────────────────────── PULNI KARTAGA YECHISH ───────────────────────────
    async def add_withdrawal(self, user_id: int, amount: int, card: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO withdrawals (user_id, amount, card) VALUES ($1,$2,$3) RETURNING id",
                user_id, amount, card)

    async def get_withdrawal(self, wid: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM withdrawals WHERE id = $1", wid)

    async def set_withdrawal_status(self, wid: int, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE withdrawals SET status=$2, resolved_at=NOW() WHERE id=$1", wid, status)

    async def list_withdrawals(self, status: str | None = None, limit: int = 30):
        async with self.pool.acquire() as conn:
            if status:
                return await conn.fetch(
                    "SELECT * FROM withdrawals WHERE status=$1 ORDER BY id DESC LIMIT $2", status, limit)
            return await conn.fetch("SELECT * FROM withdrawals ORDER BY id DESC LIMIT $1", limit)
