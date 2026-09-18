#!/usr/bin/env python3
"""Интеграционный тест: прогоняет апдейты через настоящий диспетчер aiogram.

Сеть и Telegram не нужны: Bot и Message подменяются заглушками, а апдейты
подаются через ``Dispatcher.feed_update``. Проверяются: маршрутизация апдейтов,
мидлвари, выдача ключей, оплата с баланса, промокоды, админ-панель.

Запуск:  python tools/integration_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="dfvpnbot-it-")
os.environ.update({
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "ADMIN_IDS": "777",
    "DB_PATH": str(Path(TMP) / "it.sqlite3"),
    "VPN_PANEL": "pool",
    "PAYMENT_METHODS": "stars,balance,manual",
    "TRIAL_ENABLED": "true",
    "TRIAL_DAYS": "3",
    "THROTTLING_RATE": "0",  # в тесте не тормозим
    "LOG_LEVEL": "ERROR",
    "LOG_FILE": str(Path(TMP) / "bot.log"),
})

from aiogram import Bot  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.runtime import runtime as rt  # noqa: E402
from bot import build_dispatcher  # noqa: E402

OUT: list[tuple[str, str]] = []          # (вид, текст)
CALLBACK_ANSWERS: list[str] = []


def install_stubs(bot: Bot) -> None:
    """Перехватывает вызовы Telegram API на уровне класса Bot.

    В aiogram 3 шорткаты (`message.answer`, `call.answer`, `bot.send_message`)
    возвращают объект метода, который при await вызывает ``Bot.__call__`` —
    поэтому подменяем именно его.
    """

    async def fake_call(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "SendMessage":
            OUT.append(("message", getattr(method, "text", "") or ""))
        elif name == "SendPhoto":
            OUT.append(("photo", getattr(method, "caption", "") or ""))
        elif name == "SendDocument":
            OUT.append(("document", getattr(method, "caption", "") or ""))
        elif name == "SendInvoice":
            OUT.append(("invoice", f"{getattr(method, 'title', '')} / {getattr(method, 'prices', '')}"))
        elif name == "EditMessageText":
            OUT.append(("message", getattr(method, "text", "") or ""))
        elif name in ("AnswerCallbackQuery", "AnswerPreCheckoutQuery"):
            text = getattr(method, "text", None)
            if text:
                CALLBACK_ANSWERS.append(text)
            return True
        elif name in ("SetMyCommands", "DeleteMyCommands"):
            return True
        return None

    async def get_me(self, *a, **kw):
        return User(id=1, is_bot=True, first_name="TestBot", username="dfvpn_test_bot")

    Bot.__call__ = fake_call            # type: ignore[method-assign]
    Bot.get_me = get_me                 # type: ignore[method-assign]


def make_message(user_id: int, text: str, message_id: int = 1) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Тест", username=f"user{user_id}")
    chat = Chat(id=user_id, type="private")
    message = Message.model_construct(
        message_id=message_id, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text=text, html_text=text, entities=[], caption=None,
    )
    return Update.model_construct(update_id=message_id, message=message)


def make_callback(user_id: int, data: str, message_id: int = 1, text: str = "") -> Update:
    user = User(id=user_id, is_bot=False, first_name="Тест", username=f"user{user_id}")
    chat = Chat(id=user_id, type="private")
    message = Message.model_construct(
        message_id=message_id, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text=text or "…", html_text=text or "…", entities=[], caption=None,
    )
    call = CallbackQuery.model_construct(
        id=str(message_id), from_user=user, chat_instance="ci", message=message, data=data, inline_message_id=None,
    )
    return Update.model_construct(update_id=10000 + message_id, callback_query=call)


def sent(kind: str) -> str:
    return "\n".join(text for k, text in OUT if k == kind)


async def run() -> None:
    failures: list[str] = []

    def expect(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'✅' if condition else '❌'} {name}")
        if not condition:
            failures.append(f"{name}: {detail}")

    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    await rt.load(db, cfg)
    await db.add_keys([f"vless://pool-key-{i}@srv:443#pool{i}" for i in range(5)])

    bot = Bot(cfg.bot_token)
    install_stubs(bot)
    dp = build_dispatcher(cfg, db, bot, None)
    dp["bot_username"] = "dfvpn_test_bot"

    print("\n1. Пользовательский поток")
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "/start"))
    expect("на /start пришло приветствие", "Добро пожаловать" in sent("message"), sent("message")[:80])

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "menu:buy", 2))
    expect("магазин показан", "Выберите тариф" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 3))
    expect("заказ создан", "Заказ #" in sent("message"), sent("message")[:80])
    order_id = await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1")
    expect("в БД есть заказ kind=subscription",
           (await db.get_order(int(order_id)))["kind"] == "subscription")

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, f"pay:manual:{order_id}", 4))
    expect("ручная оплата предложена", "Оплата заказа" in sent("message"), sent("message")[:80])
    admins_notified = any("Ручная оплата" in text for _, text in OUT)
    expect("админ уведомлён о ручной оплате", admins_notified)

    # пополнение баланса и оплата с баланса
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "👤 Профиль"))
    expect("профиль показан", "Профиль" in sent("message"))
    await db.add_balance(555, 1000, method="admin", kind="adjust", note="test")

    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:1m", 5))
    new_order = await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1")
    await dp.feed_update(bot, make_callback(555, f"pay:balance:{new_order}", 6))
    expect("оплата с баланса прошла", "Ключ отправлен" in sent("message"), sent("message")[:120])
    sub = await db.active_subscription(555)
    expect("подписка выдана и ключ из пула", sub is not None and "pool-key" in (sub["vpn_key"] or ""))
    balance_after = int((await db.get_user(555))["balance"])
    expect("с баланса списано 299", balance_after == 701, str(balance_after))

    OUT.clear()
    await dp.feed_update(bot, make_message(555, "🔑 Мои ключи"))
    expect("список ключей показан", "Мои подписки" in sent("message"), sent("message")[:80])
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, f"sub:view:{sub['id']}", 7))
    expect("карточка подписки показана", "Подписка #" in sent("message"))

    print("\n2. Промокод через бота")
    await db.create_promo("SALE25", "percent", 25, max_uses=5, expires_at=None)
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "buy:3m", 8))
    promo_order = int(await db.scalar("SELECT id FROM orders WHERE user_id = 555 ORDER BY id DESC LIMIT 1"))
    await dp.feed_update(bot, make_callback(555, f"promo:order:{promo_order}", 9))
    await dp.feed_update(bot, make_message(555, "sale25", 10))
    order = await db.get_order(promo_order)
    expect("скидка 25% применена", int(order["amount"]) == 799 - 199, str(order["amount"]))
    expect("сообщение об успехе промокода", "применён" in sent("message"), sent("message")[:120])

    print("\n3. Админ-панель")
    OUT.clear()
    await dp.feed_update(bot, make_message(777, "/admin"))
    expect("админ-панель открыта", "Админ-панель" in sent("message"), sent("message")[:120])

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:stats", 11))
    expect("статистика показана", "Статистика" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:paneltest", 12))
    expect("проверка панели показана", "Проверка панели" in sent("message"))

    OUT.clear()
    await dp.feed_update(bot, make_callback(777, "adm:orders", 13))
    expect("список заказов показан", "Заказы" in sent("message"), sent("message")[:80])

    # подтверждение ручного заказа админом
    approve_order = int(await db.scalar(
        "SELECT id FROM orders WHERE status = 'pending' AND kind = 'subscription' ORDER BY id LIMIT 1"))
    OUT.clear()
    await dp.feed_update(bot, make_callback(777, f"adm:order:approve:{approve_order}", 14))
    approved = await db.get_order(approve_order)
    expect("заказ подтверждён админом", approved["status"] == "paid", approved["status"])

    # обычный пользователь не имеет доступа к админ-колбэкам
    OUT.clear()
    CALLBACK_ANSWERS.clear()
    await dp.feed_update(bot, make_callback(555, "adm:stats", 15))
    expect("админ-колбэк недоступен обычному пользователю",
           "Статистика" not in sent("message"),
           sent("message")[:80])

    print("\n4. Прочее")
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "🆘 Поддержка"))
    expect("раздел поддержки открыт", "Поддержка" in sent("message"))
    OUT.clear()
    await dp.feed_update(bot, make_callback(555, "support:faq", 16))
    expect("FAQ показан", "Частые вопросы" in sent("message"))
    OUT.clear()
    await dp.feed_update(bot, make_message(555, "🤝 Пригласить друга"))
    expect("реферальный раздел показан", "Приглашайте друзей" in sent("message"))
    expect("в ссылке есть ref-метка", "start=ref_555" in sent("message"), sent("message")[-200:])

    OUT.clear()
    await dp.feed_update(bot, make_message(999, "/start ref_555"))
    ref_user = await db.get_user(999)
    expect("реферал привязан", int(ref_user["referrer_id"]) == 555, str(ref_user["referrer_id"]))

    await db.close()
    await bot.session.close()

    print(f"\n{'=' * 60}")
    if failures:
        print(f"ОШИБКИ ({len(failures)}):")
        for item in failures:
            print(f"  • {item}")
        sys.exit(1)
    print("Интеграционный тест пройден ✅")


if __name__ == "__main__":
    asyncio.run(run())
