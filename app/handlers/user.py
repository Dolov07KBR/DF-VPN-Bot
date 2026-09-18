"""Хендлеры пользовательской части: магазин, оплата, ключи, профиль, рефералы, поддержка."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery

from app.config import Config
from app.db import Database
from app.keyboards import (
    BTN_BUY,
    BTN_HELP,
    BTN_MY_KEYS,
    BTN_PROFILE,
    BTN_REF,
    BTN_SUPPORT,
    BTN_TRIAL,
    back_kb,
    cancel_kb,
    checkout_kb,
    keys_kb,
    main_menu,
    payment_methods_kb,
    plans_kb,
    profile_kb,
    referral_kb,
    subscription_kb,
    support_kb,
    topup_amounts_kb,
    topup_methods_kb,
)
from app.runtime import runtime as rt
from app.services.panels import PanelError
from app.services.payments import PaymentError, create_stars_invoice
from app.services.subs import complete_order, grant_subscription, notify_admins, safe_send
from app.utils import (
    STATUS_RU,
    apps_instructions,
    esc,
    fmt_dt,
    human_left,
    make_qr_png,
    price,
)

log = logging.getLogger(__name__)
router = Router(name="user")

FAQ_TEXT = (
    "❓ <b>Частые вопросы</b>\n\n"
    "<b>Как подключиться?</b>\n"
    "«🔑 Мои ключи» → выберите подписку → «📲 Как подключиться». Импортируйте ссылку в приложение "
    "(v2rayNG, Hiddify, Streisand, NekoBox) и нажмите «Подключиться».\n\n"
    "<b>Интернет не работает после подключения?</b>\n"
    "Проверьте, что приложение показывает «Подключено», перезапустите приложение, "
    "попробуйте другой сервер в списке. Если не помогло — напишите в поддержку.\n\n"
    "<b>Можно ли на несколько устройств?</b>\n"
    "Да, в рамках лимита устройств вашего тарифа (указан в карточке подписки).\n\n"
    "<b>Как продлить?</b>\n"
    "«🔑 Мои ключи» → подписка → «🔄 Продлить». Конфиг остаётся тем же, срок продлевается.\n\n"
    "<b>Оплата не прошла, деньги списались?</b>\n"
    "Напишите в поддержку — проверим платёж и вернём деньги либо выдадим доступ вручную."
)


class PromoStates(StatesGroup):
    enter = State()


class TopupStates(StatesGroup):
    custom_amount = State()


class TicketStates(StatesGroup):
    subject = State()
    message = State()
    reply = State()


# ---------------------------------------------------------------------------
# Старт и главное меню
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config, db: Database, bot: Bot, bot_username: str = "") -> None:
    user = message.from_user
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    row = await db.get_user(user.id)
    if row is not None and row["referrer_id"] is None and payload.startswith("ref_"):
        raw = payload[4:]
        if raw.isdigit() and int(raw) != user.id:
            await db.set_user_field(user.id, "referrer_id", int(raw))
            await db.set_user_field(user.id, "username", user.username)
            if cfg.ref_welcome_bonus > 0:
                await db.add_balance(user.id, cfg.ref_welcome_bonus, method="referral",
                                     kind="welcome", note="бонус за переход по ссылке")
                await safe_send(
                    bot, user.id,
                    f"🎁 Вам начислен приветственный бонус <b>{price(cfg.ref_welcome_bonus, cfg.currency)}</b> "
                    "на баланс — можно использовать при первой покупке!",
                )
            await safe_send(
                bot, int(raw),
                "👋 По вашей ссылке зашёл новый пользователь! Если он оформит подписку, "
                f"вы получите {rt.ref_percent}% от платежа на баланс.",
            )

    greeting = (
        f"👋 <b>Добро пожаловать в {esc(cfg.bot_name)}!</b>\n\n"
        "Здесь можно купить быстрый и стабильный VPN:\n"
        "• работает на телефоне, компьютере и в браузере;\n"
        "• до 5 устройств в зависимости от тарифа;\n"
        "• оплата картой, СБП или Telegram Stars;\n"
        "• выдача ключа сразу после оплаты.\n\n"
        "Выберите действие в меню ниже 👇"
    )
    sub = await db.active_subscription(user.id)
    if sub is not None:
        greeting += f"\n🟢 Ваша подписка активна, осталось <b>{human_left(sub['expires_at'])}</b>."
    await message.answer(greeting, reply_markup=main_menu(cfg.is_admin(user.id), rt.trial_enabled))


@router.message(Command("menu"))
async def cmd_menu(message: Message, cfg: Config) -> None:
    await message.answer("Главное меню 👇", reply_markup=main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, cfg: Config) -> None:
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Главное меню 👇",
                              reply_markup=main_menu(cfg.is_admin(call.from_user.id), rt.trial_enabled))
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "check:subscription")
async def cb_check_subscription(call: CallbackQuery, cfg: Config, bot: Bot) -> None:
    if not cfg.required_channels:
        await call.answer("Подписка не требуется 🙂", show_alert=False)
        return
    for channel in cfg.required_channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=call.from_user.id)
        except Exception:
            continue
        if member.status not in {"creator", "administrator", "member", "restricted"}:
            await call.answer("Вы ещё не подписались на все каналы 🙂", show_alert=True)
            return
    await call.message.edit_text("✅ Спасибо за подписку! Доступ открыт.")
    await call.message.answer("Главное меню 👇", reply_markup=main_menu(cfg.is_admin(call.from_user.id), rt.trial_enabled))
    await call.answer("Готово!")


# ---------------------------------------------------------------------------
# Магазин: тарифы и оформление заказа
# ---------------------------------------------------------------------------
async def _show_plans(target: Message | CallbackQuery, db: Database, cfg: Config) -> None:
    plans = await db.active_plans()
    if not plans:
        text = "😔 Сейчас нет доступных тарифов. Загляните позже или напишите в поддержку."
        if isinstance(target, CallbackQuery):
            await target.message.answer(text)
        else:
            await target.answer(text)
        return
    text = (
        "🛒 <b>Выберите тариф</b>\n\n"
        f"Оплата: картой/СБП, Telegram Stars или с баланса.\n"
        f"Пробный период: {'да, ' + str(rt.trial_days) + ' дн.' if rt.trial_enabled else 'нет'}\n\n"
        "Чем длиннее тариф — тем выгоднее цена за месяц."
    )
    markup = plans_kb(plans, cfg.currency)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == BTN_BUY)
async def msg_plans(message: Message, db: Database, cfg: Config) -> None:
    await _show_plans(message, db, cfg)


@router.callback_query(F.data == "menu:buy")
async def cb_plans(call: CallbackQuery, db: Database, cfg: Config) -> None:
    await _show_plans(call, db, cfg)
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery, db: Database, cfg: Config) -> None:
    code = call.data.split(":", 1)[1]
    plan = await db.get_plan(code)
    if plan is None or not plan["is_active"]:
        await call.answer("Этот тариф недоступен.", show_alert=True)
        return
    order_id = await db.create_order(
        user_id=call.from_user.id, plan_code=code, amount=int(plan["price"]),
        base_amount=int(plan["price"]), promo_code=None, method="none", kind="subscription",
    )
    await _show_order(call, db, cfg, order_id)
    await call.answer()


async def _show_order(target: CallbackQuery, db: Database, cfg: Config, order_id: int) -> None:
    order = await db.get_order(order_id)
    if order is None:
        await target.answer("Заказ не найден.", show_alert=True)
        return
    plan = await db.get_plan(order["plan_code"])
    user = await db.get_user(int(order["user_id"]))
    balance = int(user["balance"]) if user else 0
    text = (
        f"🧾 <b>Заказ #{order_id}</b>\n\n"
        f"Тариф: <b>{esc(plan['title'] if plan else order['plan_code'])}</b>\n"
        f"Срок: {plan['days'] if plan else '—'} дн.\n"
        f"Устройств: {plan['devices'] if plan else '—'}\n"
    )
    if order["discount"]:
        text += f"Скидка: <b>−{price(int(order['discount']), cfg.currency)}</b> (промокод <code>{esc(order['promo_code'])}</code>)\n"
        text += f"Было: {price(int(order['base_amount']), cfg.currency)}\n"
    text += f"\nК оплате: <b>{price(int(order['amount']), cfg.currency)}</b>"
    available = [m for m in cfg.payment_methods if m != "none"]
    if "balance" in available and balance < int(order["amount"]):
        available = [m for m in available if m != "balance"]
    markup = payment_methods_kb(available, order_id, balance, cfg.currency)
    try:
        await target.message.edit_text(text, reply_markup=markup)
    except Exception:
        await target.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("order:cancel:"))
async def cb_cancel_order(call: CallbackQuery, db: Database, cfg: Config) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await db.get_order(order_id)
    if order is None or order["status"] != "pending":
        await call.answer("Заказ уже неактуален.", show_alert=True)
        return
    await db.set_order_status(order_id, "canceled")
    await call.message.edit_text("❌ Заказ отменён. Если оплата уже ушла — напишите в поддержку.")
    await call.answer()


# --- промокоды ---
@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.enter)
    await call.message.answer("🎟 Введите промокод одним сообщением:", reply_markup=cancel_kb("menu:buy"))
    await call.answer()


@router.callback_query(F.data.startswith("promo:order:"))
async def cb_promo_order(call: CallbackQuery, state: FSMContext) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(promo_order=order_id)
    await state.set_state(PromoStates.enter)
    await call.message.answer("🎟 Введите промокод:", reply_markup=cancel_kb(f"order:show:{order_id}"))
    await call.answer()


@router.callback_query(F.data.startswith("order:show:"))
async def cb_order_show(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext) -> None:
    await state.clear()
    order_id = int(call.data.rsplit(":", 1)[1])
    await _show_order(call, db, cfg, order_id)
    await call.answer()


@router.message(PromoStates.enter)
async def msg_promo(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    data = await state.get_data()
    order_id = data.get("promo_order")
    code = (message.text or "").strip().upper()
    await state.clear()

    if not order_id:
        plans = await db.active_plans()
        await message.answer("Промокод можно применить при оформлении заказа: выберите тариф и нажмите «🎟 Промокод».",
                             reply_markup=plans_kb(plans, cfg.currency))
        return

    order = await db.get_order(int(order_id))
    if order is None or order["status"] != "pending":
        await message.answer("Заказ уже неактуален.", reply_markup=back_kb())
        return

    valid, reason, promo = await db.promo_valid(code, message.from_user.id)
    if not valid or promo is None:
        await message.answer(f"❌ {reason}", reply_markup=back_kb(f"order:show:{order_id}"))
        return

    base = int(order["base_amount"])
    if promo["discount_type"] == "percent":
        discount = base * int(promo["value"]) // 100
    else:
        discount = min(int(promo["value"]), base)
    new_amount = max(1, base - discount)
    await db.execute(
        "UPDATE orders SET amount = ?, discount = ?, promo_code = ? WHERE id = ?",
        (new_amount, discount, code, int(order_id)),
    )
    await db.apply_promo(code, message.from_user.id)
    await message.answer(
        f"✅ Промокод <code>{esc(code)}</code> применён!\n"
        f"Скидка: <b>{price(discount, cfg.currency)}</b>, к оплате: <b>{price(new_amount, cfg.currency)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ К оплате", callback_data=f"order:show:{order_id}")
        ]]),
    )
    await notify_admins(message.bot, cfg,
                        f"🎟 Промокод <code>{esc(code)}</code> применён пользователем "
                        f"<code>{message.from_user.id}</code> (заказ #{order_id})")


# ---------------------------------------------------------------------------
# Оплата
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, db: Database, cfg: Config, bot: Bot, yk=None) -> None:
    parts = call.data.split(":")
    action, order_id = parts[1], int(parts[2])
    order = await db.get_order(order_id)
    if order is None or order["status"] != "pending":
        await call.answer("Заказ уже неактуален.", show_alert=True)
        return
    amount = int(order["amount"])

    if action == "check":
        if not order["payment_id"] or yk is None:
            await call.answer("Платёж не найден — выберите способ оплаты заново.", show_alert=True)
            return
        paid = await yk.is_paid(order["payment_id"], amount)
        if paid:
            try:
                await complete_order(bot, cfg, db, order_id, payment_id=order["payment_id"])
            except PanelError as exc:
                await call.answer(f"Оплата прошла, но выдача задержалась: {exc}", show_alert=True)
                return
            await call.message.edit_text("✅ Оплата получена! Ключ отправлен отдельным сообщением.")
        else:
            await call.answer("Платёж пока не подтверждён. Оплатите по ссылке или подождите пару минут.",
                              show_alert=True)
        return

    if action == "yookassa":
        if yk is None:
            await call.answer("Оплата картой временно недоступна, выберите другой способ.", show_alert=True)
            return
        try:
            payment = await yk.create_payment(
                amount_rub=amount,
                description=f"VPN {order['plan_code']} (заказ #{order_id})",
                metadata={"order_id": order_id, "user_id": call.from_user.id},
            )
        except PaymentError as exc:
            log.error("ЮKassa: %s", exc)
            await call.answer("Не удалось создать платёж, попробуйте позже.", show_alert=True)
            await notify_admins(bot, cfg, f"⚠️ Ошибка ЮKassa по заказу #{order_id}: {esc(exc)}")
            return
        await db.execute("UPDATE orders SET method = 'yookassa', payment_id = ? WHERE id = ?",
                         (payment["id"], order_id))
        url = (payment.get("confirmation") or {}).get("confirmation_url", "")
        text = (
            f"💳 <b>Оплата заказа #{order_id}</b>\n\n"
            f"Сумма: <b>{price(amount, cfg.currency)}</b>\n"
            "Нажмите «Перейти к оплате» — откроется страница ЮKassa (карта, СБП, SberPay).\n\n"
            "После оплаты вернитесь в бот и нажмите «🔄 Проверить оплату». "
            "Если включены вебхуки — доступ выдастся автоматически."
        )
        await call.message.edit_text(text, reply_markup=checkout_kb(url, order_id) if url
                                     else back_kb(f"order:show:{order_id}"))
        await call.answer("Счёт создан")
        return

    if action == "stars":
        stars = cfg.stars_price(amount)
        await call.message.edit_text(
            f"⭐️ <b>Оплата звёздами</b>\n\nСумма: <b>{stars} ⭐️</b> "
            f"(≈ {price(amount, cfg.currency)})",
        )
        await create_stars_invoice(
            bot, chat_id=call.from_user.id, order_id=order_id,
            title=f"VPN {order['plan_code']}",
            description=f"Подписка {order['plan_code']} — заказ #{order_id}",
            stars_amount=stars,
        )
        await db.execute("UPDATE orders SET method = 'stars' WHERE id = ?", (order_id,))
        await call.answer()
        return

    if action == "balance":
        user = await db.get_user(call.from_user.id)
        balance = int(user["balance"]) if user else 0
        if balance < amount:
            await call.answer(f"Недостаточно средств: на балансе {price(balance, cfg.currency)}",
                              show_alert=True)
            return
        await db.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?",
                         (amount, call.from_user.id))
        await db.execute("UPDATE orders SET method = 'balance' WHERE id = ?", (order_id,))
        try:
            await complete_order(bot, cfg, db, order_id)
        except PanelError as exc:
            await db.add_balance(call.from_user.id, amount, method="balance", kind="refund",
                                 note=f"возврат по заказу #{order_id}")
            await call.answer(f"Не получилось выдать ключ: {exc}", show_alert=True)
            return
        await call.message.edit_text("✅ Оплачено с баланса! Ключ отправлен отдельным сообщением.")
        await call.answer()
        return

    if action == "manual":
        await db.execute("UPDATE orders SET method = 'manual' WHERE id = ?", (order_id,))
        await call.message.edit_text(
            f"🧾 <b>Оплата заказа #{order_id}</b>\n\n"
            f"Сумма: <b>{price(amount, cfg.currency)}</b>\n\n"
            f"{esc(cfg.manual_payment_note)}\n\n"
            "После оплаты пришлите в поддержку чек или скриншот и номер заказа — "
            "администратор подтвердит платёж, и ключ придёт автоматически.",
            reply_markup=back_kb("menu:support", "🆘 Написать в поддержку"),
        )
        await call.answer("Заявка создана")
        await notify_admins(
            bot, cfg,
            f"🧾 <b>Ручная оплата</b>\nЗаказ #{order_id}\n"
            f"Пользователь: <code>{call.from_user.id}</code> (@{call.from_user.username or '—'})\n"
            f"Сумма: {price(amount, cfg.currency)}",
        )
        return

    await call.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    payload = query.invoice_payload or ""
    order_id = int(payload.split(":", 1)[1]) if payload.startswith("order:") else 0
    order = await db.get_order(order_id) if order_id else None
    if order is None or order["status"] != "pending":
        await query.answer(ok=False, error_message="Заказ не найден или уже оплачен.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, db: Database, cfg: Config, bot: Bot) -> None:
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    order_id = int(payload.split(":", 1)[1]) if payload.startswith("order:") else 0
    if not order_id:
        await message.answer("Оплата получена, но заказ не найден. Напишите в поддержку.")
        return
    await db.execute("UPDATE orders SET method = 'stars' WHERE id = ?", (order_id,))
    try:
        await complete_order(bot, cfg, db, order_id, payment_id=payment.telegram_payment_charge_id)
    except PanelError as exc:
        await message.answer(
            "✅ Оплата получена, но выдача ключа задержалась — администратор уже уведомлён.",
        )
        await notify_admins(bot, cfg, f"🚨 Оплата звёздами, выдача не удалась: заказ #{order_id}, {esc(exc)}")
        return
    await message.answer(f"⭐️ Спасибо за оплату {payment.total_amount} ⭐️! Ключ отправлен выше.")


# ---------------------------------------------------------------------------
# Мои ключи
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_MY_KEYS)
async def msg_my_keys(message: Message, db: Database, cfg: Config) -> None:
    subs = await db.user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("У вас пока нет подписок 🙂", reply_markup=back_kb("menu:buy", "🛒 Купить VPN"))
        return
    text = "🔑 <b>Мои подписки</b>\n\nВыберите подписку, чтобы посмотреть конфиг, QR-код и инструкцию."
    await message.answer(text, reply_markup=keys_kb(subs))


@router.callback_query(F.data.startswith("sub:view:"))
async def cb_sub_view(call: CallbackQuery, db: Database, cfg: Config) -> None:
    sub_id = int(call.data.rsplit(":", 1)[1])
    sub = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return
    link = sub["sub_url"] or sub["vpn_key"] or ""
    text = (
        f"🔑 <b>Подписка #{sub_id}</b>\n\n"
        f"Тариф: <b>{esc(sub['plan_code'])}</b>\n"
        f"Статус: {STATUS_RU.get(sub['status'], sub['status'])}\n"
        f"Действует до: <b>{fmt_dt(sub['expires_at'])}</b> (осталось {human_left(sub['expires_at'])})\n"
        f"Устройств: {sub['devices']}, трафик: "
        f"{'безлимит' if not sub['traffic_gb'] else str(sub['traffic_gb']) + ' ГБ'}\n"
        f"Панель: {sub['panel']}\n\n"
        f"🔗 <code>{esc(link)}</code>"
    )
    markup = subscription_kb(sub_id, sub["sub_url"], sub["vpn_key"] or "", cfg.support_username)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("sub:copy:"))
async def cb_sub_copy(call: CallbackQuery, db: Database) -> None:
    sub_id = int(call.data.rsplit(":", 1)[1])
    sub = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return
    link = sub["sub_url"] or sub["vpn_key"] or ""
    await call.message.answer(f"<code>{esc(link)}</code>")
    await call.answer("Скопировано 👆")


@router.callback_query(F.data.startswith("sub:qr:"))
async def cb_sub_qr(call: CallbackQuery, db: Database) -> None:
    from aiogram.types import BufferedInputFile

    sub_id = int(call.data.rsplit(":", 1)[1])
    sub = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return
    link = sub["sub_url"] or sub["vpn_key"] or ""
    png = make_qr_png(link)
    if png is None:
        await call.answer("Генерация QR недоступна (не установлен модуль qrcode).", show_alert=True)
        return
    await call.message.answer_photo(BufferedInputFile(png, filename="vpn-qr.png"),
                                    caption="📷 Отсканируйте QR-код в приложении VPN")
    await call.answer()


@router.callback_query(F.data.startswith("sub:howto:"))
async def cb_sub_howto(call: CallbackQuery, db: Database) -> None:
    sub_id = int(call.data.rsplit(":", 1)[1])
    sub = await db.fetchone("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if sub is None or int(sub["user_id"]) != call.from_user.id:
        await call.answer("Подписка не найдена.", show_alert=True)
        return
    await call.message.answer(apps_instructions(sub["vpn_key"] or "", sub["sub_url"], int(sub["devices"])))
    await call.answer()


@router.callback_query(F.data.startswith("sub:renew:"))
async def cb_sub_renew(call: CallbackQuery, db: Database, cfg: Config) -> None:
    await _show_plans(call, db, cfg)
    await call.answer("При продлении конфиг останется тем же 👍")


# ---------------------------------------------------------------------------
# Пробный период
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_TRIAL)
async def msg_trial(message: Message, db: Database, cfg: Config, bot: Bot) -> None:
    if not rt.trial_enabled:
        await message.answer("Пробный период сейчас недоступен.")
        return
    user = await db.get_user(message.from_user.id)
    if user is not None and user["trial_used"]:
        await message.answer("🎁 Пробный период уже был использован — доступен только платный тариф.",
                             reply_markup=back_kb("menu:buy", "🛒 Купить VPN"))
        return
    active = await db.active_subscription(message.from_user.id)
    if active is not None:
        await message.answer("У вас уже есть активная подписка 🙂", reply_markup=back_kb("menu:main"))
        return
    try:
        await grant_subscription(bot, cfg, db, message.from_user.id, "trial")
    except PanelError as exc:
        await message.answer(f"⚠️ Не удалось выдать пробный доступ: {esc(exc)}\nНапишите в поддержку.",
                             reply_markup=back_kb("menu:support", "🆘 Поддержка"))
        await notify_admins(bot, cfg, f"⚠️ Пробный доступ не выдан <code>{message.from_user.id}</code>: {esc(exc)}")
        return
    await db.set_user_field(message.from_user.id, "trial_used", 1)
    await notify_admins(bot, cfg, f"🎁 Пробный период выдан <code>{message.from_user.id}</code>")


# ---------------------------------------------------------------------------
# Профиль и баланс
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_PROFILE)
async def msg_profile(message: Message, db: Database, cfg: Config) -> None:
    user = await db.get_user(message.from_user.id)
    sub = await db.active_subscription(message.from_user.id)
    orders = await db.user_orders(message.from_user.id, limit=100)
    paid = sum(1 for o in orders if o["status"] == "paid")
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"💰 Баланс: <b>{price(int(user['balance']) if user else 0, cfg.currency)}</b>\n"
        f"🛒 Покупок: <b>{paid}</b>\n"
    )
    if sub is not None:
        text += (
            f"\n🟢 Подписка: <b>{esc(sub['plan_code'])}</b>\n"
            f"⏳ Осталось: <b>{human_left(sub['expires_at'])}</b> (до {fmt_dt(sub['expires_at'])})\n"
        )
    else:
        text += "\n🔴 Активной подписки нет.\n"
    if user is not None and int(user["ref_earned"] or 0) > 0:
        text += f"\n🤝 Заработано на рефералах: <b>{price(int(user['ref_earned']), cfg.currency)}</b>"
    await message.answer(text, reply_markup=profile_kb(cfg.balance_enabled))


@router.callback_query(F.data == "menu:profile")
async def cb_profile(call: CallbackQuery, db: Database, cfg: Config) -> None:
    user = await db.get_user(call.from_user.id)
    sub = await db.active_subscription(call.from_user.id)
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{call.from_user.id}</code>\n"
        f"💰 Баланс: <b>{price(int(user['balance']) if user else 0, cfg.currency)}</b>\n"
    )
    if sub is not None:
        text += f"\n🟢 Подписка активна, осталось {human_left(sub['expires_at'])}"
    else:
        text += "\n🔴 Активной подписки нет."
    try:
        await call.message.edit_text(text, reply_markup=profile_kb(cfg.balance_enabled))
    except Exception:
        await call.message.answer(text, reply_markup=profile_kb(cfg.balance_enabled))
    await call.answer()


@router.callback_query(F.data == "orders:history")
async def cb_orders_history(call: CallbackQuery, db: Database, cfg: Config) -> None:
    orders = await db.user_orders(call.from_user.id, limit=15)
    if not orders:
        await call.answer("Заказов пока нет.", show_alert=True)
        return
    lines = ["📜 <b>Последние заказы</b>\n"]
    for order in orders:
        lines.append(
            f"#{order['id']} · {esc(order['plan_code'])} · {price(int(order['amount']), cfg.currency)} · "
            f"{STATUS_RU.get(order['status'], order['status'])} · {order['created_at'][:10]}"
        )
    await call.message.answer("\n".join(lines), reply_markup=back_kb("menu:profile", "◀️ В профиль"))
    await call.answer()


@router.callback_query(F.data == "topup:start")
async def cb_topup_start(call: CallbackQuery, cfg: Config) -> None:
    if not cfg.balance_enabled:
        await call.answer("Пополнение баланса отключено.", show_alert=True)
        return
    await call.message.edit_text(
        f"💰 <b>Пополнение баланса</b>\n\nВыберите сумму (от {price(cfg.min_topup, cfg.currency)}):",
        reply_markup=topup_amounts_kb(cfg.currency),
    )
    await call.answer()


@router.callback_query(F.data == "topup:custom")
async def cb_topup_custom(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    await state.set_state(TopupStates.custom_amount)
    await call.message.answer(
        f"✏️ Введите сумму пополнения в {cfg.currency} (от {cfg.min_topup} до {cfg.max_topup}):",
        reply_markup=cancel_kb("menu:profile"),
    )
    await call.answer()


@router.message(TopupStates.custom_amount)
async def msg_topup_custom(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    await state.clear()
    try:
        amount = int(float(raw))
    except ValueError:
        await message.answer("Нужно целое число, попробуйте снова.", reply_markup=cancel_kb("menu:profile"))
        return
    await _create_topup(message, db, cfg, amount)


@router.callback_query(F.data.startswith("topup:sum:"))
async def cb_topup_sum(call: CallbackQuery, db: Database, cfg: Config) -> None:
    amount = int(call.data.rsplit(":", 1)[1])
    await _create_topup(call.message, db, cfg, amount, user_id=call.from_user.id, answer=call)
    await call.answer()


async def _create_topup(target: Message, db: Database, cfg: Config, amount: int,
                        user_id: int | None = None, answer: CallbackQuery | None = None) -> None:
    uid = user_id or (target.from_user.id if target.from_user else 0)
    if amount < cfg.min_topup or amount > cfg.max_topup:
        text = f"Сумма должна быть от {cfg.min_topup} до {cfg.max_topup} {cfg.currency}."
        if answer:
            await answer.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    order_id = await db.create_order(user_id=uid, plan_code="topup", amount=amount, base_amount=amount,
                                     promo_code=None, method="none", kind="topup")
    available = [m for m in cfg.payment_methods if m in {"yookassa", "stars"}]
    if not available:
        await target.answer("Пополнение временно недоступно.")
        return
    text = (
        f"💰 Пополнение баланса на <b>{price(amount, cfg.currency)}</b>\n\nВыберите способ оплаты:"
    )
    markup = topup_methods_kb(order_id, available)
    if answer:
        try:
            await target.edit_text(text, reply_markup=markup)
        except Exception:
            await target.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Реферальная программа
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_REF)
async def msg_ref(message: Message, db: Database, cfg: Config, bot: Bot, bot_username: str = "") -> None:
    user = await db.get_user(message.from_user.id)
    invited = await db.scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (message.from_user.id,))
    link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}" if bot_username else "ссылка недоступна"
    earned = int(user["ref_earned"]) if user else 0
    text = (
        "🤝 <b>Приглашайте друзей и зарабатывайте</b>\n\n"
        f"Вы получаете <b>{rt.ref_percent}%</b> от каждой покупки приглашённого — на баланс, "
        "которым можно оплачивать свою подписку.\n\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"💰 Заработано: <b>{price(earned, cfg.currency)}</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>"
    )
    await message.answer(text, reply_markup=referral_kb(link, bot_username, rt.ref_percent))


@router.callback_query(F.data == "ref:stats")
async def cb_ref_stats(call: CallbackQuery, db: Database, cfg: Config) -> None:
    invited = await db.fetchall(
        "SELECT telegram_id, username, full_name, created_at FROM users WHERE referrer_id = ? "
        "ORDER BY created_at DESC LIMIT 20",
        (call.from_user.id,),
    )
    paid = await db.scalar(
        "SELECT COUNT(*) FROM orders o JOIN users u ON u.telegram_id = o.user_id "
        "WHERE u.referrer_id = ? AND o.status = 'paid'",
        (call.from_user.id,),
    )
    if not invited:
        await call.answer("Вы пока никого не пригласили.", show_alert=True)
        return
    lines = ["📊 <b>Ваши рефералы</b>\n"]
    for idx, row in enumerate(invited, 1):
        name = row["full_name"] or (f"@{row['username']}" if row["username"] else str(row["telegram_id"]))
        lines.append(f"{idx}. {esc(name)} — с {row['created_at'][:10]}")
    lines.append(f"\n💳 Из них оплатили: <b>{paid}</b>")
    await call.message.answer("\n".join(lines), reply_markup=back_kb("menu:main"))
    await call.answer()


# ---------------------------------------------------------------------------
# Поддержка
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_SUPPORT)
async def msg_support(message: Message, db: Database, cfg: Config) -> None:
    ticket = await db.open_ticket(message.from_user.id)
    text = (
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите проблему — ответим в этом чате. Обычно отвечаем в течение пары часов.\n"
        "Полезно сразу приложить: номер заказа, скриншот ошибки, название приложения."
    )
    if ticket is not None:
        text += f"\n\n💬 У вас есть открытое обращение #{ticket['id']}."
    await message.answer(text, reply_markup=support_kb(ticket is not None, cfg.support_username))


@router.callback_query(F.data == "menu:support")
async def cb_support(call: CallbackQuery, db: Database, cfg: Config) -> None:
    ticket = await db.open_ticket(call.from_user.id)
    text = (
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите проблему — ответим в этом чате.\n"
    )
    if ticket is not None:
        text += f"\n💬 Открытое обращение #{ticket['id']}."
    try:
        await call.message.edit_text(text, reply_markup=support_kb(ticket is not None, cfg.support_username))
    except Exception:
        await call.message.answer(text, reply_markup=support_kb(ticket is not None, cfg.support_username))
    await call.answer()


@router.callback_query(F.data == "support:faq")
async def cb_faq(call: CallbackQuery) -> None:
    await call.message.answer(FAQ_TEXT, reply_markup=back_kb("menu:support", "◀️ В поддержку"))
    await call.answer()


@router.callback_query(F.data == "support:new")
async def cb_support_new(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    ticket = await db.open_ticket(call.from_user.id)
    if ticket is not None:
        await call.message.answer(f"💬 Продолжаем обращение #{ticket['id']}. Напишите сообщение:")
        await state.update_data(ticket_id=ticket["id"])
        await state.set_state(TicketStates.reply)
        await call.answer()
        return
    await state.set_state(TicketStates.subject)
    await call.message.answer("✍️ Кратко опишите тему обращения (например: «Не работает на iPhone»):",
                              reply_markup=cancel_kb("menu:support"))
    await call.answer()


@router.message(TicketStates.subject)
async def msg_ticket_subject(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    subject = (message.text or "").strip()[:100]
    if not subject:
        await message.answer("Тема не может быть пустой, попробуйте снова.")
        return
    ticket_id = await db.create_ticket(message.from_user.id, subject)
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(TicketStates.message)
    await message.answer("📝 Теперь опишите проблему подробнее (можно приложить ссылку или скриншот):")
    await notify_admins(
        bot, cfg,
        f"🧾 <b>Новое обращение #{ticket_id}</b>\nПользователь: <code>{message.from_user.id}</code>\n"
        f"Тема: {esc(subject)}\n\nОтветить: раздел «🧾 Тикеты» в админке.",
    )


@router.message(TicketStates.message)
async def msg_ticket_message(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    await state.clear()
    if not ticket_id:
        await message.answer("Обращение не найдено, начните заново.", reply_markup=back_kb("menu:support"))
        return
    text = message.text or message.caption or "[вложение]"
    await db.add_ticket_message(ticket_id, False, text)
    await message.answer(f"✅ Сообщение отправлено в поддержку (обращение #{ticket_id}). Ответ придёт сюда же.",
                         reply_markup=main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))
    await notify_admins(
        bot, cfg,
        f"💬 <b>Обращение #{ticket_id}</b> — новое сообщение от <code>{message.from_user.id}</code>:\n{esc(text)}",
    )


@router.callback_query(F.data.startswith("ticket:reply:"))
async def cb_ticket_reply(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if ticket is None or int(ticket["user_id"]) != call.from_user.id:
        await call.answer("Обращение не найдено.", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(TicketStates.reply)
    await call.message.answer("✍️ Напишите сообщение в поддержку:")
    await call.answer()


@router.message(TicketStates.reply)
async def msg_ticket_reply(message: Message, state: FSMContext, db: Database, cfg: Config, bot: Bot) -> None:
    data = await state.get_data()
    ticket_id = int(data.get("ticket_id") or 0)
    await state.clear()
    text = message.text or message.caption or "[вложение]"
    await db.add_ticket_message(ticket_id, False, text)
    await message.answer("✅ Отправлено. Ответ придёт в этот чат.", reply_markup=main_menu(cfg.is_admin(message.from_user.id), rt.trial_enabled))
    await notify_admins(bot, cfg, f"💬 <b>Обращение #{ticket_id}</b>: сообщение от <code>{message.from_user.id}</code>\n{esc(text)}")


@router.callback_query(F.data.startswith("ticket:close:"))
async def cb_ticket_close(call: CallbackQuery, db: Database) -> None:
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if ticket is None or int(ticket["user_id"]) != call.from_user.id:
        await call.answer("Обращение не найдено.", show_alert=True)
        return
    await db.close_ticket(ticket_id)
    await call.message.edit_text(f"✅ Обращение #{ticket_id} закрыто. Спасибо!")
    await call.answer()


# ---------------------------------------------------------------------------
# Помощь и правовые документы
# ---------------------------------------------------------------------------
@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def msg_help(message: Message, cfg: Config) -> None:
    await message.answer(FAQ_TEXT, reply_markup=back_kb("menu:main"))


@router.message(Command("terms"))
async def cmd_terms(message: Message, cfg: Config) -> None:
    text = cfg.terms_text or (
        "📄 <b>Условия использования</b>\n\n"
        "1. Сервис предоставляется «как есть» для личного использования.\n"
        "2. Запрещено использовать сервис для незаконных действий, спама, атак и рассылок.\n"
        "3. При нарушении правил доступ отключается без возврата средств.\n"
        "4. Один конфиг — до указанного в тарифе количества устройств.\n"
        "5. Возврат средств — по решению поддержки в течение 3 дней с момента оплаты."
    )
    await message.answer(text, reply_markup=back_kb("menu:main"))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, cfg: Config) -> None:
    text = cfg.privacy_text or (
        "🔒 <b>Политика конфиденциальности</b>\n\n"
        "• Мы храним только Telegram ID, username и данные о заказах — для выдачи доступа и поддержки.\n"
        "• Данные не передаются третьим лицам, кроме платёжного провайдера (для проведения оплаты).\n"
        "• Трафик пользователя не логируется.\n"
        "• Удалить свои данные можно, обратившись в поддержку."
    )
    await message.answer(text, reply_markup=back_kb("menu:main"))


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_denied(call: CallbackQuery) -> None:
    """Сюда попадают админ-кнопки, нажатые обычным пользователем (в т.ч. из старого сообщения)."""
    await call.answer("⛔️ Недостаточно прав.", show_alert=True)
