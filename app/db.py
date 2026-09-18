"""Слой хранения: SQLite через aiosqlite.

В базе живут: пользователи, тарифы, заказы, подписки, пул ключей, промокоды,
тикеты поддержки, история платежей, рассылки и настройки.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id     INTEGER PRIMARY KEY,
    username        TEXT,
    full_name       TEXT,
    balance         INTEGER NOT NULL DEFAULT 0,
    referrer_id     INTEGER,
    ref_earned      INTEGER NOT NULL DEFAULT 0,
    trial_used      INTEGER NOT NULL DEFAULT 0,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    code            TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    price           INTEGER NOT NULL,
    days            INTEGER NOT NULL,
    traffic_gb      INTEGER NOT NULL DEFAULT 0,
    devices         INTEGER NOT NULL DEFAULT 1,
    is_active       INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    plan_code       TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    base_amount     INTEGER NOT NULL,
    discount        INTEGER NOT NULL DEFAULT 0,
    promo_code      TEXT,
    method          TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'subscription',
    status          TEXT NOT NULL DEFAULT 'pending',
    payment_id      TEXT,
    created_at      TEXT NOT NULL,
    paid_at         TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    plan_code       TEXT NOT NULL,
    vpn_key         TEXT,
    sub_url         TEXT,
    panel           TEXT NOT NULL,
    panel_username  TEXT,
    devices         INTEGER NOT NULL DEFAULT 1,
    traffic_gb      INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    notified        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions(status, expires_at);

CREATE TABLE IF NOT EXISTS key_pool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    plan_code       TEXT,
    title           TEXT,
    is_used         INTEGER NOT NULL DEFAULT 0,
    used_by         INTEGER,
    used_at         TEXT,
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promos (
    code            TEXT PRIMARY KEY,
    discount_type   TEXT NOT NULL DEFAULT 'percent',
    value           INTEGER NOT NULL,
    max_uses        INTEGER NOT NULL DEFAULT 0,
    used            INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    used_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL,
    from_admin      INTEGER NOT NULL DEFAULT 0,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    amount          INTEGER NOT NULL,
    method          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    audience        TEXT NOT NULL,
    sent            INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""

DEFAULT_PLANS: Sequence[tuple[str, str, int, int, int, int, int]] = (
    # code, title, price, days, traffic_gb, devices, sort_order
    ("1m", "🔥 1 месяц", 150, 30, 0, 2, 10),
    ("3m", "⭐ 3 месяца", 405, 90, 0, 3, 20),
    ("6m", "💎 6 месяцев", 765, 180, 0, 5, 30),
    ("12m", "👑 12 месяцев", 1350, 365, 0, 5, 40),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


class Database:
    """Небольшая обёртка над aiosqlite: одна долгоживущая коннекция + блокировка."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------
    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._seed_plans()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызывался")
        return self._conn

    async def _seed_plans(self) -> None:
        async with self._lock:
            cur = await self.conn.execute("SELECT COUNT(*) AS c FROM plans")
            row = await cur.fetchone()
            if row and row["c"]:
                return
            for code, title, price, days, traffic, devices, order in DEFAULT_PLANS:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO plans(code, title, price, days, traffic_gb, devices, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (code, title, price, days, traffic, devices, order),
                )
            await self.conn.commit()

    # --- generic helpers ----------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            return list(await cur.fetchall())

    async def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # --- users ---------------------------------------------------------
    async def ensure_user(self, telegram_id: int, username: str | None, full_name: str | None,
                          referrer_id: int | None = None) -> aiosqlite.Row:
        now = to_iso(utcnow())
        row = await self.fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        if row is None:
            if referrer_id == telegram_id:
                referrer_id = None
            if referrer_id is not None:
                exists = await self.fetchone("SELECT 1 FROM users WHERE telegram_id = ?", (referrer_id,))
                if exists is None:
                    referrer_id = None
            await self.execute(
                "INSERT INTO users(telegram_id, username, full_name, referrer_id, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_id, username, full_name, referrer_id, now, now),
            )
            row = await self.fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        else:
            await self.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen_at = ? WHERE telegram_id = ?",
                (username, full_name, now, telegram_id),
            )
        assert row is not None
        return row

    async def get_user(self, telegram_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))

    async def set_user_field(self, telegram_id: int, field: str, value: Any) -> None:
        allowed = {"username", "full_name", "balance", "referrer_id", "ref_earned", "trial_used", "is_banned"}
        if field not in allowed:
            raise ValueError(f"Недопустимое поле users.{field}")
        await self.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))

    async def add_balance(self, telegram_id: int, delta: int, *, method: str = "balance",
                          kind: str = "topup", note: str = "") -> None:
        await self.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (delta, telegram_id))
        await self.log_payment(telegram_id, delta, method, kind, note)

    async def all_user_ids(self, audience: str = "all") -> list[int]:
        if audience == "subscribers":
            sql = (
                "SELECT DISTINCT user_id FROM subscriptions WHERE status = 'active' AND expires_at > ?"
            )
            rows = await self.fetchall(sql, (to_iso(utcnow()),))
        elif audience == "no_subscription":
            sql = (
                "SELECT telegram_id FROM users WHERE is_banned = 0 AND telegram_id NOT IN "
                "(SELECT user_id FROM subscriptions WHERE status = 'active' AND expires_at > ?)"
            )
            rows = await self.fetchall(sql, (to_iso(utcnow()),))
        else:
            rows = await self.fetchall("SELECT telegram_id FROM users WHERE is_banned = 0")
        return [int(r[0]) for r in rows]

    # --- plans ---------------------------------------------------------
    async def active_plans(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order, price")

    async def all_plans(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM plans ORDER BY sort_order, price")

    async def get_plan(self, code: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM plans WHERE code = ?", (code,))

    async def upsert_plan(self, code: str, title: str, price: int, days: int,
                          traffic_gb: int, devices: int, is_active: int = 1) -> None:
        existing = await self.get_plan(code)
        if existing is None:
            await self.execute(
                "INSERT INTO plans(code, title, price, days, traffic_gb, devices, is_active, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code, title, price, days, traffic_gb, devices, is_active, price),
            )
        else:
            await self.execute(
                "UPDATE plans SET title = ?, price = ?, days = ?, traffic_gb = ?, devices = ?, is_active = ? "
                "WHERE code = ?",
                (title, price, days, traffic_gb, devices, is_active, code),
            )

    async def delete_plan(self, code: str) -> None:
        await self.execute("DELETE FROM plans WHERE code = ?", (code,))

    # --- orders ---------------------------------------------------------
    async def create_order(self, user_id: int, plan_code: str, amount: int, base_amount: int,
                           promo_code: str | None, method: str, kind: str = "subscription",
                           discount: int = 0, status: str = "pending", payment_id: str | None = None,
                           note: str = "") -> int:
        return await self.execute(
            "INSERT INTO orders(user_id, plan_code, amount, base_amount, discount, promo_code, method, kind, "
            "status, payment_id, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, plan_code, amount, base_amount, discount, promo_code, method, kind,
             status, payment_id, to_iso(utcnow()), note),
        )

    async def get_order(self, order_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    async def set_order_status(self, order_id: int, status: str, payment_id: str | None = None) -> None:
        if payment_id is not None:
            await self.execute(
                "UPDATE orders SET status = ?, payment_id = ?, paid_at = ? WHERE id = ?",
                (status, payment_id, to_iso(utcnow()), order_id),
            )
        else:
            await self.execute(
                "UPDATE orders SET status = ?, paid_at = ? WHERE id = ?",
                (status, to_iso(utcnow()), order_id),
            )

    async def user_orders(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )

    async def pending_orders(self, method: str | None = None, limit: int = 20) -> list[aiosqlite.Row]:
        if method:
            return await self.fetchall(
                "SELECT * FROM orders WHERE status = 'pending' AND method = ? ORDER BY id DESC LIMIT ?",
                (method, limit),
            )
        return await self.fetchall(
            "SELECT * FROM orders WHERE status = 'pending' ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def stale_orders(self, hours: int) -> list[aiosqlite.Row]:
        border = to_iso(utcnow() - timedelta(hours=hours))
        return await self.fetchall(
            "SELECT * FROM orders WHERE status = 'pending' AND created_at < ?", (border,)
        )

    # --- subscriptions ---------------------------------------------------
    async def active_subscription(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active' AND expires_at > ? "
            "ORDER BY expires_at DESC LIMIT 1",
            (user_id, to_iso(utcnow())),
        )

    async def user_subscriptions(self, user_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC", (user_id,)
        )

    async def create_subscription(self, user_id: int, plan_code: str, vpn_key: str, sub_url: str | None,
                                  panel: str, panel_username: str, devices: int, traffic_gb: int,
                                  expires_at: datetime) -> int:
        return await self.execute(
            "INSERT INTO subscriptions(user_id, plan_code, vpn_key, sub_url, panel, panel_username, devices, "
            "traffic_gb, expires_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (user_id, plan_code, vpn_key, sub_url, panel, panel_username, devices, traffic_gb,
             to_iso(expires_at), to_iso(utcnow())),
        )

    async def update_subscription(self, sub_id: int, **fields: Any) -> None:
        allowed = {"vpn_key", "sub_url", "expires_at", "status", "devices", "traffic_gb",
                   "notified", "panel_username", "plan_code"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self.execute(
            f"UPDATE subscriptions SET {assignments} WHERE id = ?", (*updates.values(), sub_id)
        )

    async def subscriptions_expiring(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM subscriptions WHERE status = 'active' AND expires_at > ?", (to_iso(utcnow()),)
        )

    async def subscriptions_expired(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM subscriptions WHERE status = 'active' AND expires_at <= ?", (to_iso(utcnow()),)
        )

    # --- key pool ---------------------------------------------------------
    async def add_keys(self, keys: Iterable[str], plan_code: str | None = None, title: str = "") -> int:
        added = 0
        for raw in keys:
            key = raw.strip()
            if not key:
                continue
            try:
                await self.execute(
                    "INSERT INTO key_pool(content, plan_code, title, added_at) VALUES (?, ?, ?, ?)",
                    (key, plan_code or None, title, to_iso(utcnow())),
                )
                added += 1
            except aiosqlite.IntegrityError:
                continue
        return added

    async def take_key(self, plan_code: str | None, user_id: int) -> Optional[aiosqlite.Row]:
        """Берёт ключ из пула: сначала под конкретный тариф, затем «для любого»,
        затем любой свободный (нужно для пробного периода и нестандартных тарифов)."""
        row = None
        if plan_code:
            row = await self.fetchone(
                "SELECT * FROM key_pool WHERE is_used = 0 AND plan_code = ? ORDER BY id LIMIT 1", (plan_code,)
            )
        if row is None:
            row = await self.fetchone(
                "SELECT * FROM key_pool WHERE is_used = 0 AND plan_code IS NULL ORDER BY id LIMIT 1"
            )
        if row is None:
            row = await self.fetchone("SELECT * FROM key_pool WHERE is_used = 0 ORDER BY id LIMIT 1")
        if row is None:
            return None
        await self.execute(
            "UPDATE key_pool SET is_used = 1, used_by = ?, used_at = ? WHERE id = ?",
            (user_id, to_iso(utcnow()), row["id"]),
        )
        return row

    async def pool_stats(self) -> dict[str, int]:
        free = int(await self.scalar("SELECT COUNT(*) FROM key_pool WHERE is_used = 0"))
        used = int(await self.scalar("SELECT COUNT(*) FROM key_pool WHERE is_used = 1"))
        return {"free": free, "used": used, "total": free + used}

    async def clear_used_keys(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS c FROM key_pool WHERE is_used = 1")
        count = int(row["c"]) if row else 0
        await self.execute("DELETE FROM key_pool WHERE is_used = 1")
        return count

    # --- promos ------------------------------------------------------------
    async def create_promo(self, code: str, discount_type: str, value: int, max_uses: int,
                           expires_at: str | None) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO promos(code, discount_type, value, max_uses, used, expires_at, is_active, "
            "created_at) VALUES (?, ?, ?, ?, 0, ?, 1, ?)",
            (code.upper(), discount_type, value, max_uses, expires_at, to_iso(utcnow())),
        )

    async def get_promo(self, code: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM promos WHERE code = ?", (code.upper(),))

    async def promo_valid(self, code: str, user_id: int) -> tuple[bool, str, Optional[aiosqlite.Row]]:
        row = await self.get_promo(code)
        if row is None or not row["is_active"]:
            return False, "Промокод не найден.", None
        if row["expires_at"] and from_iso(row["expires_at"]) < utcnow():
            return False, "Срок действия промокода истёк.", None
        if row["max_uses"] and row["used"] >= row["max_uses"]:
            return False, "Лимит активаций промокода исчерпан.", None
        used = await self.fetchone(
            "SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", (code.upper(), user_id)
        )
        if used is not None:
            return False, "Вы уже использовали этот промокод.", None
        return True, "OK", row

    async def apply_promo(self, code: str, user_id: int) -> None:
        code = code.upper()
        await self.execute(
            "INSERT INTO promo_uses(code, user_id, used_at) VALUES (?, ?, ?)",
            (code, user_id, to_iso(utcnow())),
        )
        await self.execute("UPDATE promos SET used = used + 1 WHERE code = ?", (code,))

    async def all_promos(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM promos ORDER BY created_at DESC")

    async def toggle_promo(self, code: str, active: bool) -> None:
        await self.execute("UPDATE promos SET is_active = ? WHERE code = ?", (1 if active else 0, code.upper()))

    async def delete_promo(self, code: str) -> None:
        await self.execute("DELETE FROM promos WHERE code = ?", (code.upper(),))

    # --- tickets -----------------------------------------------------------
    async def create_ticket(self, user_id: int, subject: str) -> int:
        now = to_iso(utcnow())
        return await self.execute(
            "INSERT INTO tickets(user_id, subject, status, created_at, updated_at) VALUES (?, ?, 'open', ?, ?)",
            (user_id, subject, now, now),
        )

    async def open_ticket(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (user_id,)
        )

    async def get_ticket(self, ticket_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    async def add_ticket_message(self, ticket_id: int, from_admin: bool, text: str) -> None:
        now = to_iso(utcnow())
        await self.execute(
            "INSERT INTO ticket_messages(ticket_id, from_admin, text, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, 1 if from_admin else 0, text, now),
        )
        await self.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))

    async def close_ticket(self, ticket_id: int) -> None:
        await self.execute("UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?",
                           (to_iso(utcnow()), ticket_id))

    async def ticket_messages(self, ticket_id: int, limit: int = 30) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id DESC LIMIT ?", (ticket_id, limit)
        )

    async def open_tickets(self, limit: int = 20) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM tickets WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?", (limit,)
        )

    # --- payments / stats ---------------------------------------------------
    async def log_payment(self, user_id: int | None, amount: int, method: str, kind: str, note: str = "") -> None:
        await self.execute(
            "INSERT INTO payments_log(user_id, amount, method, kind, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount, method, kind, note, to_iso(utcnow())),
        )

    async def log_broadcast(self, admin_id: int, text: str, audience: str, sent: int, failed: int) -> None:
        await self.execute(
            "INSERT INTO broadcasts(admin_id, text, audience, sent, failed, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, text, audience, sent, failed, to_iso(utcnow())),
        )

    async def stats(self) -> dict[str, int]:
        now = utcnow()
        day_ago = to_iso(now - timedelta(days=1))
        week_ago = to_iso(now - timedelta(days=7))
        month_ago = to_iso(now - timedelta(days=30))
        now_iso = to_iso(now)
        return {
            "users_total": int(await self.scalar("SELECT COUNT(*) FROM users")),
            "users_day": int(await self.scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day_ago,))),
            "users_week": int(await self.scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week_ago,))),
            "banned": int(await self.scalar("SELECT COUNT(*) FROM users WHERE is_banned = 1")),
            "subs_active": int(await self.scalar(
                "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND expires_at > ?", (now_iso,))),
            "subs_expired": int(await self.scalar(
                "SELECT COUNT(*) FROM subscriptions WHERE expires_at <= ?", (now_iso,))),
            "orders_pending": int(await self.scalar("SELECT COUNT(*) FROM orders WHERE status = 'pending'")),
            "orders_paid": int(await self.scalar("SELECT COUNT(*) FROM orders WHERE status = 'paid'")),
            "revenue_total": int(await self.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND kind = 'subscription'")),
            "revenue_day": int(await self.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND kind = 'subscription' "
                "AND paid_at >= ?", (day_ago,))),
            "revenue_month": int(await self.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND kind = 'subscription' "
                "AND paid_at >= ?", (month_ago,))),
            "topups_total": int(await self.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND kind = 'topup'")),
            "tickets_open": int(await self.scalar("SELECT COUNT(*) FROM tickets WHERE status = 'open'")),
        }

    async def top_referrers(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT u.telegram_id, u.username, u.full_name, u.ref_earned, "
            "(SELECT COUNT(*) FROM users x WHERE x.referrer_id = u.telegram_id) AS invited "
            "FROM users u WHERE u.ref_earned > 0 OR (SELECT COUNT(*) FROM users x WHERE x.referrer_id = u.telegram_id) > 0 "
            "ORDER BY u.ref_earned DESC, invited DESC LIMIT ?",
            (limit,),
        )

    # --- settings ------------------------------------------------------------
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
