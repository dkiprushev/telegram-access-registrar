"""Telegram Access Registrar integration."""
from __future__ import annotations

from typing import Any
from datetime import UTC, datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers import llm

from .api import TelegramApi
from . import request_log
from homeassistant.exceptions import Unauthorized
from .assist import SmartHouseAPI, TelegramAssist
from .const import (
    CONF_ADMIN_CHAT_ID,
    CONF_BOT_TOKEN,
    DOMAIN,
    EVENT_ACCESS_REQUESTED,
    SERVICE_APPROVE,
    SERVICE_LIST_REQUESTS,
    SERVICE_ISSUE_AKUVOX_KEY,
    SERVICE_SET_HOME_CONTROL,
    SERVICE_SET_ADMIN_CHAT_ID,
    SERVICE_REJECT,
    SERVICE_SEND_MESSAGE,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import RegistrarCoordinator
from .inventory import InventoryAssistant
from .news import NewsBulletin, next_bulletin
from .registry import AccessRegistry

NEWS_CONFIG = "telegram_access_registrar_news_config"
NEWS_SERVICE = "news_now"
_NEWS_SCHEMA = vol.Schema({
    vol.Optional("enabled", default=False): bool,
    vol.Optional("ai_task", default="ai_task.codex_assist_ai_task"): str,
    # Accept existing YAML from releases with separate news TTS settings.
    vol.Optional("tts"): dict,
})
CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN, default=dict): vol.Schema({vol.Optional("news", default=dict): _NEWS_SCHEMA})},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data[NEWS_CONFIG] = config[DOMAIN]["news"]
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start one independent Telegram long-polling registration bot."""
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}")
    registry = AccessRegistry(store)
    session = async_get_clientsession(hass)
    api = TelegramApi(session, entry.data[CONF_BOT_TOKEN])
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

    inventory = InventoryAssistant(hass)
    assist = TelegramAssist(hass, api, inventory)
    news_config = hass.data[NEWS_CONFIG]
    bulletin = NewsBulletin(hass, session, api, int(entry.data[CONF_ADMIN_CHAT_ID]), news_config)
    log_path = request_log.storage_path(hass.config, entry.entry_id)

    async def log_request(update: dict[str, Any]) -> None:
        await hass.async_add_executor_job(
            request_log.record, log_path, int(entry.data[CONF_ADMIN_CHAT_ID]), update
        )

    async def news_command(chat_id: int, command: str, argument: str) -> None:
        await bulletin.handle_command(registry, chat_id, command, argument)

    coordinator = RegistrarCoordinator(
        api,
        registry,
        int(entry.data[CONF_ADMIN_CHAT_ID]),
        lambda data: (hass.bus.async_fire(EVENT_ACCESS_REQUESTED, data), request_access(data)),
        add_stock=inventory.async_add_stock,
        assist_reply=assist.async_reply,
        log_request=log_request,
        confirm_change=assist.async_confirm_change,
        news_command=news_command,
    )
    await coordinator.async_start()
    await registry.async_ensure_admin(int(entry.data[CONF_ADMIN_CHAT_ID]))
    cancel_news = None
    news_stopped = False

    async def scheduled_news(_now: datetime) -> None:
        nonlocal cancel_news
        if news_stopped:
            return
        cancel_news = async_track_point_in_utc_time(hass, scheduled_news, next_bulletin(datetime.now(UTC)))
        await bulletin.run(
            deliver=True, scheduled=True, recipients=registry.news_subscribers, prompt_for=registry.news_prompt,
        )

    def stop_news() -> None:
        nonlocal news_stopped
        news_stopped = True
        bulletin.close()
        if cancel_news is not None:
            cancel_news()

    if news_config["enabled"]:
        cancel_news = async_track_point_in_utc_time(hass, scheduled_news, next_bulletin(datetime.now(UTC)))
    entry.async_on_unload(stop_news)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "registry": registry,
        "request_log_path": log_path,
        "coordinator": coordinator,
        "api": api,  # Kept runtime-only; never persisted.
        "news": bulletin,
    }
    _async_register_services(hass)
    entry.async_on_unload(llm.async_register_api(hass, SmartHouseAPI(hass, inventory, entry.entry_id, api)))
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
        pin = next((str(key[k]) for k in ("key_code", "TmpKey", "pin", "code", "password", "KeyCode") if key.get(k)), "see QR code")
        await runtime["api"].async_send_message(recipient["chat_id"], f"Доступ активен до {end:%d.%m %H:%M}. PIN: {pin}")
        await runtime["api"].async_send_photo(recipient["chat_id"], await client.async_get_panel_temp_key_qr(str(key["key_id"])), "QR-код доступа: вход и пассажирский лифт")
        return {"key_id": str(key["key_id"]), "pin": pin, "expires_at": end.isoformat()}

    async def set_home_control(call: ServiceCall) -> dict[str, Any]:
        user = await _runtime(hass)["registry"].async_set_home_control(call.data["chat_id"], call.data["allowed"])
        return {"updated": user is not None, "recipient": user}

    async def set_admin_chat_id(call: ServiceCall) -> None:
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ADMIN_CHAT_ID: call.data["chat_id"]})
        await hass.config_entries.async_reload(entry.entry_id)

    async def list_request_log(call: ServiceCall) -> dict[str, Any]:
        user_id = call.context.user_id
        user = await hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
        return await hass.async_add_executor_job(
            request_log.read, _runtime(hass)["request_log_path"], call.data["limit"],
            call.data.get("before_id"), call.data.get("chat_id"),
        )

    async def news_now(call: ServiceCall) -> dict[str, Any]:
        user_id = call.context.user_id
        user = await hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
        runtime = _runtime(hass)
        return await runtime["news"].run(
            deliver=call.data["deliver"], prompt_for=runtime["registry"].news_prompt,
        )

    hass.services.async_register(
        DOMAIN, NEWS_SERVICE, news_now,
        vol.Schema({vol.Optional("deliver", default=False): bool}), supports_response="only",
    )
    hass.services.async_register(
        DOMAIN, "list_request_log", list_request_log,
        vol.Schema({
            vol.Optional("limit", default=25): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Optional("before_id"): vol.Coerce(int),
            vol.Optional("chat_id"): vol.Coerce(int),
        }),
        supports_response="only",
    )
    hass.services.async_register(DOMAIN, SERVICE_APPROVE, approve, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_REJECT, reject, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_LIST_REQUESTS, list_requests, supports_response="only")
    hass.services.async_register(DOMAIN, SERVICE_SEND_MESSAGE, send_message, vol.Schema({vol.Required("username"): str, vol.Required("message"): str}))
    hass.services.async_register(DOMAIN, SERVICE_ISSUE_AKUVOX_KEY, issue_akuvox_key, vol.Schema({vol.Required("username"): str, vol.Required("relay_names"): [str], vol.Optional("duration_hours", default=48): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)), vol.Optional("uses", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=100000))}), supports_response="only")
    hass.services.async_register(DOMAIN, SERVICE_SET_HOME_CONTROL, set_home_control, vol.Schema({vol.Required("chat_id"): vol.Coerce(int), vol.Required("allowed"): bool}), supports_response="optional")
    hass.services.async_register(DOMAIN, SERVICE_SET_ADMIN_CHAT_ID, set_admin_chat_id, vol.Schema({vol.Required("chat_id"): vol.Coerce(int)}))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop polling and remove entry-scoped runtime state."""
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime["coordinator"].async_stop()
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, "list_request_log")
        hass.services.async_remove(DOMAIN, NEWS_SERVICE)
        for service in (SERVICE_APPROVE, SERVICE_REJECT, SERVICE_LIST_REQUESTS, SERVICE_SEND_MESSAGE, SERVICE_ISSUE_AKUVOX_KEY, SERVICE_SET_HOME_CONTROL, SERVICE_SET_ADMIN_CHAT_ID):
            hass.services.async_remove(DOMAIN, service)
    return True
