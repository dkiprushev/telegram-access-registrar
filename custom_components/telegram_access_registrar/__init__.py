"""Telegram Access Registrar integration."""
from __future__ import annotations

from typing import Any
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import TelegramApi
from .const import (
    CONF_ADMIN_CHAT_ID,
    CONF_BOT_TOKEN,
    DOMAIN,
    EVENT_ACCESS_REQUESTED,
    SERVICE_APPROVE,
    SERVICE_LIST_REQUESTS,
    SERVICE_ISSUE_AKUVOX_KEY,
    SERVICE_REJECT,
    SERVICE_SEND_MESSAGE,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import RegistrarCoordinator
from .registry import AccessRegistry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start one independent Telegram long-polling registration bot."""
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}")
    registry = AccessRegistry(store)
    api = TelegramApi(async_get_clientsession(hass), entry.data[CONF_BOT_TOKEN])
    def request_access(data: dict[str, Any]) -> None:
        """Make /access immediately issue the deliberately fixed guest key."""
        recipient = data.get("recipient", {})
        username = recipient.get("username") if isinstance(recipient, dict) else None
        if not username:
            return
        hass.async_create_task(
            hass.services.async_call(
                DOMAIN,
                SERVICE_ISSUE_AKUVOX_KEY,
                {
                    "username": username,
                    "relay_names": ["Вхід 25-Е", "Ліфт пасажирський 25-Е"],
                    "duration_hours": 48,
                },
                blocking=True,
            )
        )

    coordinator = RegistrarCoordinator(
        api,
        registry,
        int(entry.data[CONF_ADMIN_CHAT_ID]),
        lambda data: (hass.bus.async_fire(EVENT_ACCESS_REQUESTED, data), request_access(data)),
    )
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "registry": registry,
        "coordinator": coordinator,
        "api": api,  # Kept runtime-only; never persisted.
    }
    _async_register_services(hass)
    return True


def _runtime(hass: HomeAssistant) -> dict[str, Any]:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ValueError("Telegram Access Registrar is not configured")
    return hass.data[DOMAIN][entries[0].entry_id]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_LIST_REQUESTS):
        return

    async def approve(call: ServiceCall) -> dict[str, Any]:
        identity = await _runtime(hass)["registry"].async_approve(call.data["chat_id"])
        return {"approved": identity is not None, "recipient": identity}

    async def reject(call: ServiceCall) -> dict[str, Any]:
        identity = await _runtime(hass)["registry"].async_reject(call.data["chat_id"])
        return {"rejected": identity is not None, "recipient": identity}

    async def list_requests(call: ServiceCall) -> dict[str, Any]:
        return _runtime(hass)["registry"].snapshot()

    async def send_message(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        recipient = runtime["registry"].get_approved_by_username(call.data["username"])
        if recipient is None:
            raise ValueError("Recipient is not approved or has no Telegram username")
        await runtime["api"].async_send_message(recipient["chat_id"], call.data["message"])

    async def issue_akuvox_key(call: ServiceCall) -> dict[str, Any]:
        """Issue an Akuvox key and deliver its PIN plus QR to an approved user."""
        runtime = _runtime(hass)
        recipient = runtime["registry"].get_approved_by_username(call.data["username"])
        if recipient is None:
            raise ValueError("Recipient is not approved")
        coordinators = list(hass.data.get("akuvox", {}).values())
        if len(coordinators) != 1:
            raise ValueError("Exactly one configured Akuvox account is required")
        client = coordinators[0].client
        requested = {name.casefold() for name in call.data["relay_names"]}
        relays = client.get_panel_data().get("relays", [])
        selected = [r for r in relays if str(r.get("name", "")).casefold() in requested or str(r.get("door_name", "")).casefold() in requested]
        if len(selected) != len(requested):
            raise ValueError("One or more requested Akuvox relays were not found")
        now = dt_util.now().replace(tzinfo=None)
        end = now + timedelta(hours=int(call.data.get("duration_hours", 48)))
        description = f"Telegram @{call.data['username'].lstrip('@')}"
        await client.async_create_panel_temp_key(description, now.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), int(call.data.get("uses", 100)), [{"mac": str(r["mac"]), "relay": str(r["relay_id"])} for r in selected])
        keys = client.get_panel_data().get("temporary_keys", [])
        key = next((item for item in reversed(keys) if item.get("description") == description), None)
        if not key or not key.get("key_id"):
            raise ValueError("Akuvox created the key but did not return it")
        pin = next((str(key[k]) for k in ("pin", "code", "password", "KeyCode") if key.get(k)), "see QR code")
        await runtime["api"].async_send_message(recipient["chat_id"], f"Доступ активен до {end:%d.%m %H:%M}. PIN: {pin}")
        await runtime["api"].async_send_photo(recipient["chat_id"], await client.async_get_panel_temp_key_qr(str(key["key_id"])), "QR-код доступа: вход и пассажирский лифт")
        return {"key_id": str(key["key_id"]), "pin": pin, "expires_at": end.isoformat()}

    hass.services.async_register(DOMAIN, SERVICE_APPROVE, approve, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_REJECT, reject, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_LIST_REQUESTS, list_requests, supports_response="only")
    hass.services.async_register(DOMAIN, SERVICE_SEND_MESSAGE, send_message, vol.Schema({vol.Required("username"): str, vol.Required("message"): str}))
    hass.services.async_register(DOMAIN, SERVICE_ISSUE_AKUVOX_KEY, issue_akuvox_key, vol.Schema({vol.Required("username"): str, vol.Required("relay_names"): [str], vol.Optional("duration_hours", default=48): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)), vol.Optional("uses", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=100000))}), supports_response="only")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop polling and remove entry-scoped runtime state."""
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime["coordinator"].async_stop()
    if not hass.data[DOMAIN]:
        for service in (SERVICE_APPROVE, SERVICE_REJECT, SERVICE_LIST_REQUESTS, SERVICE_SEND_MESSAGE, SERVICE_ISSUE_AKUVOX_KEY):
            hass.services.async_remove(DOMAIN, service)
    return True
