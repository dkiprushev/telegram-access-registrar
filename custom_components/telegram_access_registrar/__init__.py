"""Telegram Access Registrar integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
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
    coordinator = RegistrarCoordinator(
        api,
        registry,
        int(entry.data[CONF_ADMIN_CHAT_ID]),
        lambda data: hass.bus.async_fire(EVENT_ACCESS_REQUESTED, data),
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

    hass.services.async_register(DOMAIN, SERVICE_APPROVE, approve, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_REJECT, reject, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_LIST_REQUESTS, list_requests, supports_response="only")
    hass.services.async_register(DOMAIN, SERVICE_SEND_MESSAGE, send_message, vol.Schema({vol.Required("username"): str, vol.Required("message"): str}))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop polling and remove entry-scoped runtime state."""
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime["coordinator"].async_stop()
    if not hass.data[DOMAIN]:
        for service in (SERVICE_APPROVE, SERVICE_REJECT, SERVICE_LIST_REQUESTS, SERVICE_SEND_MESSAGE):
            hass.services.async_remove(DOMAIN, service)
    return True
