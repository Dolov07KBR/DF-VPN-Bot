#!/usr/bin/env bash
# =============================================================================
#  DF VPN Bot — установщик на VPS (Debian/Ubuntu/CentOS/Fedora/Arch/Alpine).
#  Автор: @Dolov07KBR (https://github.com/Dolov07KBR)
#
#  Установка одной командой:
#     bash <(curl -fsSL https://raw.githubusercontent.com/Dolov07KBR/DF-VPN-Bot/main/install.sh)
#
#  Что делает:
#    1. ставит зависимости (python3, venv, git, sqlite3, curl);
#    2. скачивает проект в /opt/df-vpn-bot;
#    3. создаёт виртуальное окружение и ставит пакеты;
#    4. в интерактивном режиме спрашивает токен, админов, панель и оплату → пишет .env;
#    5. создаёт systemd-сервис, включает автозапуск и стартует бота.
#
#  Дополнительные режимы:
#     --update          обновить код и перезапустить сервис
#     --uninstall       удалить сервис (данные остаются в /opt/df-vpn-bot/data)
#     --yes             неинтерактивно: значения берутся из переменных окружения
#     --dir PATH        каталог установки (по умолчанию /opt/df-vpn-bot)
#     --no-service      не создавать systemd-сервис (запуск вручную)
#     --help            справка
# =============================================================================

set -Eeuo pipefail

REPO_OWNER="Dolov07KBR"
REPO_NAME="DF-VPN-Bot"
REPO_BRANCH="main"
TARBALL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_BRANCH}.tar.gz"

INSTALL_DIR="${DFVPN_DIR:-/opt/df-vpn-bot}"
SERVICE_NAME="dfvpnbot"
SERVICE_USER="dfvpn"
NONINTERACTIVE=0
CREATE_SERVICE=1
ACTION="install"

C_RESET="\033[0m"; C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"
C_RED="\033[31m"; C_BLUE="\033[36m"
say()  { printf "${C_BLUE}▸${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!${C_RESET} %s\n" "$*"; }
die()  { printf "${C_RED}✗ %s${C_RESET}\n" "$*" >&2; exit 1; }
title(){ printf "\n${C_BOLD}%s${C_RESET}\n" "$*"; }

usage() {
	# Печатаем только шапку-комментарий из начала файла
	awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
	exit 0
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--update)     ACTION="update"; shift ;;
		--uninstall)  ACTION="uninstall"; shift ;;
		--yes|-y)     NONINTERACTIVE=1; shift ;;
		--no-service) CREATE_SERVICE=0; shift ;;
		--dir)        INSTALL_DIR="$2"; shift 2 ;;
		--help|-h)    usage ;;
		*) die "Неизвестный параметр: $1 (см. --help)" ;;
	esac
done

[[ $EUID -eq 0 ]] || die "Запустите установщик от root: sudo bash install.sh"

# ----------------------------------------------------------------------------
# Определение дистрибутива и установка зависимостей
# ----------------------------------------------------------------------------
detect_pm() {
	if command -v apt-get >/dev/null 2>&1; then echo apt
	elif command -v dnf >/dev/null 2>&1; then echo dnf
	elif command -v yum >/dev/null 2>&1; then echo yum
	elif command -v pacman >/dev/null 2>&1; then echo pacman
	elif command -v apk >/dev/null 2>&1; then echo apk
	else echo unknown; fi
}

install_deps() {
	local pm; pm="$(detect_pm)"
	title "1/6 Установка системных зависимостей (${pm})"
	export DEBIAN_FRONTEND=noninteractive
	case "$pm" in
		apt)
			apt-get update -qq
			apt-get install -y -qq python3 python3-venv python3-pip git curl sqlite3 ca-certificates >/dev/null
			;;
		dnf|yum)
			$pm install -y -q python3 python3-pip git curl sqlite ca-certificates >/dev/null
			;;
		pacman)
			pacman -Sy --noconfirm python python-pip git curl sqlite >/dev/null
			;;
		apk)
			apk add --no-cache python3 py3-pip py3-virtualenv git curl sqlite >/dev/null
			;;
		*)
			warn "Неизвестный пакетный менеджер. Убедитесь, что установлены: python3, pip, venv, git, curl."
			;;
	esac
	command -v python3 >/dev/null || die "python3 не найден после установки зависимостей"
	ok "$(python3 --version) готов"
}

# ----------------------------------------------------------------------------
# Загрузка кода
# ----------------------------------------------------------------------------
fetch_sources() {
	title "2/6 Загрузка кода в ${INSTALL_DIR}"
	mkdir -p "$INSTALL_DIR"

	if [[ -f "$(dirname "$0")/bot.py" ]]; then
		say "Найден локальный код рядом с установщиком — копирую его"
		tar -cf - -C "$(dirname "$0")" --exclude='.git' --exclude='.venv' --exclude='data' . \
			| tar -xf - -C "$INSTALL_DIR"
		ok "Код скопирован"
		return
	fi

	say "Скачиваю архив ${REPO_OWNER}/${REPO_NAME}@${REPO_BRANCH}"
	local tmp; tmp="$(mktemp -d)"
	curl -fsSL "$TARBALL" -o "$tmp/src.tar.gz" || die "Не удалось скачать архив. Проверьте доступ в интернет."
	tar -xzf "$tmp/src.tar.gz" -C "$tmp"
	rm -rf "${INSTALL_DIR:?}"/*
	cp -a "$tmp/${REPO_NAME}-${REPO_BRANCH}/." "$INSTALL_DIR/"
	rm -rf "$tmp"
	ok "Код загружен"
}

# ----------------------------------------------------------------------------
# Виртуальное окружение
# ----------------------------------------------------------------------------
setup_venv() {
	title "3/6 Python-окружение"
	if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
		python3 -m venv "${INSTALL_DIR}/.venv" || die "Не удалось создать venv (нужен пакет python3-venv)"
	fi
	"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip setuptools wheel
	"${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt" \
		|| die "Не удалось установить зависимости"
	ok "Зависимости установлены ($("${INSTALL_DIR}/.venv/bin/python" --version))"
}

# ----------------------------------------------------------------------------
# Диалог настройки → .env
# ----------------------------------------------------------------------------
ask() {             # ask <переменная> <вопрос> [значение по умолчанию]
	local __var="$1" __prompt="$2" __default="${3:-}" __answer=""
	if [[ $NONINTERACTIVE -eq 1 ]]; then
		__answer="${!__var:-$__default}"
	else
		if [[ -n "$__default" ]]; then
			read -r -p "$(printf "${C_BOLD}%s${C_RESET} [%s]: " "$__prompt" "$__default")" __answer || true
			__answer="${__answer:-$__default}"
		else
			read -r -p "$(printf "${C_BOLD}%s${C_RESET}: " "$__prompt")" __answer || true
		fi
	fi
	printf -v "$__var" '%s' "$__answer"
}

confirm() {         # confirm <вопрос> <y|n по умолчанию>
	local prompt="$1" default="${2:-n}" answer=""
	if [[ $NONINTERACTIVE -eq 1 ]]; then
		[[ "$default" == "y" ]]
		return
	fi
	read -r -p "$(printf "${C_BOLD}%s${C_RESET} [%s]: " "$prompt" "$default")" answer || true
	answer="${answer:-$default}"
	[[ "$answer" =~ ^([YyДд]|yes|да)$ ]]
}

write_env() {
	title "4/6 Настройка (.env)"

	local BOT_TOKEN="${BOT_TOKEN:-}" ADMIN_IDS="${ADMIN_IDS:-}"
	if [[ -z "$BOT_TOKEN" ]]; then
		ask BOT_TOKEN "Токен бота от @BotFather"
	fi
	[[ "$BOT_TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]] || die "Похоже, токен указан неверно (ожидается вида 123456:AA...)"
	if [[ -z "$ADMIN_IDS" ]]; then
		ask ADMIN_IDS "Ваш Telegram ID (админ, можно несколько через запятую)"
	fi
	[[ "$ADMIN_IDS" =~ ^[0-9]+([,[:space:]]*[0-9]+)*$ ]] || die "ADMIN_IDS должен содержать числовые Telegram ID"

	ask BOT_NAME "Название сервиса" "${BOT_NAME:-DF VPN}"
	ask SUPPORT_USERNAME "Ник поддержки без @ (необязательно)" "${SUPPORT_USERNAME:-support}"
	ask REQUIRED_CHANNELS "Обязательные каналы, например @ch1,@ch2 (пусто = без проверки)" "${REQUIRED_CHANNELS:-}"

	ask PAYMENT_METHODS "Способы оплаты (yookassa,stars,balance,manual)" "${PAYMENT_METHODS:-yookassa,stars,balance,manual}"

	local YOOKASSA_SHOP_ID="${YOOKASSA_SHOP_ID:-}" YOOKASSA_SECRET_KEY="${YOOKASSA_SECRET_KEY:-}"
	if [[ "$PAYMENT_METHODS" == *yookassa* ]]; then
		ask YOOKASSA_SHOP_ID "ЮKassa shopId (Enter — пропустить и отключить карты)" "$YOOKASSA_SHOP_ID"
		if [[ -n "$YOOKASSA_SHOP_ID" ]]; then
			ask YOOKASSA_SECRET_KEY "ЮKassa секретный ключ" "$YOOKASSA_SECRET_KEY"
		else
			warn "ЮKassa пропущена — способ оплаты yookassa отключён"
			PAYMENT_METHODS="$(echo "$PAYMENT_METHODS" | tr ',' '\n' | grep -v '^yookassa$' | paste -sd, -)"
		fi
	fi

	ask TRIAL_ENABLED "Включить пробный период? (true/false)" "${TRIAL_ENABLED:-false}"
	ask TRIAL_DAYS "Дней пробного периода" "${TRIAL_DAYS:-3}"
	ask REF_PERCENT "Реферальный процент (0–50)" "${REF_PERCENT:-15}"

	echo
	say "Как бот будет выдавать VPN-ключи?"
	echo "   1) pool    — пул готовых конфигов (ключи загрузите в админке)"
	echo "   2) xui     — панель 3x-ui (клиенты создаются автоматически)"
	echo "   3) marzban — панель Marzban"
	local panel_choice="${VPN_PANEL:-}"
	if [[ "$panel_choice" == "pool" || "$panel_choice" == "xui" || "$panel_choice" == "marzban" ]]; then
		:
	else
		ask panel_choice "Выберите 1/2/3" "1"
	fi
	case "$panel_choice" in
		2|xui)     VPN_PANEL="xui" ;;
		3|marzban) VPN_PANEL="marzban" ;;
		*)         VPN_PANEL="pool" ;;
	esac

	local XUI_URL="${XUI_URL:-}" XUI_USERNAME="${XUI_USERNAME:-admin}" XUI_PASSWORD="${XUI_PASSWORD:-}"
	local XUI_INBOUND_ID="${XUI_INBOUND_ID:-1}" XUI_PUBLIC_HOST="${XUI_PUBLIC_HOST:-}"
	local MARZBAN_URL="${MARZBAN_URL:-}" MARZBAN_USERNAME="${MARZBAN_USERNAME:-admin}" MARZBAN_PASSWORD="${MARZBAN_PASSWORD:-}"
	if [[ "$VPN_PANEL" == "xui" ]]; then
		echo
		say "Настройки панели 3x-ui (адрес панели, например http://127.0.0.1:2053 или http://1.2.3.4:2053/secretpath)"
		ask XUI_URL "XUI_URL" "$XUI_URL"
		ask XUI_USERNAME "Логин панели" "$XUI_USERNAME"
		ask XUI_PASSWORD "Пароль панели" "$XUI_PASSWORD"
		ask XUI_INBOUND_ID "ID inbound'а для выдачи" "$XUI_INBOUND_ID"
		ask XUI_PUBLIC_HOST "Публичный адрес сервера для ссылок (домен/IP)" "$XUI_PUBLIC_HOST"
	elif [[ "$VPN_PANEL" == "marzban" ]]; then
		echo
		say "Настройки панели Marzban"
		ask MARZBAN_URL "MARZBAN_URL (например https://panel.example.com)" "$MARZBAN_URL"
		ask MARZBAN_USERNAME "Логин админа" "$MARZBAN_USERNAME"
		ask MARZBAN_PASSWORD "Пароль админа" "$MARZBAN_PASSWORD"
	fi

	local WEBHOOK_ENABLED="false" WEBHOOK_PUBLIC_URL="${WEBHOOK_PUBLIC_URL:-}" WEBHOOK_PORT="${WEBHOOK_PORT:-8080}"
	if [[ "$PAYMENT_METHODS" == *yookassa* ]] && [[ -n "${YOOKASSA_SHOP_ID:-}" ]]; then
		echo
		if confirm "Есть публичный HTTPS-адрес для вебхуков ЮKassa? (иначе бот будет сам проверять оплату раз в 3 минуты)" "${WEBHOOK_ENABLED:-n}"; then
			WEBHOOK_ENABLED="true"
			ask WEBHOOK_PUBLIC_URL "Публичный URL вебхука (https://домен/yookassa/webhook)" "$WEBHOOK_PUBLIC_URL"
			ask WEBHOOK_PORT "Локальный порт вебхука" "$WEBHOOK_PORT"
		fi
	fi

	local env_file="${INSTALL_DIR}/.env"
	umask 077
	cat > "$env_file" <<EOF
# Создано установщиком DF VPN Bot $(date -u +%Y-%m-%dT%H:%M:%SZ)
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
BOT_NAME=${BOT_NAME}
SUPPORT_USERNAME=${SUPPORT_USERNAME}
NEWS_CHANNEL_URL=${NEWS_CHANNEL_URL:-}
REQUIRED_CHANNELS=${REQUIRED_CHANNELS}

CURRENCY=${CURRENCY:-₽}
TRIAL_ENABLED=${TRIAL_ENABLED}
TRIAL_DAYS=${TRIAL_DAYS}
TRIAL_DEVICES=${TRIAL_DEVICES:-1}
TRIAL_TRAFFIC_GB=${TRIAL_TRAFFIC_GB:-10}
REF_PERCENT=${REF_PERCENT}
REF_WELCOME_BONUS=${REF_WELCOME_BONUS:-0}
REF_MIN_PAYOUT=${REF_MIN_PAYOUT:-100}
PAYMENT_METHODS=${PAYMENT_METHODS}

YOOKASSA_SHOP_ID=${YOOKASSA_SHOP_ID:-}
YOOKASSA_SECRET_KEY=${YOOKASSA_SECRET_KEY:-}
YOOKASSA_RETURN_URL=${YOOKASSA_RETURN_URL:-https://t.me}
YOOKASSA_TEST=${YOOKASSA_TEST:-false}
STARS_RATE=${STARS_RATE:-1.5}
MIN_TOPUP=${MIN_TOPUP:-100}
MAX_TOPUP=${MAX_TOPUP:-50000}
MANUAL_PAYMENT_NOTE=${MANUAL_PAYMENT_NOTE:-Реквизиты уточняйте в поддержке.}

WEBHOOK_ENABLED=${WEBHOOK_ENABLED}
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=${WEBHOOK_PORT}
WEBHOOK_PATH=${WEBHOOK_PATH:-/yookassa/webhook}
WEBHOOK_PUBLIC_URL=${WEBHOOK_PUBLIC_URL:-}

VPN_PANEL=${VPN_PANEL}
XUI_URL=${XUI_URL:-}
XUI_USERNAME=${XUI_USERNAME:-}
XUI_PASSWORD=${XUI_PASSWORD:-}
XUI_API_TOKEN=${XUI_API_TOKEN:-}
XUI_INBOUND_ID=${XUI_INBOUND_ID:-1}
XUI_PUBLIC_HOST=${XUI_PUBLIC_HOST:-}
XUI_SUB_BASE=${XUI_SUB_BASE:-}
MARZBAN_URL=${MARZBAN_URL:-}
MARZBAN_USERNAME=${MARZBAN_USERNAME:-}
MARZBAN_PASSWORD=${MARZBAN_PASSWORD:-}
MARZBAN_PROXIES_JSON=${MARZBAN_PROXIES_JSON:-{"vless": {"flow": "xtls-rprx-vision"}}}
MARZBAN_INBOUNDS_JSON=${MARZBAN_INBOUNDS_JSON:-{"vless": ["VLESS TCP REALITY"]}}
MARZBAN_VERIFY_SSL=${MARZBAN_VERIFY_SSL:-true}

DB_PATH=${INSTALL_DIR}/data/dfvpnbot.sqlite3
LOG_LEVEL=${LOG_LEVEL:-INFO}
LOG_FILE=${INSTALL_DIR}/data/bot.log
AUTO_DISABLE_EXPIRED=${AUTO_DISABLE_EXPIRED:-true}
NOTIFY_DAYS_BEFORE=${NOTIFY_DAYS_BEFORE:-3,1}
ORDERS_TTL_HOURS=${ORDERS_TTL_HOURS:-48}
THROTTLING_RATE=${THROTTLING_RATE:-0.6}
EOF
	chmod 600 "$env_file"
	ok "Конфигурация записана в ${env_file}"
}

# ----------------------------------------------------------------------------
# Проверка конфигурации запуском бота «на сухую»
# ----------------------------------------------------------------------------
preflight() {
	title "5/6 Проверка настроек"
	local output
	if output="$("${INSTALL_DIR}/.venv/bin/python" - <<'PY' 2>&1
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(".").resolve()))
from app.config import load_config, validate
cfg = load_config()
problems = validate(cfg)
if problems:
    print("ПРОБЛЕМЫ:")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("OK")
PY
	)"; then
		ok "Конфигурация корректна"
	else
		warn "Проверка сообщила о проблемах:"
		echo "$output" | sed 's/^/    /'
		warn "Бот может не запуститься — исправьте .env (${INSTALL_DIR}/.env)"
	fi
}

# ----------------------------------------------------------------------------
# systemd-сервис
# ----------------------------------------------------------------------------
install_service() {
	title "6/6 Служба systemd"
	if ! command -v systemctl >/dev/null 2>&1; then
		warn "systemd не найден — запустите бота вручную:"
		echo "    cd ${INSTALL_DIR} && .venv/bin/python bot.py"
		return
	fi

	id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null || true
	mkdir -p "${INSTALL_DIR}/data"
	chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
	chmod 600 "${INSTALL_DIR}/.env"

	cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=DF VPN Bot (Telegram bot for VPN subscriptions)
Documentation=https://github.com/${REPO_OWNER}/${REPO_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
# Базовое усиление безопасности
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

	systemctl daemon-reload
	systemctl enable --quiet "$SERVICE_NAME" 2>/dev/null || true
	systemctl restart "$SERVICE_NAME"
	sleep 4
	if systemctl is-active --quiet "$SERVICE_NAME"; then
		ok "Служба ${SERVICE_NAME} запущена"
	else
		warn "Служба не запустилась. Логи:"
		journalctl -u "$SERVICE_NAME" -n 25 --no-pager || true
	fi
}

# ----------------------------------------------------------------------------
# Режимы update / uninstall
# ----------------------------------------------------------------------------
do_update() {
	title "Обновление DF VPN Bot"
	[[ -d "$INSTALL_DIR" ]] || die "Каталог ${INSTALL_DIR} не найден — сначала установите бота."
	local tmp; tmp="$(mktemp -d)"
	curl -fsSL "$TARBALL" -o "$tmp/src.tar.gz" || die "Не удалось скачать обновление"
	tar -xzf "$tmp/src.tar.gz" -C "$tmp"
	# сохраняем .env и данные
	mv "${INSTALL_DIR}/.env" "${tmp}/env.keep" 2>/dev/null || true
	rm -rf "${INSTALL_DIR:?}"/*
	cp -a "$tmp/${REPO_NAME}-${REPO_BRANCH}/." "$INSTALL_DIR/"
	mv "${tmp}/env.keep" "${INSTALL_DIR}/.env" 2>/dev/null || true
	rm -rf "$tmp"
	"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade -r "${INSTALL_DIR}/requirements.txt"
	chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" 2>/dev/null || true
	if command -v systemctl >/dev/null 2>&1; then
		systemctl restart "$SERVICE_NAME"
		ok "Код обновлён, служба перезапущена"
	else
		ok "Код обновлён"
	fi
}

do_uninstall() {
	title "Удаление DF VPN Bot"
	confirm "Удалить службу ${SERVICE_NAME}? (данные останутся)" "n" || die "Отменено"
	if command -v systemctl >/dev/null 2>&1; then
		systemctl stop "$SERVICE_NAME" 2>/dev/null || true
		systemctl disable "$SERVICE_NAME" 2>/dev/null || true
		rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
		systemctl daemon-reload
	fi
	warn "Каталог ${INSTALL_DIR} не удалён (там .env и база). Удалить вручную при необходимости."
	ok "Служба удалена"
}

summary() {
	title "Готово!"
	cat <<EOF
  Каталог:        ${INSTALL_DIR}
  Конфигурация:   ${INSTALL_DIR}/.env
  База и логи:    ${INSTALL_DIR}/data/

  Управление службой:
    systemctl status ${SERVICE_NAME}
    systemctl restart ${SERVICE_NAME}
    journalctl -u ${SERVICE_NAME} -f

  Дальше в Telegram:
    1. Откройте бота и отправьте /start;
    2. Отправьте /admin — откроется админ-панель;
    3. Если панель выдачи = pool: Админка → 🔑 Ключи → ➕ Добавить ключи;
    4. Если ЮKassa: укажите HTTP-уведомление${WEBHOOK_PUBLIC_URL:+ ${WEBHOOK_PUBLIC_URL}} в кабинете ЮKassa.

  Обновление:  sudo bash ${INSTALL_DIR}/install.sh --update
  Удаление:    sudo bash ${INSTALL_DIR}/install.sh --uninstall
EOF
}

case "$ACTION" in
	update)    do_update ;;
	uninstall) do_uninstall ;;
	install)
		install_deps
		fetch_sources
		setup_venv
		write_env
		preflight
		[[ $CREATE_SERVICE -eq 1 ]] && install_service
		summary
		;;
esac
