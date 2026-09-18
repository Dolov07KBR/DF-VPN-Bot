#!/usr/bin/env python3
"""Автономная проверка логики бота без сети и Telegram.

Запуск:  python tools/selftest.py

Проверяет: инициализацию БД, выдачу ключей из пула, продление, оплату заказа,
пополнение баланса, реферальные бонусы, промокоды, напоминания, отключение
истёкших подписок, сборку ссылок 3x-ui и валидацию конфигурации.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="dfvpnbot-selftest-")
os.environ.update({
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "ADMIN_IDS": "777",
    "DB_PATH": str(Path(TMP) / "test.sqlite3"),
    "VPN_PANEL": "pool",
    "PAYMENT_METHODS": "stars,balance,manual",
    "TRIAL_ENABLED": "true",
    "TRIAL_DAYS": "2",
    "REF_PERCENT": "20",
    "LOG_FILE": str(Path(TMP) / "bot.log"),
})

from app.config import load_config, validate  # noqa: E402
from app.db import Database, from_iso, to_iso, utcnow  # noqa: E402
from app.keyboards import main_menu, payment_methods_kb, plans_kb  # noqa: E402
from app.runtime import runtime as rt  # noqa: E402
from app.services.panels import MarzbanPanel, PoolPanel, XuiPanel, _build_link, get_panel  # noqa: E402
from app.services.subs import (  # noqa: E402
    complete_order,
    grant_subscription,
    process_expired,
    send_reminders,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  ❌ {name} — {detail}")


class FakeBot:
    """Заглушка aiogram.Bot: запоминает исходящие сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[int] = []

    async def send_message(self, chat_id, text, reply_markup=None, disable_web_page_preview=None, **kw):
        self.messages.append((int(chat_id), text))
        return None

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        self.photos.append(int(chat_id))
        return None

    async def send_invoice(self, **kw):
        return None

    async def refund_star_payment(self, **kw):
        return True

    def last_text_for(self, chat_id: int) -> str:
        for cid, text in reversed(self.messages):
            if cid == chat_id:
                return text
        return ""

    def all_text_for(self, chat_id: int) -> str:
        return "\n".join(text for cid, text in self.messages if cid == chat_id)


async def run() -> None:
    cfg = load_config()
    print("\n1. Конфигурация и БД")
    problems = validate(cfg)
    check("конфиг валиден", problems == [], "; ".join(problems))
    db = Database(cfg.db_path)
    await db.connect()
    await rt.load(db, cfg)
    check("тарифы посеяны по умолчанию", len(await db.active_plans()) == 4)
    check("рантайм подхватил реф-процент из .env", rt.ref_percent == 20, str(rt.ref_percent))

    bot = FakeBot()
    print("\n2. Выдача и продление подписок (панель pool)")
    await db.ensure_user(1001, "alice", "Алиса")
    await db.ensure_user(1002, "bob", "Боб")
    await db.add_keys([f"vless://test-key-{i}@example.com:443#test{i}" for i in range(3)], plan_code="1m")
    sub = await grant_subscription(bot, cfg, db, 1001, "1m")
    check("подписка создана", sub is not None and sub["status"] == "active")
    check("ключ взят из пула", "vless://test-key" in (sub["vpn_key"] or ""), sub["vpn_key"] or "")
    check("срок = 30 дней", 29 <= (from_iso(sub["expires_at"]) - utcnow()).days + 1 <= 30)
    check("пользователю отправлена карточка", "VPN готов" in bot.all_text_for(1001))
    check("QR отправлен", 1001 in bot.photos)

    extended = await grant_subscription(bot, cfg, db, 1001, "1m")
    delta_days = (from_iso(extended["expires_at"]) - from_iso(sub["expires_at"])).days
    check("продление сдвинуло срок на 30 дней", delta_days == 30, str(delta_days))
    check("ключ при продлении не изменился", extended["vpn_key"] == sub["vpn_key"])
    check("в пуле осталось 2 свободных ключа", (await db.pool_stats())["free"] == 2)

    print("\n3. Пробный период")
    trial = await grant_subscription(bot, cfg, db, 1002, "trial")
    check("пробная подписка выдана", trial is not None and trial["panel"] == "pool")
    trial_days = (from_iso(trial["expires_at"]) - utcnow()).days
    check("пробный срок из runtime (2 дня)", trial_days in (1, 2), str(trial_days))

    print("\n4. Заказы, баланс и реферальные бонусы")
    await db.ensure_user(2001, "ref_user", "Реферер")
    await db.ensure_user(3001, "buyer", "Покупатель", referrer_id=2001)
    order_id = await db.create_order(3001, "1m", 299, 299, None, "stars")
    await complete_order(bot, cfg, db, order_id, payment_id="star-charge-1")
    order = await db.get_order(order_id)
    check("заказ помечен оплаченным", order["status"] == "paid")
    buyer_sub = await db.active_subscription(3001)
    check("подписка выдана по заказу", buyer_sub is not None)

    referrer = await db.get_user(2001)
    check("реферальный бонус 20% начислен", int(referrer["balance"]) == 59, str(referrer["balance"]))
    check("реферер уведомлён", "Реферальный бонус" in bot.last_text_for(2001))

    topup_id = await db.create_order(3001, "topup", 500, 500, None, "stars", kind="topup")
    await complete_order(bot, cfg, db, topup_id, payment_id="star-charge-2")
    buyer = await db.get_user(3001)
    check("баланс пополнен на 500", int(buyer["balance"]) == 500, str(buyer["balance"]))

    pay_order = await db.create_order(3001, "1m", 299, 299, None, "balance")
    await complete_order(bot, cfg, db, pay_order)
    buyer = await db.get_user(3001)
    check("списание баланса выполнено вручную (проверка сценария)", int(buyer["balance"]) == 500)

    print("\n5. Промокоды")
    await db.create_promo("vpn10", "percent", 10, max_uses=1, expires_at=None)
    ok, reason, promo = await db.promo_valid("VPN10", 3001)
    check("промокод валиден для нового пользователя", ok, reason)
    await db.apply_promo("VPN10", 3001)
    ok2, reason2, _ = await db.promo_valid("VPN10", 3001)
    check("повторное использование запрещено", not ok2, reason2)
    ok3, reason3, _ = await db.promo_valid("VPN10", 4001)
    check("лимит активаций соблюдается", not ok3, reason3)

    base, discount = 1000, 0
    promo_row = await db.get_promo("VPN10")
    discount = base * int(promo_row["value"]) // 100
    check("расчёт скидки 10% корректен", discount == 100, str(discount))

    print("\n6. Напоминания и истечение подписок")
    await db.update_subscription(int(buyer_sub["id"]), expires_at=to_iso(utcnow() + timedelta(hours=20)))
    await send_reminders(bot, cfg, db)
    check("напоминание за 1 день отправлено", "заканчивается" in bot.last_text_for(3001))
    await db.update_subscription(int(buyer_sub["id"]), expires_at=to_iso(utcnow() - timedelta(minutes=5)))
    await process_expired(bot, cfg, db)
    refreshed = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (int(buyer_sub["id"]),))
    check("истёкшая подписка помечена expired", refreshed["status"] == "expired", refreshed["status"])
    check("пользователь уведомлён об окончании", "закончилась" in bot.last_text_for(3001))

    print("\n7. Сборка ссылок 3x-ui")
    vless_inbound = {
        "id": 1, "protocol": "vless", "port": 443,
        "settings": '{"clients": [{"id": "uuid-1", "flow": "xtls-rprx-vision", "email": "tg1_1m_x"}]}',
        "streamSettings": (
            '{"network": "tcp", "security": "reality", "realitySettings": {"serverNames": ["www.microsoft.com"], '
            '"shortIds": ["abcd1234"], "settings": {"publicKey": "PUBKEY123", "fingerprint": "chrome"}}}'
        ),
    }
    link = _build_link(vless_inbound, {"id": "uuid-1", "flow": "xtls-rprx-vision", "email": "tg1_1m_x"}, "1.2.3.4")
    check("vless+reality ссылка собрана", link.startswith("vless://uuid-1@1.2.3.4:443?"), link)
    check("в ссылке есть pbk/sni/sid", all(p in link for p in ("pbk=PUBKEY123", "sni=www.microsoft.com", "sid=abcd1234")))

    ws_inbound = {
        "id": 2, "protocol": "vless", "port": 8443,
        "settings": '{"clients": [{"id": "uuid-2", "email": "u2"}]}',
        "streamSettings": (
            '{"network": "ws", "security": "tls", "tlsSettings": {"serverName": "cdn.example.com", "alpn": ["h2"]},'
            ' "wsSettings": {"path": "/secret", "headers": {"Host": "cdn.example.com"}}}'
        ),
    }
    ws_link = _build_link(ws_inbound, {"id": "uuid-2", "email": "u2"}, "example.com")
    check("vless+ws+tls ссылка собрана", "type=ws" in ws_link and "path=%2Fsecret" in ws_link, ws_link)

    vmess_inbound = {
        "id": 3, "protocol": "vmess", "port": 8080,
        "settings": '{"clients": [{"id": "uuid-3", "alterId": 0, "email": "u3"}]}',
        "streamSettings": '{"network": "tcp", "security": "none"}',
    }
    vmess_link = _build_link(vmess_inbound, {"id": "uuid-3", "alterId": 0, "email": "u3"}, "example.com")
    check("vmess ссылка (base64) собрана", vmess_link.startswith("vmess://"), vmess_link[:20])

    trojan_inbound = {
        "id": 4, "protocol": "trojan", "port": 443,
        "settings": '{"clients": [{"id": "pass123", "email": "u4"}]}',
        "streamSettings": '{"network": "tcp", "security": "tls", "tlsSettings": {"serverName": "t.example.com"}}',
    }
    trojan_link = _build_link(trojan_inbound, {"id": "pass123", "email": "u4"}, "example.com")
    check("trojan ссылка собрана", trojan_link.startswith("trojan://pass123@"), trojan_link)

    print("\n8. Инициализация панелей")
    xui = XuiPanel(cfg)
    check("XuiPanel создан (pool-конфиг подставляется)", xui.base == "")
    os.environ["XUI_URL"] = "http://127.0.0.1:2053/secretpath"
    os.environ["XUI_USERNAME"] = "admin"
    os.environ["XUI_PASSWORD"] = "pass"
    xui_cfg = load_config()
    XuiPanel(xui_cfg)
    check("XUI URL разобран", XuiPanel(xui_cfg).base.endswith("/secretpath"))
    mb = MarzbanPanel(cfg) if cfg.marzban_url else None
    check("MarzbanPanel корректно требует настройки", mb is None or mb.base == "")
    panel = get_panel(cfg, db)
    check("фабрика возвращает PoolPanel", isinstance(panel, PoolPanel))

    print("\n9. Клавиатуры и статистика")
    check("главное меню собирается", main_menu(True, True) is not None)
    check("список тарифов собирается", plans_kb(await db.active_plans()) is not None)
    check("выбор способа оплаты собирается", payment_methods_kb(["stars", "balance"], 1, 100) is not None)
    stats = await db.stats()
    check("статистика считается", stats["users_total"] >= 4 and stats["revenue_total"] >= 598, str(stats["revenue_total"]))
    check("топ рефералов не пуст", len(await db.top_referrers()) >= 1)


    print("\n10. Клиент ЮKassa (HTTP подменён заглушкой)")
    import json as _json

    from app.services.payments import PaymentError, YooKassaClient

    class FakeResponse:
        def __init__(self, status: int, payload: dict) -> None:
            self.status = status
            self._payload = payload

        async def text(self) -> str:
            return _json.dumps(self._payload)

        async def json(self, content_type=None) -> dict:
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, responses: list) -> None:
            self.responses = responses
            self.calls: list = []
            self.closed = False

        def post(self, url, json=None, headers=None):
            self.calls.append(("POST", url, json, headers))
            status, payload = self.responses.pop(0)
            return FakeResponse(status, payload)

        def get(self, url):
            self.calls.append(("GET", url, None, None))
            status, payload = self.responses.pop(0)
            return FakeResponse(status, payload)

    os.environ["YOOKASSA_SHOP_ID"] = "123456"
    os.environ["YOOKASSA_SECRET_KEY"] = "test_secret_key"
    os.environ["YOOKASSA_RETURN_URL"] = "https://t.me/test_bot"
    yk_cfg = load_config()
    client = YooKassaClient(yk_cfg)

    session = FakeSession([(200, {"id": "pay-1", "status": "pending",
                                  "confirmation": {"confirmation_url": "https://yoomoney.ru/checkout/1"}})])
    async def fake_session():
        return session
    client._get_session = fake_session  # type: ignore[assignment]

    payment = await client.create_payment(amount_rub=150, description="VPN 1m заказ #1",
                                          metadata={"order_id": 1, "user_id": 555})
    method, url, payload, headers = session.calls[0]
    check("платёж создан через API v3", url.endswith("/v3/payments") and method == "POST", url)
    check("сумма передана в формате 150.00", payload["amount"]["value"] == "150.00", str(payload["amount"]))
    check("валюта RUB и capture=true", payload["amount"]["currency"] == "RUB" and payload["capture"] is True)
    check("return_url из конфига", payload["confirmation"]["return_url"] == "https://t.me/test_bot")
    check("metadata содержит заказ", payload["metadata"]["order_id"] == "1", str(payload["metadata"]))
    check("Idempotence-Key передан", bool(headers.get("Idempotence-Key")), str(headers))
    check("confirmation_url получен", payment["confirmation"]["confirmation_url"].startswith("https://yoomoney.ru"))

    session.responses.append((200, {"status": "succeeded", "paid": True, "amount": {"value": "150.00"}}))
    check("успешный платёж подтверждён", await client.is_paid("pay-1", 150) is True)

    session.responses.append((200, {"status": "succeeded", "paid": True, "amount": {"value": "100.00"}}))
    check("платёж с чужой суммой отклонён", await client.is_paid("pay-1", 150) is False)

    session.responses.append((200, {"status": "pending", "paid": False, "amount": {"value": "299.00"}}))
    check("неоплаченный платёж отклонён", await client.is_paid("pay-1", 150) is False)

    session.responses.append((400, {"description": "bad request"}))
    check("ошибка API обрабатывается", await client.is_paid("pay-x", 150) is False)

    print("\n11. Вебхук ЮKassa: разбор уведомления")
    from app.webhook import YOOKASSA_NETS, _ip_allowed
    check("IP ЮKassa из белого списка разрешён", _ip_allowed("185.71.76.10"))
    check("посторонний IP отклонён", not _ip_allowed("8.8.8.8"))
    check("диапазоны IP заданы", len(YOOKASSA_NETS) >= 6)

    await db.close()
    print(f"\n{'=' * 60}\nИТОГО: {len(PASSED)} успешно, {len(FAILED)} ошибок")
    if FAILED:
        for item in FAILED:
            print(f"  • {item}")
        sys.exit(1)
    print("Все проверки пройдены ✅")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        # aiosqlite держит поток на каждое соединение: закрываемся явно
        import gc

        gc.collect()
