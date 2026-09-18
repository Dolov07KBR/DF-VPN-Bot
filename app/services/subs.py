"""Бизнес-логика подписок: выдача, продление, истечение, напоминания, рефералы."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config import Config
from app.db import Database, from_iso, to_iso, utcnow
from app.keyboards import main_menu, subscription_kb
from app.services.panels import PanelError, get_panel
from app.runtime import runtime as rt
from app.services.payments import YooKassaClient
from app.utils import esc, fmt_dt, human_left, make_qr_png, price

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------
async def notify_admins(bot: Bot, cfg: Config, text: str, reply_markup=None) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as exc:  # администратор мог заблокировать бота
            log.warning("Не удалось уведомить админа %s: %s", admin_id, exc)


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("Сообщение пользователю %s не доставлено: %s", chat_id, exc)
        return False
    except Exception as exc:  # pragma: no cover
        log.warning("Ошибка отправки пользователю %s: %s", chat_id, exc)
        return False


# ---------------------------------------------------------------------------
# Выдача и продление подписок
# ---------------------------------------------------------------------------
async def send_subscription_card(bot: Bot, cfg: Config, db: Database, user_id: int, sub_row) -> None:
    """Отправляет пользователю сообщение с конфигом, QR и инструкцией."""
    sub_url = sub_row["sub_url"] if "sub_url" in sub_row.keys() else None
    link = sub_url or (sub_row["vpn_key"] or "")
    text = (
        "🎉 <b>VPN готов!</b>\n\n"
        f"Тариф: <b>{esc(sub_row['plan_code'])}</b>\n"
        f"Действует до: <b>{fmt_dt(sub_row['expires_at'])}</b> "
        f"(осталось {human_left(sub_row['expires_at'])})\n"
        f"Устройств: <b>{sub_row['devices']}</b>, трафик: <b>"
        f"{'безлимит' if not sub_row['traffic_gb'] else str(sub_row['traffic_gb']) + ' ГБ'}</b>\n\n"
        f"🔗 <b>Ваш конфиг:</b>\n<code>{esc(link)}</code>\n\n"
        "Нажмите кнопку ниже, чтобы получить QR-код и инструкцию по подключению."
    )
    markup = subscription_kb(sub_row["id"], sub_url, sub_row["vpn_key"] or "", cfg.support_username)
    await safe_send(bot, user_id, text, markup)

    qr = make_qr_png(link) if link else None
    if qr:
        try:
            from aiogram.types import BufferedInputFile

            await bot.send_photo(
                user_id,
                BufferedInputFile(qr, filename="vpn-qr.png"),
                caption="📷 QR-код вашей конфигурации — отсканируйте в приложении VPN.",
            )
        except Exception as exc:  # pragma: no cover
            log.info("QR не отправлен: %s", exc)


async def grant_subscription(bot: Bot, cfg: Config, db: Database, user_id: int, plan_code: str,
                             *, days: Optional[int] = None, devices: Optional[int] = None,
                             traffic_gb: Optional[int] = None, notify: bool = True,
                             quiet_admin: bool = False):
    """Создаёт новую подписку или продлевает уже активную."""
    plan = await db.get_plan(plan_code)
    if plan is None and plan_code != "trial":
        raise PanelError(f"Тариф {plan_code} не найден.")
    if plan is not None and not plan["is_active"] and plan_code != "trial":
        raise PanelError("Тариф временно недоступен.")

    if plan_code == "trial":
        days = days or rt.trial_days
        devices = devices or cfg.trial_devices
        traffic_gb = traffic_gb if traffic_gb is not None else cfg.trial_traffic_gb
    else:
        days = days or int(plan["days"])
        devices = devices or int(plan["devices"])
        traffic_gb = traffic_gb if traffic_gb is not None else int(plan["traffic_gb"])

    panel = get_panel(cfg, db)
    existing = await db.active_subscription(user_id)

    if existing is not None and existing["panel"] == panel.name:
        # Продление: тот же клиент, срок сдвигается вперёд.
        new_expires = from_iso(existing["expires_at"]) + timedelta(days=days)
        extended = await panel.extend_client(panel_username=existing["panel_username"] or "", days=days)
        await db.update_subscription(
            existing["id"],
            expires_at=to_iso(new_expires),
            status="active",
            notified="",
            plan_code=plan_code if plan_code != "trial" else existing["plan_code"],
        )
        if not extended:
            log.warning("Панель не подтвердила продление для %s", existing["panel_username"])
        refreshed = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (existing["id"],))
        if notify:
            await safe_send(
                bot, user_id,
                f"✅ <b>Подписка продлена до {fmt_dt(new_expires)}</b> "
                f"(+{days} дн.). Приятного пользования!",
            )
        if not quiet_admin:
            await notify_admins(
                bot, cfg,
                f"🔄 Продление подписки\nПользователь: <code>{user_id}</code>\n"
                f"Тариф: {esc(plan_code)}\nСрок: +{days} дн. → до {fmt_dt(new_expires)}",
            )
        return refreshed

    issued = await panel.create_client(
        user_id=user_id, days=days, devices=devices or 1,
        traffic_gb=traffic_gb or 0, plan_code=plan_code,
    )
    expires_at = utcnow() + timedelta(days=days)
    sub_id = await db.create_subscription(
        user_id=user_id, plan_code=plan_code, vpn_key=issued.vpn_key, sub_url=issued.sub_url,
        panel=issued.panel, panel_username=issued.panel_username, devices=devices or 1,
        traffic_gb=traffic_gb or 0, expires_at=expires_at,
    )
    row = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if notify:
        await send_subscription_card(bot, cfg, db, user_id, row)
        await safe_send(bot, user_id, "Главное меню 👇", main_menu(cfg.is_admin(user_id), rt.trial_enabled))
    if not quiet_admin:
        await notify_admins(
            bot, cfg,
            f"🆕 Новая подписка\nПользователь: <code>{user_id}</code>\nТариф: {esc(plan_code)}\n"
            f"Срок: {days} дн. (до {fmt_dt(expires_at)})\nПанель: {issued.panel}",
        )
    return row


# ---------------------------------------------------------------------------
# Оплата заказа
# ---------------------------------------------------------------------------
async def apply_referral_bonus(bot: Bot, cfg: Config, db: Database, buyer_id: int, amount: int) -> None:
    """Начисляет пригласившему процент от первого/каждого платежа приглашённого."""
    if rt.ref_percent <= 0:
        return
    user = await db.get_user(buyer_id)
    if user is None or not user["referrer_id"]:
        return
    bonus = amount * rt.ref_percent // 100
    if bonus <= 0:
        return
    referrer_id = int(user["referrer_id"])
    await db.add_balance(referrer_id, bonus, method="referral", kind="referral",
                         note=f"бонус за покупку пользователя {buyer_id}")
    await db.execute(
        "UPDATE users SET ref_earned = ref_earned + ? WHERE telegram_id = ?", (bonus, referrer_id)
    )
    await safe_send(
        bot, referrer_id,
        f"🎉 <b>Реферальный бонус!</b>\nВаш приглашённый совершил покупку, "
        f"на баланс начислено <b>{price(bonus, cfg.currency)}</b>.",
    )


async def complete_order(bot: Bot, cfg: Config, db: Database, order_id: int,
                         *, payment_id: str | None = None, silent: bool = False) -> bool:
    """Помечает заказ оплаченным и выдаёт то, что заказано (подписку или баланс)."""
    order = await db.get_order(order_id)
    if order is None:
        log.warning("Заказ #%s не найден", order_id)
        return False
    if order["status"] == "paid":
        return True

    await db.set_order_status(order_id, "paid", payment_id=payment_id)
    user_id = int(order["user_id"])

    if order["kind"] == "topup":
        await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                         (order["amount"], user_id))
        await db.log_payment(user_id, order["amount"], order["method"], "topup", f"заказ #{order_id}")
        if not silent:
            user = await db.get_user(user_id)
            await safe_send(
                bot, user_id,
                f"💰 Баланс пополнен на <b>{price(order['amount'], cfg.currency)}</b>.\n"
                f"Текущий баланс: <b>{price(int(user['balance']) if user else 0, cfg.currency)}</b>",
            )
    else:
        await db.log_payment(user_id, order["amount"], order["method"], "order", f"заказ #{order_id}")
        try:
            await grant_subscription(bot, cfg, db, user_id, order["plan_code"], notify=not silent)
        except PanelError as exc:
            await safe_send(
                bot, user_id,
                "⚠️ Оплата получена, но выдать конфиг автоматически не получилось.\n"
                "Мы уже работаем над этим — администратор выдаст ключ вручную.",
            )
            await notify_admins(
                bot, cfg,
                f"🚨 <b>Оплата прошла, но выдача не удалась</b>\nЗаказ #{order_id}, "
                f"пользователь <code>{user_id}</code>\nОшибка: {esc(exc)}",
            )
            raise
        await apply_referral_bonus(bot, cfg, db, user_id, order["amount"])

    await notify_admins(
        bot, cfg,
        f"💾 <b>Оплачен заказ #{order_id}</b>\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Сумма: {price(order['amount'], cfg.currency)}\n"
        f"Способ: {order['method']}\n"
        f"Позиция: {esc(order['plan_code'])} ({order['kind']})",
    )
    return True


# ---------------------------------------------------------------------------
# Фоновые задачи
# ---------------------------------------------------------------------------
async def process_expired(bot: Bot, cfg: Config, db: Database) -> None:
    """Отключает истёкшие подписки в панели и уведомляет пользователей."""
    panel = get_panel(cfg, db)
    for sub in await db.subscriptions_expired():
        if cfg.auto_disable_expired:
            try:
                await panel.set_enabled(panel_username=sub["panel_username"] or "", enabled=False)
            except Exception as exc:
                log.warning("Не удалось отключить клиента %s: %s", sub["panel_username"], exc)
        await db.update_subscription(sub["id"], status="expired")
        await safe_send(
            bot, int(sub["user_id"]),
            "⌛️ <b>Подписка закончилась</b>\n\n"
            f"Тариф: {esc(sub['plan_code'])}\n"
            "Продлите подписку, чтобы вернуть доступ — конфиг останется тем же.",
            None,
        )
        await notify_admins(bot, cfg, f"⌛️ Подписка истекла: <code>{sub['user_id']}</code> ({esc(sub['plan_code'])})")


async def send_reminders(bot: Bot, cfg: Config, db: Database) -> None:
    """Напоминает о скором окончании подписки (за 3 дня / за 1 день по умолчанию)."""
    for sub in await db.subscriptions_expiring():
        left_hours = (from_iso(sub["expires_at"]) - utcnow()).total_seconds() / 3600
        notified = set(filter(None, (sub["notified"] or "").split(",")))
        for days in cfg.notify_days_before:
            marker = f"{days}d"
            if left_hours <= days * 24 and marker not in notified:
                notified.add(marker)
                await db.update_subscription(sub["id"], notified=",".join(sorted(notified)))
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Продлить подписку", callback_data=f"sub:renew:{sub['id']}")
                ]])
                await safe_send(
                    bot, int(sub["user_id"]),
                    f"⏰ <b>Подписка заканчивается</b>\n\nОсталось: <b>{human_left(sub['expires_at'])}</b> "
                    f"(до {fmt_dt(sub['expires_at'])}).\nПродлите вовремя, чтобы не потерять доступ.",
                    markup,
                )
                break


async def poll_yookassa_orders(bot: Bot, cfg: Config, db: Database) -> None:
    """Резервный путь: если вебхуки недоступны, сами проверяем статус платежей ЮKassa."""
    if not cfg.yookassa_enabled:
        return
    orders = await db.pending_orders(method="yookassa")
    if not orders:
        return
    client = YooKassaClient(cfg)
    try:
        for order in orders:
            if not order["payment_id"]:
                continue
            try:
                paid = await client.is_paid(order["payment_id"], int(order["amount"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("Проверка заказа #%s: %s", order["id"], exc)
                continue
            if paid:
                try:
                    await complete_order(bot, cfg, db, int(order["id"]), payment_id=order["payment_id"])
                except Exception as exc:  # noqa: BLE001
                    log.error("Не удалось выдать заказ #%s: %s", order["id"], exc)
    finally:
        await client.close()


async def cancel_stale_orders(db: Database, hours: int) -> None:
    for order in await db.stale_orders(hours):
        await db.set_order_status(int(order["id"]), "canceled")


async def background_worker(bot: Bot, cfg: Config, db: Database) -> None:
    """Единый цикл фоновых задач: напоминания, истечение, проверка платежей."""
    while True:
        try:
            await process_expired(bot, cfg, db)
            await send_reminders(bot, cfg, db)
            await poll_yookassa_orders(bot, cfg, db)
            await cancel_stale_orders(db, cfg.orders_ttl_hours)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка в фоновом цикле: %s", exc)
        await asyncio.sleep(180)
