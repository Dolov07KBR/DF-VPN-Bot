"""Выдача VPN-ключей: единый интерфейс для трёх режимов.

* ``pool``    — готовые ключи/конфиги, заранее загруженные админом;
* ``xui``     — панель 3x-ui (создание клиента через API, ссылка собирается сама);
* ``marzban`` — панель Marzban (создание пользователя, ссылка подписки от панели).
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from app.config import Config
from app.db import Database, from_iso, to_iso, utcnow
from app.utils import now_plus

log = logging.getLogger(__name__)


class PanelError(RuntimeError):
    """Ошибка взаимодействия с панелью/пулом ключей."""


@dataclass
class IssuedKey:
    vpn_key: str
    sub_url: Optional[str]
    panel: str
    panel_username: str


class BasePanel(ABC):
    name = "base"

    @abstractmethod
    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey: ...

    async def extend_client(self, *, panel_username: str, days: int) -> bool:
        """Продлить подписку у существующего клиента. True — если продление применено в панели."""
        return False

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        return False

    async def delete_client(self, *, panel_username: str) -> bool:
        return False

    async def health(self) -> tuple[bool, str]:
        return True, "проверка не поддерживается"

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Пул заранее загруженных ключей
# ---------------------------------------------------------------------------
class PoolPanel(BasePanel):
    name = "pool"

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey:
        row = await self.db.take_key(plan_code, user_id)
        if row is None:
            raise PanelError(
                "Пул ключей пуст. Администратору нужно добавить конфиги: "
                "Админка → 🔑 Ключи → ➕ Добавить ключи."
            )
        return IssuedKey(
            vpn_key=row["content"],
            sub_url=None,
            panel=self.name,
            panel_username=f"pool:{row['id']}",
        )

    async def extend_client(self, *, panel_username: str, days: int) -> bool:
        # Ключ из пула остаётся тем же — продление живёт только в базе бота.
        return True

    async def health(self) -> tuple[bool, str]:
        stats = await self.db.pool_stats()
        return stats["free"] > 0, f"свободных ключей: {stats['free']} из {stats['total']}"


# ---------------------------------------------------------------------------
# 3x-ui
# ---------------------------------------------------------------------------
class XuiPanel(BasePanel):
    name = "xui"

    def __init__(self, cfg: Config) -> None:
        self.base = cfg.xui_url.rstrip("/")
        self.username = cfg.xui_username
        self.password = cfg.xui_password
        self.token = cfg.xui_api_token
        self.inbound_id = cfg.xui_inbound_id
        self.public_host = cfg.xui_public_host
        self.sub_base = cfg.xui_sub_base
        self._session: Optional[aiohttp.ClientSession] = None
        self._logged_in = False

    # --- инфраструктура -------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def _login(self, session: aiohttp.ClientSession) -> None:
        if self.token:
            self._logged_in = True
            return
        async with session.post(
            f"{self.base}/login",
            data={"username": self.username, "password": self.password},
            ssl=False,
        ) as resp:
            text = await resp.text()
        if resp.status >= 400 or '"success":true' not in text.replace(" ", ""):
            raise PanelError(f"Не удалось авторизоваться в 3x-ui (HTTP {resp.status})")
        self._logged_in = True

    async def _api(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        session = await self._get_session()
        if not self._logged_in:
            await self._login(session)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base}/panel/api{path}"
        kwargs: dict[str, Any] = {"headers": headers, "ssl": False}
        if payload is not None:
            kwargs["json"] = payload
        async with session.request(method, url, **kwargs) as resp:
            raw = await resp.text()
            if resp.status in (401, 403):
                self._logged_in = False
                raise PanelError("3x-ui: сессия истекла или нет прав (проверьте логин/пароль/токен).")
            if resp.status >= 400:
                raise PanelError(f"3x-ui вернул HTTP {resp.status}: {raw[:200]}")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise PanelError(f"3x-ui вернул не JSON: {raw[:200]}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise PanelError(f"3x-ui: {data.get('msg') or 'неизвестная ошибка'}")
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- работа с inbounds ----------------------------------------------
    async def _list_inbounds(self) -> list[dict]:
        data = await self._api("/inbounds/list")
        return list(data.get("obj") or [])

    async def _inbound(self) -> dict:
        inbounds = await self._list_inbounds()
        if not inbounds:
            raise PanelError("3x-ui: в панели нет ни одного inbound.")
        if self.inbound_id:
            for inbound in inbounds:
                if int(inbound.get("id") or 0) == self.inbound_id:
                    return inbound
            raise PanelError(f"3x-ui: inbound id={self.inbound_id} не найден.")
        return inbounds[0]

    @staticmethod
    def _clients(inbound: dict) -> list[dict]:
        try:
            settings = inbound.get("settings") or "{}"
            return list(json.loads(settings).get("clients") or [])
        except (json.JSONDecodeError, TypeError):
            return []

    async def _find_client(self, email: str) -> tuple[dict, dict] | None:
        inbound = await self._inbound()
        for client in self._clients(inbound):
            if client.get("email") == email:
                return inbound, client
        return None

    # --- публичный API ----------------------------------------------------
    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey:
        inbound = await self._inbound()
        protocol = str(inbound.get("protocol") or "vless")
        settings = self._clients(inbound)
        template = settings[0] if settings else {}
        email = f"tg{user_id}_{plan_code}_{int(time.time())}"
        client_uuid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)
        expiry_ms = int(now_plus(days).timestamp() * 1000)
        client = {
            "id": client_uuid if protocol != "trojan" else secrets.token_urlsafe(16),
            "flow": template.get("flow", "xtls-rprx-vision" if protocol == "vless" else ""),
            "email": email,
            "limitIp": max(1, devices),
            "totalGB": traffic_gb * 1024**3,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": str(user_id),
            "subId": sub_id,
            "reset": 0,
        }
        inner = {"clients": [client]}
        if protocol == "vmess":
            inner["clients"][0]["alterId"] = 0
            inner["clients"][0].pop("flow", None)
        await self._api(
            "/inbounds/addClient",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps(inner)},
        )
        link = _build_link(inbound, client, self.public_host or _host_from_url(self.base))
        sub_url = f"{self.sub_base}/{sub_id}" if self.sub_base else None
        return IssuedKey(vpn_key=link, sub_url=sub_url, panel=self.name, panel_username=email)

    async def extend_client(self, *, panel_username: str, days: int) -> bool:
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        expiry_ms = int(now_plus(days).timestamp() * 1000) if days > 0 else 0
        client["expiryTime"] = expiry_ms
        client["enable"] = True
        await self._api(
            f"/inbounds/updateClient/{client['id']}",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps({"clients": [client]})},
        )
        return True

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        client["enable"] = bool(enabled)
        await self._api(
            f"/inbounds/updateClient/{client['id']}",
            method="POST",
            payload={"id": inbound["id"], "settings": json.dumps({"clients": [client]})},
        )
        return True

    async def delete_client(self, *, panel_username: str) -> bool:
        found = await self._find_client(panel_username)
        if not found:
            return False
        inbound, client = found
        await self._api(f"/inbounds/delClient/{inbound['id']}/{client['id']}", method="POST")
        return True

    async def health(self) -> tuple[bool, str]:
        try:
            inbound = await self._inbound()
        except PanelError as exc:
            return False, str(exc)
        clients = len(self._clients(inbound))
        return True, (
            f"inbound #{inbound['id']} ({inbound.get('protocol')}:{inbound.get('port')}), "
            f"клиентов в панели: {clients}"
        )


def _host_from_url(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].split(":")[0]
    except IndexError:
        return url


def _build_link(inbound: dict, client: dict, host: str) -> str:
    """Собирает ссылку конфигурации (vless/vmess/trojan) из настроек inbound."""
    protocol = str(inbound.get("protocol") or "vless").lower()
    port = inbound.get("port")
    remark = f"{client.get('email', 'vpn')}"
    try:
        stream = json.loads(inbound.get("streamSettings") or "{}")
    except json.JSONDecodeError:
        stream = {}
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    params: dict[str, str] = {"type": network, "security": security}

    if security == "reality":
        reality = stream.get("realitySettings", {})
        params["pbk"] = reality.get("settings", {}).get("publicKey", "")
        params["fp"] = reality.get("settings", {}).get("fingerprint", "chrome")
        names = reality.get("serverNames") or []
        if names:
            params["sni"] = names[0]
        short_ids = reality.get("shortIds") or []
        if short_ids:
            params["sid"] = short_ids[0]
        params["spx"] = "%2F"
    elif security in {"tls", "xtls"}:
        tls = stream.get("tlsSettings", {})
        names = tls.get("serverName") or ""
        if isinstance(names, str) and names:
            params["sni"] = names
        elif isinstance(names, list) and names:
            params["sni"] = names[0]
        alpn = tls.get("alpn") or []
        if alpn:
            params["alpn"] = ",".join(alpn)
        params["fp"] = tls.get("settings", {}).get("fingerprint", "chrome")

    if network == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        ws_host = (ws.get("headers") or {}).get("Host")
        if ws_host:
            params["host"] = ws_host
    elif network == "grpc":
        grpc = stream.get("grpcSettings", {})
        params["serviceName"] = grpc.get("serviceName", "")
    elif network == "tcp":
        header = (stream.get("tcpSettings") or {}).get("header", {})
        if header.get("type") == "http":
            params["headerType"] = "http"
    elif network in {"httpupgrade", "xhttp"}:
        section = stream.get(f"{network}Settings", {})
        params["path"] = section.get("path", "/")
        if section.get("host"):
            params["host"] = section["host"]

    query = "&".join(f"{k}={quote(str(v), safe='%')}" for k, v in params.items() if v not in (None, ""))

    if protocol == "vless":
        if client.get("flow"):
            query += f"&flow={client['flow']}"
        return f"vless://{client['id']}@{host}:{port}?{query}#{quote(remark)}"

    if protocol == "trojan":
        return f"trojan://{quote(str(client['id']))}@{host}:{port}?{query}#{quote(remark)}"

    if protocol == "vmess":
        payload = {
            "v": "2",
            "ps": remark,
            "add": host,
            "port": str(port),
            "id": client["id"],
            "aid": str(client.get("alterId", 0)),
            "scy": "auto",
            "net": network,
            "type": "none",
            "host": params.get("host", ""),
            "path": params.get("path", ""),
            "tls": "tls" if security in {"tls", "xtls"} else "",
            "sni": params.get("sni", ""),
            "fp": params.get("fp", ""),
        }
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        return f"vmess://{encoded}"

    raise PanelError(f"3x-ui: протокол {protocol} не поддерживается для генерации ссылки.")


# ---------------------------------------------------------------------------
# Marzban
# ---------------------------------------------------------------------------
class MarzbanPanel(BasePanel):
    name = "marzban"

    def __init__(self, cfg: Config) -> None:
        self.base = cfg.marzban_url.rstrip("/")
        self.username = cfg.marzban_username
        self.password = cfg.marzban_password
        self.proxies = json.loads(cfg.marzban_proxies_json)
        self.inbounds = json.loads(cfg.marzban_inbounds_json)
        self.verify_ssl = cfg.marzban_verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._token: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        return self._session

    async def _auth(self) -> str:
        if self._token:
            return self._token
        session = await self._get_session()
        async with session.post(
            f"{self.base}/api/admin/token",
            data={"username": self.username, "password": self.password},
            ssl=self.verify_ssl,
        ) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                raise PanelError(f"Marzban: не удалось получить токен (HTTP {resp.status}): {raw[:200]}")
            data = json.loads(raw)
        token = data.get("access_token")
        if not token:
            raise PanelError("Marzban: ответ без access_token.")
        self._token = token
        return token

    async def _api(self, path: str, *, method: str = "GET", payload: dict | None = None,
                   retry: bool = True) -> dict:
        session = await self._get_session()
        token = await self._auth()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        kwargs: dict[str, Any] = {"headers": headers, "ssl": self.verify_ssl}
        if payload is not None:
            kwargs["json"] = payload
        async with session.request(method, f"{self.base}/api{path}", **kwargs) as resp:
            raw = await resp.text()
            if resp.status in (401, 403) and retry:
                self._token = None
                return await self._api(path, method=method, payload=payload, retry=False)
            if resp.status == 409:
                raise PanelError("Marzban: пользователь с таким именем уже существует.")
            if resp.status >= 400:
                raise PanelError(f"Marzban вернул HTTP {resp.status}: {raw[:200]}")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PanelError(f"Marzban вернул не JSON: {raw[:200]}") from exc

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_client(self, *, user_id: int, days: int, devices: int, traffic_gb: int,
                            plan_code: str) -> IssuedKey:
        username = f"tg{user_id}-{plan_code}-{int(time.time())}"
        expire_ts = int(now_plus(days).timestamp())
        payload: dict[str, Any] = {
            "username": username,
            "proxies": self.proxies,
            "inbounds": self.inbounds,
            "expire": expire_ts,
            "data_limit": traffic_gb * 1024**3,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": f"Telegram ID {user_id}",
        }
        data = await self._api("/user", method="POST", payload=payload)
        links = data.get("links") or []
        sub_url = data.get("subscription_url")
        key = links[0] if links else (sub_url or "")
        if not key:
            raise PanelError("Marzban: пользователь создан, но панель не вернула ссылку.")
        return IssuedKey(vpn_key=key, sub_url=sub_url, panel=self.name, panel_username=username)

    async def extend_client(self, *, panel_username: str, days: int) -> bool:
        try:
            data = await self._api(f"/user/{panel_username}")
        except PanelError:
            return False
        current_expire = int(data.get("expire") or 0)
        base = max(current_expire, int(utcnow().timestamp())) if current_expire else int(utcnow().timestamp())
        new_expire = base + days * 86400
        await self._api(
            f"/user/{panel_username}",
            method="PUT",
            payload={"expire": new_expire, "status": "active"},
        )
        return True

    async def set_enabled(self, *, panel_username: str, enabled: bool) -> bool:
        try:
            await self._api(
                f"/user/{panel_username}",
                method="PUT",
                payload={"status": "active" if enabled else "disabled"},
            )
            return True
        except PanelError:
            return False

    async def delete_client(self, *, panel_username: str) -> bool:
        try:
            await self._api(f"/user/{panel_username}", method="DELETE")
            return True
        except PanelError:
            return False

    async def health(self) -> tuple[bool, str]:
        try:
            data = await self._api("/users?limit=1")
        except PanelError as exc:
            return False, str(exc)
        total = data.get("total", "?")
        return True, f"пользователей в панели: {total}"


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------
_panel: BasePanel | None = None


def get_panel(cfg: Config, db: Database) -> BasePanel:
    global _panel
    if _panel is None:
        if cfg.panel == "xui":
            _panel = XuiPanel(cfg)
        elif cfg.panel == "marzban":
            _panel = MarzbanPanel(cfg)
        else:
            _panel = PoolPanel(db)
    return _panel


async def close_panel() -> None:
    global _panel
    if _panel is not None:
        await _panel.close()
        _panel = None


def subscription_payload(sub_row) -> dict:
    """Приводит строку подписки к словарю, нужному панелям."""
    return {
        "panel_username": sub_row["panel_username"],
        "expires_at": from_iso(sub_row["expires_at"]) if isinstance(sub_row["expires_at"], str)
        else sub_row["expires_at"],
    }


__all__ = [
    "BasePanel", "PoolPanel", "XuiPanel", "MarzbanPanel", "PanelError", "IssuedKey",
    "get_panel", "close_panel", "to_iso",
]
