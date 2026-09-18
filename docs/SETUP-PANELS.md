# Настройка выдачи ключей: пул, 3x-ui, Marzban

Режим выбирается переменной `VPN_PANEL` в `.env` (`pool`, `xui`, `marzban`).
Переключение не требует изменений кода — достаточно поменять значение и перезапустить бота:

```bash
sudo nano /opt/df-vpn-bot/.env      # VPN_PANEL=xui
sudo systemctl restart dfvpnbot
```

Проверить связь с панелью можно из бота: `/admin` → 🧪 Проверить панель.

---

## Режим 1. `pool` — пул готовых конфигов

Самый простой вариант: бот отдаёт клиенту заранее загруженный конфиг.

1. `/admin` → 🔑 Ключи → ➕ Добавить ключи.
2. Выберите тариф (или «Для любого тарифа»).
3. Вставьте ключи/ссылки — по одному в строке, можно пачкой:

```
vless://uuid@server:443?type=tcp&security=reality&pbk=...#user-1
vless://uuid2@server:443?type=tcp&security=reality&pbk=...#user-2
```

Особенности:
- ключи, привязанные к тарифу, выдаются именно на этот тариф; «универсальные» — на любой (в том числе на пробный период);
- если пул пуст, покупка завершится сообщением об ошибке, а админам придёт уведомление;
- 🔑 Ключи → 📋 Список пула — посмотреть, что свободно, а что выдано; 🗑 Очистить использованные — почистить историю.

---

## Режим 2. `xui` — панель 3x-ui

Бот создаёт клиента через API панели и сам собирает ссылку (`vless`, `vmess`, `trojan`; `reality`, `tls`, `ws`, `grpc`, `httpupgrade`).

### Что заполнить в `.env`

| Переменная | Пример | Комментарий |
|---|---|---|
| `XUI_URL` | `http://127.0.0.1:2053` или `http://1.2.3.4:2053/secretpath` | Полный адрес панели с web-base-path, если он включён |
| `XUI_USERNAME` / `XUI_PASSWORD` | `admin` / `…` | Логин в панель |
| `XUI_API_TOKEN` | `…` | Альтернатива логину/паролю: Настройки → Безопасность → API Token (предпочтительно) |
| `XUI_INBOUND_ID` | `1` | ID inbound'а, к которому добавлять клиентов |
| `XUI_PUBLIC_HOST` | `vpn.example.com` | Адрес, который попадёт в конфиг клиента (если отличается от адреса панели) |
| `XUI_SUB_BASE` | `https://sub.example.com/sub` | Если в панели включена выдача подписок — бот дополнительно отдаст ссылку подписки |

### Как узнать `XUI_INBOUND_ID`

- в веб-панели: Inbounds → номер слева от inbound'а;
- через API: `GET {XUI_URL}/panel/api/inbounds/list` (нужна сессия или токен).

### Рекомендации

- Панель обычно слушает локальный интерфейс (`127.0.0.1:2053`) — бот на том же сервере достучится без открытия порта наружу. Если панель на другом сервере, поставьте её за HTTPS-прокси.
- В настройках inbound'а клиенты должны использовать `reality`/`tls` — ссылка строится из `streamSettings`, поэтому любые изменения в панели сразу отражаются в конфигах клиентов.
- Продление: бот обновляет `expiryTime` существующего клиента — конфиг у пользователя не меняется. Истечение — `enable=false` (если `AUTO_DISABLE_EXPIRED=true`).
- Лимиты тарифа переносятся в панель: `limitIp` = число устройств, `totalGB` = трафик.

---

## Режим 3. `marzban` — панель Marzban

Бот получает токен (`POST /api/admin/token`), создаёт пользователя (`POST /api/user`), отдаёт ссылку подписки и первый конфиг, а при продлении обновляет `expire` и снимает блокировку.

### Что заполнить в `.env`

| Переменная | Пример |
|---|---|
| `MARZBAN_URL` | `https://panel.example.com` |
| `MARZBAN_USERNAME` / `MARZBAN_PASSWORD` | `admin` / `…` |
| `MARZBAN_PROXIES_JSON` | `{"vless": {"flow": "xtls-rprx-vision"}}` |
| `MARZBAN_INBOUNDS_JSON` | `{"vless": ["VLESS TCP REALITY"]}` |
| `MARZBAN_VERIFY_SSL` | `true` (поставьте `false` только для самоподписанного сертификата) |

Имена inbound'ов должны точно совпадать с теми, что созданы в Marzban (вкладка Inbounds).
Формат `proxies`/`inbounds` — как в [документации Marzban](https://github.com/Gozargah/Marzban).

### Рекомендации

- Токен админа живёт ограниченное время — бот автоматически переполучает его при 401.
- Пользователю в панели создаётся аккаунт `tg<id>-<тариф>-<время>`, примечание содержит Telegram ID.
- Продление аккуратно прибавляется к текущей дате окончания (не «сжигает» остаток).

---

## Свой вариант выдачи

Добавьте класс в `app/services/panels.py`:

```python
class MyPanel(BasePanel):
    name = "mypanel"

    async def create_client(self, *, user_id, days, devices, traffic_gb, plan_code) -> IssuedKey:
        ...  # создать клиента, вернуть IssuedKey(vpn_key=..., sub_url=..., panel=self.name, panel_username=...)

    async def extend_client(self, *, panel_username: str, days: int) -> bool: ...
    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool: ...
    async def delete_client(self, *, panel_username: str) -> bool: ...
    async def health(self) -> tuple[bool, str]: ...
```

И зарегистрируйте его в `get_panel()` — остальная логика (оплата, продления, уведомления) заработает без изменений.
