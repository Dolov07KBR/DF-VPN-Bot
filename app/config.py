"""Конфигурация бота: чтение, разбор и валидация переменных окружения.

Все настройки берутся из .env (см. .env.example) либо из переменных окружения
(например, когда бот запущен как systemd-сервис с EnvironmentFile).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Мягкая загрузка .env: если установлен python-dotenv — используем его."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_file)
        return
    except Exception:  # pragma: no cover - fallback без зависимости
        pass
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "да"}


def _ints(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    result: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            continue
    return tuple(result) or default


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str
    admin_ids: tuple[int, ...]
    support_username: str
    news_channel_url: str
    required_channels: tuple[str, ...]
    bot_name: str

    # --- Тарифы / продажи ---
    currency: str
    trial_enabled: bool
    trial_days: int
    trial_devices: int
    trial_traffic_gb: int
    trial_panel_hint: str

    # --- Реферальная программа ---
    ref_percent: int
    ref_welcome_bonus: int
    ref_min_payout: int

    # --- Оплата ---
    payment_methods: tuple[str, ...]
    yookassa_shop_id: str
    yookassa_secret_key: str
    yookassa_return_url: str
    yookassa_test: bool
    stars_rate: float
    min_topup: int
    max_topup: int
    manual_payment_note: str

    # --- Панель выдачи ключей ---
    panel: str
    xui_url: str
    xui_username: str
    xui_password: str
    xui_api_token: str
    xui_inbound_id: int
    xui_public_host: str
    xui_sub_base: str
    marzban_url: str
    marzban_username: str
    marzban_password: str
    marzban_proxies_json: str
    marzban_inbounds_json: str
    marzban_verify_ssl: bool

    # --- Вебхуки ЮKassa ---
    webhook_enabled: bool
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_public_url: str

    # --- Прочее ---
    db_path: str
    log_level: str
    log_file: str
    auto_disable_expired: bool
    notify_days_before: tuple[int, ...]
    orders_ttl_hours: int
    throttling_rate: float
    terms_text: str
    privacy_text: str

    admins_only: bool = field(default=False)

    @property
    def yookassa_enabled(self) -> bool:
        return "yookassa" in self.payment_methods and bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def stars_enabled(self) -> bool:
        return "stars" in self.payment_methods

    @property
    def balance_enabled(self) -> bool:
        return "balance" in self.payment_methods

    @property
    def manual_enabled(self) -> bool:
        return "manual" in self.payment_methods

    def stars_price(self, amount_rub: int) -> int:
        """Перевод цены в рублях в количество Telegram Stars."""
        if self.stars_rate <= 0:
            return amount_rub
        return max(1, round(amount_rub / self.stars_rate))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_config() -> Config:
    cfg = Config(
        bot_token=_str("BOT_TOKEN"),
        admin_ids=_ints("ADMIN_IDS") or _ints("ADMIN_CHAT_ID"),
        support_username=_str("SUPPORT_USERNAME", "@support").lstrip("@"),
        news_channel_url=_str("NEWS_CHANNEL_URL"),
        required_channels=_list("REQUIRED_CHANNELS"),
        bot_name=_str("BOT_NAME", "DF VPN"),
        currency=_str("CURRENCY", "₽"),
        trial_enabled=_bool("TRIAL_ENABLED", False),
        trial_days=_int("TRIAL_DAYS", 3),
        trial_devices=_int("TRIAL_DEVICES", 1),
        trial_traffic_gb=_int("TRIAL_TRAFFIC_GB", 10),
        trial_panel_hint=_str("TRIAL_PANEL_HINT", "trial"),
        ref_percent=_int("REF_PERCENT", 15),
        ref_welcome_bonus=_int("REF_WELCOME_BONUS", 0),
        ref_min_payout=_int("REF_MIN_PAYOUT", 100),
        payment_methods=_list("PAYMENT_METHODS", ("yookassa", "stars", "balance", "manual")),
        yookassa_shop_id=_str("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_str("YOOKASSA_SECRET_KEY"),
        yookassa_return_url=_str("YOOKASSA_RETURN_URL", "https://t.me"),
        yookassa_test=_bool("YOOKASSA_TEST", False),
        stars_rate=_float("STARS_RATE", 1.5),
        min_topup=_int("MIN_TOPUP", 100),
        max_topup=_int("MAX_TOPUP", 50000),
        manual_payment_note=_str("MANUAL_PAYMENT_NOTE", "Реквизиты уточняйте у поддержки."),
        panel=_str("VPN_PANEL", "pool").lower(),
        xui_url=_str("XUI_URL").rstrip("/"),
        xui_username=_str("XUI_USERNAME", "admin"),
        xui_password=_str("XUI_PASSWORD"),
        xui_api_token=_str("XUI_API_TOKEN"),
        xui_inbound_id=_int("XUI_INBOUND_ID", 0),
        xui_public_host=_str("XUI_PUBLIC_HOST"),
        xui_sub_base=_str("XUI_SUB_BASE").rstrip("/"),
        marzban_url=_str("MARZBAN_URL").rstrip("/"),
        marzban_username=_str("MARZBAN_USERNAME", "admin"),
        marzban_password=_str("MARZBAN_PASSWORD"),
        marzban_proxies_json=_str("MARZBAN_PROXIES_JSON", '{"vless": {"flow": "xtls-rprx-vision"}}'),
        marzban_inbounds_json=_str("MARZBAN_INBOUNDS_JSON", '{"vless": ["VLESS TCP REALITY"]}'),
        marzban_verify_ssl=_bool("MARZBAN_VERIFY_SSL", True),
        webhook_enabled=_bool("WEBHOOK_ENABLED", False),
        webhook_host=_str("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_int("WEBHOOK_PORT", 8080),
        webhook_path=_str("WEBHOOK_PATH", "/yookassa/webhook"),
        webhook_public_url=_str("WEBHOOK_PUBLIC_URL"),
        db_path=_str("DB_PATH", str(BASE_DIR / "data" / "dfvpnbot.sqlite3")),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_file=_str("LOG_FILE", str(BASE_DIR / "data" / "bot.log")),
        auto_disable_expired=_bool("AUTO_DISABLE_EXPIRED", True),
        notify_days_before=tuple(sorted({d for d in _ints("NOTIFY_DAYS_BEFORE", (3, 1)) if d > 0}, reverse=True)),
        orders_ttl_hours=_int("ORDERS_TTL_HOURS", 48),
        throttling_rate=_float("THROTTLING_RATE", 0.6),
        terms_text=_str("TERMS_TEXT"),
        privacy_text=_str("PRIVACY_TEXT"),
    )
    return cfg


def validate(cfg: Config) -> list[str]:
    """Возвращает список проблем конфигурации (пустой список = всё в порядке)."""
    problems: list[str] = []
    if not cfg.bot_token:
        problems.append("BOT_TOKEN не задан — получите токен у @BotFather.")
    if not cfg.admin_ids:
        problems.append("ADMIN_IDS не задан — укажите свой Telegram ID (узнать: @userinfobot).")
    if cfg.panel not in {"pool", "xui", "marzban"}:
        problems.append(f"VPN_PANEL={cfg.panel!r} неизвестен. Доступно: pool, xui, marzban.")
    if cfg.panel == "xui":
        if not cfg.xui_url:
            problems.append("VPN_PANEL=xui, но не задан XUI_URL.")
        if not (cfg.xui_api_token or (cfg.xui_username and cfg.xui_password)):
            problems.append("VPN_PANEL=xui: нужен XUI_API_TOKEN или XUI_USERNAME/XUI_PASSWORD.")
    if cfg.panel == "marzban":
        if not cfg.marzban_url:
            problems.append("VPN_PANEL=marzban, но не задан MARZBAN_URL.")
        if not cfg.marzban_password:
            problems.append("VPN_PANEL=marzban: не задан MARZBAN_PASSWORD.")
    if "yookassa" in cfg.payment_methods and not cfg.yookassa_enabled:
        problems.append(
            "Способ оплаты yookassa включён, но YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы."
        )
    if not cfg.payment_methods:
        problems.append("PAYMENT_METHODS пуст — не осталось ни одного способа оплаты.")
    if cfg.webhook_enabled and not cfg.webhook_public_url:
        problems.append(
            "WEBHOOK_ENABLED=true, но WEBHOOK_PUBLIC_URL не задан — укажите публичный адрес "
            "вида https://bot.example.com/yookassa/webhook и пропишите его в кабинете ЮKassa."
        )
    return problems
