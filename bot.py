#!/usr/bin/env python3
"""DF VPN Bot — точка входа.

Запуск:  python bot.py     (или systemd-сервис dfvpnbot, см. install.sh)

Порядок работы:
1. читаем .env, проверяем конфигурацию;
2. подключаем SQLite, применяем рантайм-настройки;
3. собираем диспетчер aiogram (мидлвари + роутеры пользователя и админа);
4. при WEBHOOK_ENABLED=true поднимаем HTTP-сервер для уведомлений ЮKassa;
5. крутим long-polling и фоновый воркер (напоминания, истечение, проверка платежей).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from app import __author__, __author_url__, __version__
from app.config import load_config, validate
from app.db import Database
from app.handlers import admin as admin_handlers
from app.handlers import user as user_handlers
from app.middlewares import (
    ErrorsMiddleware,
    RequiredChannelMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from app.runtime import runtime
from app.extensions import extension_manager
from app.services.panels import close_panel, get_panel
from app.services.payments import YooKassaClient, YooMoneyClient
from app.services.subs import background_worker, notify_admins
from app.webhook import start_webhook_server

log = logging.getLogger("dfvpnbot")


def setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                                            encoding="utf-8"))
    except OSError as exc:  # нет прав на запись — работаем только в stdout
        print(f"Не удалось открыть лог-файл {log_file}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def set_commands(bot: Bot, admin_ids: tuple[int, ...]) -> None:
    """Publish only useful commands; admin commands are visible only to admins."""
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ])
    for admin_id in admin_ids:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="help", description="ℹ️ Помощь"),
            BotCommand(command="admin", description="🛠 Админ-панель"),
        ], scope=BotCommandScopeChat(chat_id=admin_id))


def build_dispatcher(cfg, db: Database, bot: Bot, yk, ym=None) -> Dispatcher:
    """Собирает диспетчер: мидлвари, роутеры, данные для хендлеров."""
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["db"] = db
    dp["yk"] = yk
    dp["ym"] = ym

    # --- мидлвари ---
    dp.update.outer_middleware(ErrorsMiddleware(cfg))
    throttling = ThrottlingMiddleware(cfg.throttling_rate)
    dp.message.middleware(throttling)
    dp.callback_query.middleware(throttling)
    dp.message.middleware(UserMiddleware(db, cfg))
    dp.callback_query.middleware(UserMiddleware(db, cfg))
    channel_guard = RequiredChannelMiddleware(bot, cfg)
    dp.message.middleware(channel_guard)
    dp.callback_query.middleware(channel_guard)

    # --- роутеры: админские раньше пользовательских ---
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)
    return dp


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file)

    problems = validate(cfg)
    if problems:
        log.error("Проблемы конфигурации:\n  • %s", "\n  • ".join(problems))
        log.error("Исправьте .env и запустите бота снова (образец — .env.example).")
        raise SystemExit(2)

    db = Database(cfg.db_path)
    await db.connect()
    await runtime.load(db, cfg)
    extensions_enabled = await db.get_setting("extensions.enabled", "1") == "1"
    extension_manager.folder = Path(__file__).resolve().parent / "custom_extensions"
    extension_report = await extension_manager.load(extensions_enabled)
    log.info("Расширения: enabled=%s loaded=%s errors=%s", extensions_enabled,
             len(extension_report.loaded), len(extension_report.errors))

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("Бот @%s (id=%s), версия %s, автор @%s (%s)", me.username, me.id, __version__, __author__, __author_url__)

    # Create configured clients independently of the runtime on/off switch so an
    # administrator can enable a ready provider without restarting the bot.
    yk = YooKassaClient(cfg) if cfg.yookassa_shop_id and cfg.yookassa_secret_key else None
    ym = YooMoneyClient(cfg) if cfg.yoomoney_wallet and cfg.yoomoney_token else None
    if cfg.yookassa_enabled:
        log.info("Оплата картой/СБП: включена (магазин %s)", cfg.yookassa_shop_id)
    if cfg.yoomoney_enabled:
        log.info("Оплата ЮMoney: включена (кошелёк %s)", cfg.yoomoney_wallet)
    if cfg.stars_enabled:
        log.info("Telegram Stars: включены (курс %.2f ₽ за ⭐️)", cfg.stars_rate)

    dp = build_dispatcher(cfg, db, bot, yk, ym)
    dp["bot_username"] = me.username

    # --- вебхуки ЮKassa (необязательно) ---
    webhook_runner = None
    if cfg.webhook_enabled:
        try:
            webhook_runner = await start_webhook_server(cfg, db, bot, yk)
        except OSError as exc:
            log.error("Не удалось запустить сервер вебхуков на %s:%s — %s",
                      cfg.webhook_host, cfg.webhook_port, exc)
            log.error("Платежи будут подтверждаться опросом API (раз в 3 минуты).")

    # --- фоновая обработка (напоминания, истечение, проверка платежей) ---
    worker = asyncio.create_task(background_worker(bot, cfg, db, ym), name="background-worker")

    # --- проверка панели на старте ---
    try:
        panel = get_panel(cfg, db)
        ok, message = await panel.health()
        log.info("Панель '%s': %s — %s", cfg.panel, "OK" if ok else "ПРОБЛЕМА", message)
        if not ok:
            await notify_admins(bot, cfg, f"⚠️ Панель выдачи '{cfg.panel}': {message}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Проверка панели не удалась: %s", exc)

    await set_commands(bot, cfg.admin_ids)
    await notify_admins(
        bot, cfg,
        f"🚀 <b>Бот запущен</b> (v{__version__})\nПанель: <code>{cfg.panel}</code>\n"
        f"Оплата: <code>{', '.join(cfg.payment_methods)}</code>",
    )

    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover
            pass

    async def _watch_stop() -> None:
        await stop_event.wait()
        log.info("Получен сигнал остановки — завершаем работу…")
        worker.cancel()
        await dp.stop_polling()

    watcher = asyncio.create_task(_watch_stop())

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher.cancel()
        worker.cancel()
        if webhook_runner is not None:
            await webhook_runner.cleanup()
        if yk is not None:
            await yk.close()
        await close_panel()
        await db.close()
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code not in (0, None):
            sys.exit(exc.code)
