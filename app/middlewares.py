"""Мидлвари: анти-спам, регистрация пользователей, обязательная подписка на канал."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Config
from app.db import Database
from app.keyboards import subscription_check_kb
from app.services.subs import notify_admins

log = logging.getLogger(__name__)

SKIP_CHANNEL_CHECK = {"check:subscription", "help:how"}


class ThrottlingMiddleware(BaseMiddleware):
    """Не чаще одного апдейта в `rate` секунд на пользователя."""

    def __init__(self, rate: float = 0.6) -> None:
        self.rate = max(0.0, rate)
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or self.rate <= 0:
            return await handler(event, data)
        now = time.monotonic()
        last = self._last.get(user.id, 0.0)
        if now - last < self.rate:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Слишком быстро, подождите секунду.", show_alert=False)
            return None
        self._last[user.id] = now
        if len(self._last) > 20000:  # защита от роста памяти
            cutoff = now - 3600
            self._last = {uid: ts for uid, ts in self._last.items() if ts > cutoff}
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя в БД и блокирует доступ заблокированным."""

    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        row = await self.db.ensure_user(
            user.id, user.username, getattr(user, "full_name", None)
        )
        if row["is_banned"] and not self.cfg.is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Доступ ограничен администратором.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Ваш доступ к боту ограничен.")
            return None
        data["db_user"] = row
        return await handler(event, data)


class RequiredChannelMiddleware(BaseMiddleware):
    """Проверяет подписку на обязательные каналы (если они заданы)."""

    def __init__(self, bot, cfg: Config) -> None:
        self.bot = bot
        self.cfg = cfg
        self._cache: dict[int, tuple[float, bool]] = {}
        self.ttl = 120.0

    async def _subscribed(self, user_id: int, channel: str) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id=channel, user_id=user_id)
        except Exception as exc:  # бот не админ канала / неверный @username
            log.warning("Не удалось проверить подписку на %s: %s", channel, exc)
            return True
        return member.status in {"creator", "administrator", "member", "restricted"}

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not self.cfg.required_channels:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or self.cfg.is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data in SKIP_CHANNEL_CHECK:
            return await handler(event, data)

        cached = self._cache.get(user.id)
        now = time.monotonic()
        if cached and now - cached[0] < self.ttl:
            subscribed = cached[1]
        else:
            subscribed = True
            for channel in self.cfg.required_channels:
                if not await self._subscribed(user.id, channel):
                    subscribed = False
                    break
            self._cache[user.id] = (now, subscribed)

        if subscribed:
            return await handler(event, data)

        text = (
            "🔒 <b>Доступ к боту — по подписке на канал</b>\n\n"
            "Подпишитесь на наши каналы ниже и нажмите «Я подписался» — это бесплатно "
            "и занимает пару секунд."
        )
        markup = subscription_check_kb(self.cfg.required_channels)
        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            except Exception:
                await event.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
            await event.answer()
        elif isinstance(event, Message):
            await event.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return None


class ErrorsMiddleware(BaseMiddleware):
    """Ловит исключения в хендлерах: логирует и сообщает админам."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка обработки апдейта: %s", exc)
            bot = data.get("bot")
            if bot is not None:
                await notify_admins(bot, self.cfg, f"⚠️ <b>Ошибка в боте</b>\n<code>{type(exc).__name__}: {exc}</code>")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⚠️ Произошла ошибка, попробуйте ещё раз.", show_alert=True)
                except Exception:
                    pass
            return None
