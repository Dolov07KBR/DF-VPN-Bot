"""Клавиатуры бота: главное меню, покупка, профиль, поддержка, админ-панель."""

from __future__ import annotations

from typing import Iterable, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.utils import human_gb, price

BTN_BUY = "🛒 Купить VPN"
BTN_MY_KEYS = "🔑 Мои ключи"
BTN_PROFILE = "👤 Профиль"
BTN_REF = "🤝 Пригласить друга"
BTN_SUPPORT = "🆘 Поддержка"
BTN_TRIAL = "🎁 Пробный период"
BTN_HELP = "ℹ️ Помощь"
BTN_ADMIN = "⚙️ Админка"


def main_menu(is_admin: bool = False, trial_enabled: bool = False) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_MY_KEYS)],
        [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_REF)],
    ]
    last_row = [KeyboardButton(text=BTN_SUPPORT)]
    if trial_enabled:
        last_row.insert(0, KeyboardButton(text=BTN_TRIAL))
    rows.append(last_row)
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите действие")


def cancel_kb(callback: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data=callback)]])


def back_kb(callback: str = "menu:main", label: str = "◀️ В меню") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=callback)]])


def plans_kb(plans: Sequence, currency: str = "₽", prefix: str = "buy") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        details = f"{plan['days']} дн · {human_gb(plan['traffic_gb'])} · {plan['devices']} устр."
        builder.button(
            text=f"{plan['title']} — {price(plan['price'], currency)} ({details})",
            callback_data=f"{prefix}:{plan['code']}",
        )
    builder.button(text="🎟 Ввести промокод", callback_data="promo:enter")
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def payment_methods_kb(available: Iterable[str], order_id: int, balance: int | None = None,
                       currency: str = "₽", allow_balance: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for method in available:
        if method == "yookassa":
            builder.button(text="💳 Карта / СБП", callback_data=f"pay:yookassa:{order_id}")
        elif method == "stars":
            builder.button(text="⭐️ Telegram Stars", callback_data=f"pay:stars:{order_id}")
        elif method == "balance":
            label = "💰 С баланса"
            if balance is not None:
                label += f" ({price(balance, currency)})"
            if allow_balance:
                builder.button(text=label, callback_data=f"pay:balance:{order_id}")
        elif method == "manual":
            builder.button(text="🧾 Оплачу вручную", callback_data=f"pay:manual:{order_id}")
    builder.button(text="🎟 Промокод", callback_data=f"promo:order:{order_id}")
    builder.button(text="❌ Отменить заказ", callback_data=f"order:cancel:{order_id}")
    builder.adjust(1)
    return builder.as_markup()


def profile_kb(balance_enabled: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if balance_enabled:
        builder.button(text="➕ Пополнить баланс", callback_data="topup:start")
    builder.button(text="📜 История заказов", callback_data="orders:history")
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def subscription_kb(sub_id: int, sub_url: str | None, vpn_key: str, support_username: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if sub_url:
        builder.button(text="📄 Ссылка подписки", url=sub_url)
        builder.button(text="🔗 Скопировать конфиг", callback_data=f"sub:copy:{sub_id}")
    else:
        builder.button(text="🔗 Скопировать конфиг", callback_data=f"sub:copy:{sub_id}")
    builder.button(text="📷 QR-код", callback_data=f"sub:qr:{sub_id}")
    builder.button(text="📲 Как подключиться", callback_data=f"sub:howto:{sub_id}")
    builder.button(text="🔄 Продлить", callback_data=f"sub:renew:{sub_id}")
    if support_username:
        builder.button(text="🆘 Не работает", url=f"https://t.me/{support_username}")
    builder.adjust(1)
    return builder.as_markup()


def keys_kb(subs: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subs:
        state = "🟢" if sub["status"] == "active" else "🔴"
        builder.button(text=f"{state} {sub['plan_code']} · до {sub['expires_at'][:10]}",
                       callback_data=f"sub:view:{sub['id']}")
    builder.button(text="➕ Купить ещё", callback_data="menu:buy")
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def referral_kb(ref_link: str, bot_username: str, ref_percent: int) -> InlineKeyboardMarkup:
    share = (
        "https://t.me/share/url?url="
        + ref_link.replace("https://", "").replace("&", "%26").replace("?", "%3F").replace("=", "%3D").replace("/", "%2F")
        + "&text=" + "Быстрый и стабильный VPN"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="📤 Поделиться ссылкой", url=share)
    builder.button(text="📊 Статистика", callback_data="ref:stats")
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def topup_amounts_kb(currency: str = "₽") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (200, 500, 1000, 2000, 5000):
        builder.button(text=price(amount, currency), callback_data=f"topup:sum:{amount}")
    builder.button(text="✏️ Своя сумма", callback_data="topup:custom")
    builder.button(text="◀️ Назад", callback_data="menu:profile")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def topup_methods_kb(order_id: int, available: Iterable[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for method in available:
        if method == "yookassa":
            builder.button(text="💳 Карта / СБП", callback_data=f"pay:yookassa:{order_id}")
        elif method == "stars":
            builder.button(text="⭐️ Telegram Stars", callback_data=f"pay:stars:{order_id}")
    builder.button(text="❌ Отмена", callback_data="menu:profile")
    builder.adjust(1)
    return builder.as_markup()


def checkout_kb(confirmation_url: str, order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Перейти к оплате", url=confirmation_url)
    builder.button(text="🔄 Проверить оплату", callback_data=f"pay:check:{order_id}")
    builder.button(text="❌ Отменить заказ", callback_data=f"order:cancel:{order_id}")
    builder.adjust(1)
    return builder.as_markup()


def support_kb(has_open_ticket: bool, support_username: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Новое обращение" if not has_open_ticket else "💬 Продолжить обращение",
                   callback_data="support:new")
    if support_username:
        builder.button(text="✈️ Написать в Telegram", url=f"https://t.me/{support_username}")
    builder.button(text="📖 FAQ", callback_data="support:faq")
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"ticket:reply:{ticket_id}")
    builder.button(text="✅ Закрыть", callback_data=f"ticket:close:{ticket_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


def admin_kb(stats: dict, tickets_open: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    slots = [
        ("💰 Заказы", "adm:orders", stats.get("orders_pending", 0)),
        ("🔑 Ключи", "adm:keys", 0),
        ("👥 Пользователи", "adm:users", 0),
        ("📊 Статистика", "adm:stats", 0),
        ("🎟 Промокоды", "adm:promos", 0),
        ("📢 Рассылка", "adm:broadcast", 0),
        ("🧾 Тикеты", "adm:tickets", tickets_open),
        ("🎛 Настройки", "adm:settings", 0),
    ]
    for text, data, badge in slots:
        if badge:
            text = f"{text} ({badge})"
        builder.button(text=text, callback_data=data)
    builder.button(text="💾 Бэкап базы", callback_data="adm:backup")
    builder.button(text="🧪 Проверить панель", callback_data="adm:paneltest")
    builder.adjust(2, 2, 2, 2, 1, 1)
    return builder.as_markup()


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить оплату", callback_data=f"adm:order:approve:{order_id}")
    builder.button(text="❌ Отклонить", callback_data=f"adm:order:reject:{order_id}")
    builder.adjust(2)
    return builder.as_markup()


def admin_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"adm:ticket:reply:{ticket_id}")
    builder.button(text="✅ Закрыть", callback_data=f"adm:ticket:close:{ticket_id}")
    builder.adjust(2)
    return builder.as_markup()


def admin_keys_kb(pool: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить ключи", callback_data="adm:keys:add")
    builder.button(text="📋 Список пула", callback_data="adm:keys:list:0")
    builder.button(text=f"🗑 Очистить использованные ({pool.get('used', 0)})", callback_data="adm:keys:clear")
    builder.button(text="🧪 Проверить панель", callback_data="adm:paneltest")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_users_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if total_pages > 1:
        if page > 0:
            builder.button(text="◀️", callback_data=f"adm:users:page:{page - 1}")
        builder.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
        if page < total_pages - 1:
            builder.button(text="▶️", callback_data=f"adm:users:page:{page + 1}")
    builder.button(text="🔍 Найти по ID", callback_data="adm:users:find")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(3, 1, 1)
    return builder.as_markup()


def admin_user_kb(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Написать", callback_data=f"adm:user:msg:{user_id}")
    builder.button(text="💵 Изменить баланс", callback_data=f"adm:user:balance:{user_id}")
    builder.button(text="🎁 Выдать подписку", callback_data=f"adm:user:grant:{user_id}")
    builder.button(text="🔓 Разблокировать" if is_banned else "🚫 Заблокировать",
                   callback_data=f"adm:user:{'unban' if is_banned else 'ban'}:{user_id}")
    builder.button(text="🗑 Удалить устройства", callback_data=f"adm:user:wipe:{user_id}")
    builder.button(text="◀️ Назад", callback_data="adm:users:page:0")
    builder.adjust(1)
    return builder.as_markup()


def admin_promos_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать промокод", callback_data="adm:promo:new")
    builder.button(text="📋 Список промокодов", callback_data="adm:promo:list")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def plans_admin_kb(plans: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        state = "🟢" if plan["is_active"] else "🔴"
        builder.button(text=f"{state} {plan['title']} — {plan['price']}₽ / {plan['days']} дн.",
                       callback_data=f"adm:plan:{plan['code']}")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def plan_edit_kb(code: str, is_active: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Цена", callback_data=f"adm:plan:price:{code}")
    builder.button(text="📅 Дней", callback_data=f"adm:plan:days:{code}")
    builder.button(text="📶 Трафик", callback_data=f"adm:plan:traffic:{code}")
    builder.button(text="👥 Устройств", callback_data=f"adm:plan:devices:{code}")
    builder.button(text="🔴 Скрыть" if is_active else "🟢 Показать",
                   callback_data=f"adm:plan:toggle:{code}")
    builder.button(text="◀️ Назад", callback_data="adm:settings:plans")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def broadcast_audience_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Всем", callback_data="adm:bcast:all")
    builder.button(text="🟢 С активной подпиской", callback_data="adm:bcast:subscribers")
    builder.button(text="⚪️ Без подписки", callback_data="adm:bcast:no_subscription")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def settings_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Тарифы", callback_data="adm:settings:plans")
    builder.button(text="⚙️ Тариф пробного периода", callback_data="adm:settings:trial")
    builder.button(text="🤝 Реферальный процент", callback_data="adm:settings:ref")
    builder.button(text="ℹ️ О боте / версия", callback_data="adm:about")
    builder.button(text="◀️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def subscription_check_kb(channels: tuple[str, ...]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, channel in enumerate(channels):
        if channel.startswith("https://") or channel.startswith("http://"):
            builder.button(text=f"📢 Канал {idx + 1}", url=channel)
        else:
            username = channel.lstrip("@")
            builder.button(text=f"📢 Канал {idx + 1}", url=f"https://t.me/{username}")
    builder.button(text="✅ Я подписался", callback_data="check:subscription")
    builder.adjust(1)
    return builder.as_markup()


def webapp_kb(url: str, label: str = "🌐 Открыть кабинет") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))]]
    )
