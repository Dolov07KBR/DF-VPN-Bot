"""Изменяемые «на лету» настройки: пробный период и реферальный процент.

Значения по умолчанию берутся из .env, а поверх них ложатся правки из админки
(хранятся в таблице settings). Это позволяет менять маркетинговые параметры
без перезапуска бота.
"""

from __future__ import annotations

from app.config import Config
from app.db import Database


class Runtime:
    def __init__(self) -> None:
        self.trial_enabled: bool = False
        self.trial_days: int = 3
        self.ref_percent: int = 15
        self.payment_methods: set[str] = set()

    async def load(self, db: Database, cfg: Config) -> None:
        self.trial_enabled = cfg.trial_enabled
        self.trial_days = cfg.trial_days
        self.ref_percent = cfg.ref_percent
        self.payment_methods = set(cfg.payment_methods)

        raw_enabled = await db.get_setting("trial_enabled")
        raw_days = await db.get_setting("trial_days")
        raw_ref = await db.get_setting("ref_percent")


        if raw_enabled not in ("", None):
            self.trial_enabled = raw_enabled in {"1", "true", "yes", "on"}
        if raw_days and raw_days.isdigit():
            self.trial_days = int(raw_days)
            self.trial_enabled = self.trial_days > 0
        if raw_ref and raw_ref.isdigit():
            self.ref_percent = max(0, min(50, int(raw_ref)))

        # Admin payment switches override .env defaults when explicitly stored.
        for method in ("stars", "cryptopay", "yaseller", "tgpayments", "yookassa",
                       "yoomoney", "wata", "platega", "cardlink", "demo"):
            value = await db.get_setting(f"payment.{method}.enabled")
            if value == "1":
                self.payment_methods.add(method)
            elif value == "0":
                self.payment_methods.discard(method)

    async def set_payment(self, db: Database, method: str, enabled: bool) -> None:
        if enabled:
            self.payment_methods.add(method)
        else:
            self.payment_methods.discard(method)
        await db.set_setting(f"payment.{method}.enabled", "1" if enabled else "0")

    async def set_trial(self, db: Database, days: int) -> None:
        self.trial_days = max(0, days)
        self.trial_enabled = self.trial_days > 0
        await db.set_setting("trial_days", str(self.trial_days))
        await db.set_setting("trial_enabled", "1" if self.trial_enabled else "0")

    async def set_ref_percent(self, db: Database, percent: int) -> None:
        self.ref_percent = max(0, min(50, percent))
        await db.set_setting("ref_percent", str(self.ref_percent))

    async def ensure_ref_percent(self, db: Database) -> None:
        raw = await db.get_setting("ref_percent")
        if raw and raw.isdigit():
            self.ref_percent = max(0, min(50, int(raw)))


runtime = Runtime()
