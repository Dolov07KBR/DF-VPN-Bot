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

    async def load(self, db: Database, cfg: Config) -> None:
        self.trial_enabled = cfg.trial_enabled
        self.trial_days = cfg.trial_days
        self.ref_percent = cfg.ref_percent

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
