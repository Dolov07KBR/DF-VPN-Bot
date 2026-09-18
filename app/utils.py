"""Вспомогательные функции: форматирование, QR-коды, работа со временем."""

from __future__ import annotations

import html
import io
from datetime import datetime, timedelta
from typing import Iterable

from app.db import from_iso, utcnow

MONTHS_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}

STATUS_RU = {
    "pending": "⏳ ожидает оплаты",
    "paid": "✅ оплачен",
    "canceled": "❌ отменён",
    "refunded": "↩️ возврат",
    "active": "🟢 активна",
    "expired": "🔴 истекла",
    "disabled": "⚪️ отключена",
}

METHOD_RU = {
    "yookassa": "карта/СБП (ЮKassa)",
    "stars": "Telegram Stars",
    "balance": "баланс",
    "manual": "вручную",
    "referral": "реферальный бонус",
}


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def fmt_dt(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    moment = from_iso(value) if isinstance(value, str) else value
    return f"{moment.day:02d} {MONTHS_RU[moment.month]} {moment.year}"


def fmt_dt_full(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    moment = from_iso(value) if isinstance(value, str) else value
    return f"{moment.day:02d} {MONTHS_RU[moment.month]} {moment.year}, {moment:%H:%M} МСК".replace(
        "МСК", "UTC"
    )


def human_left(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    moment = from_iso(value) if isinstance(value, str) else value
    delta = moment - utcnow()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "истёк"
    days, rem = divmod(total_seconds, 86400)
    hours, _ = divmod(rem, 3600)
    if days:
        return f"{days} д. {hours} ч."
    minutes = rem // 60
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def human_bytes(size: int) -> str:
    step = 1024.0
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < step or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.2f} {unit}"
        value /= step
    return f"{value:.2f} ТБ"


def human_gb(gb: int) -> str:
    return "безлимит" if not gb else f"{gb} ГБ"


def price(value: int, currency: str = "₽") -> str:
    return f"{value:,} {currency}".replace(",", " ")


def make_qr_png(data: str) -> bytes | None:
    """QR-код конфигурации в PNG. Тихо возвращает None, если qrcode не установлен."""
    try:
        import qrcode  # type: ignore
    except Exception:  # pragma: no cover
        return None
    img = qrcode.make(data, box_size=8, border=2)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def pick_link(sub_row) -> str:
    """Лучшая ссылка для выдачи клиенту: подписка, иначе прямой конфиг."""
    sub_url = sub_row["sub_url"] if "sub_url" in sub_row.keys() else None
    return sub_url or (sub_row["vpn_key"] or "")


def apps_instructions(link: str, sub_url: str | None, devices: int) -> str:
    """Инструкция «как подключиться» с ссылками на популярные клиенты."""
    best = sub_url or link
    return (
        "📲 <b>Как подключиться</b>\n\n"
        f"<b>1.</b> Скопируйте ссылку:\n<code>{esc(best)}</code>\n\n"
        "<b>2.</b> Установите приложение:\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=com.v2ray.ang\">v2rayNG</a> — Android\n"
        "• <a href=\"https://apps.apple.com/app/streisand/id6450534064\">Streisand</a> / "
        "<a href=\"https://apps.apple.com/app/shadowrocket/id932747118\">Shadowrocket</a> — iOS\n"
        "• <a href=\"https://github.com/hiddify/hiddify-next/releases\">Hiddify</a> — Windows / macOS / Android\n"
        "• <a href=\"https://github.com/MatsuriDayo/nekoray/releases\">NekoRay</a> / "
        "<a href=\"https://github.com/MatsuriDayo/NekoBoxForAndroid/releases\">NekoBox</a> — десктоп / Android\n\n"
        "<b>3.</b> В приложении выберите «Импорт из буфера обмена» (или «Добавить по ссылке»).\n"
        "<b>4.</b> Нажмите «Подключиться» и проверьте доступ к сайтам.\n\n"
        f"👥 Лимит устройств по тарифу: <b>{devices}</b>\n"
        "🔁 Ссылку можно использовать на всех своих устройствах (в рамках лимита)."
    )


def support_hint(support_username: str) -> str:
    return f"Поддержка: @{support_username}" if support_username else ""


def now_plus(days: int) -> datetime:
    return utcnow() + timedelta(days=days)


def time_left_hours(value: str | datetime) -> float:
    moment = from_iso(value) if isinstance(value, str) else value
    return (moment - utcnow()).total_seconds() / 3600


def chunks(items: Iterable, size: int):
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
