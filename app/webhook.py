"""HTTP-сервер для приёма уведомлений ЮKassa (webhook).

ЮKassa присылает POST с телом вида::

    {"type": "notification", "event": "payment.succeeded", "object": {...}}

Телу уведомления доверять нельзя: получив событие, бот переспрашивает платёж
через API (``GET /v3/payments/{id}``) и выдаёт доступ, только если платёж
действительно ``succeeded`` и сумма совпадает с суммой заказа. Если обработка
не удалась, отдаём 500 — ЮKassa повторит уведомление.
"""

from __future__ import annotations

import ipaddress
import json
import logging

from aiogram import Bot
from aiohttp import web

from app.config import Config
from app.db import Database
from app.services.payments import YooKassaClient
from app.services.subs import complete_order

log = logging.getLogger(__name__)

# Диапазоны IP, с которых ЮKassa отправляет уведомления (по документации).
YOOKASSA_NETS = [
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.154.128/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "2a02:5180::/32",
]


def _ip_allowed(remote: str) -> bool:
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    for cidr in YOOKASSA_NETS:
        if address in ipaddress.ip_network(cidr):
            return True
    return False


async def start_webhook_server(cfg: Config, db: Database, bot: Bot, yk: YooKassaClient | None):
    """Поднимает aiohttp-сервер с маршрутом уведомлений. Возвращает AppRunner."""

    async def handle_yookassa(request: web.Request) -> web.Response:
        raw = await request.text()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            log.warning("Некорректный JSON от ЮKassa: %s", raw[:200])
            return web.Response(status=400, text="bad json")

        event = payload.get("event", "")
        obj = payload.get("object") or {}
        payment_id = obj.get("id", "")
        log.info("Вебхук ЮKassa: %s, платёж %s", event, payment_id)

        if event != "payment.succeeded" or not payment_id:
            # Прочие события просто подтверждаем.
            return web.Response(status=200, text="ignored")

        order = await db.fetchone(
            "SELECT * FROM orders WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (payment_id,)
        )
        if order is None:
            log.warning("Платёж %s не привязан ни к одному заказу", payment_id)
            return web.Response(status=200, text="unknown payment")

        if order["status"] == "paid":
            return web.Response(status=200, text="already paid")

        if yk is None:
            return web.Response(status=500, text="yookassa disabled")

        paid = await yk.is_paid(payment_id, int(order["amount"]))
        if not paid:
            log.warning("Платёж %s не подтверждён через API — пропускаем", payment_id)
            return web.Response(status=200, text="not verified")

        try:
            await complete_order(bot, cfg, db, int(order["id"]), payment_id=payment_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка выдачи по платежу %s: %s", payment_id, exc)
            return web.Response(status=500, text="retry later")

        return web.Response(status=200, text="ok")

    async def handle_health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "df-vpn-bot webhook"})

    app = web.Application()
    app.router.add_post(cfg.webhook_path, handle_yookassa)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port)
    await site.start()
    log.info(
        "Вебхуки ЮKassa слушаем на %s:%s%s (публичный URL: %s)",
        cfg.webhook_host, cfg.webhook_port, cfg.webhook_path, cfg.webhook_public_url or "не задан",
    )
    return runner
