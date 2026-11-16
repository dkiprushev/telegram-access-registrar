"""Config flow for Telegram Access Registrar."""
from __future__ import annotations

from datetime import datetime
import sqlite3
from string import punctuation

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import request_log
from .api import TelegramApi, TelegramApiError
from .const import CONF_ADMIN_CHAT_ID, CONF_BOT_TOKEN, DOMAIN


class TelegramAccessRegistrarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a dedicated Telegram registration bot."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return RequestLogOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                profile = await TelegramApi(
                    async_get_clientsession(self.hass), user_input[CONF_BOT_TOKEN]
                ).async_validate_token()
            except TelegramApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(profile["id"]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=profile.get("username", "Telegram registrar"), data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_BOT_TOKEN): str,
                # Home Assistant's form frontend submits number fields as text.
                # Coercion keeps the config flow compatible with both the UI and
                # YAML/test callers while retaining an integer in the entry.
                vol.Required(CONF_ADMIN_CHAT_ID): vol.Coerce(int),
            }),
            errors=errors,
        )


# Telegram input is untrusted. Render Markdown punctuation as literal text.
_MARKDOWN_ESCAPES = str.maketrans({char: f"&#{ord(char)};" for char in punctuation})


def _format_request(entry):
    message = entry.get("message") or {}
    sender = entry.get("from") or message.get("from") or {}
    username = sender.get("username")
    name = sender.get("first_name") or ""
    if sender.get("last_name"):
        name = f"{name} {sender['last_name']}".strip()
    sender_label = " ".join(part for part in (name, f"@{username}" if username else "") if part)
    received = dt_util.as_local(datetime.fromisoformat(entry["received_at"]))
    details = entry.get("data") or message.get("text") or message.get("caption")
    if not details:
        details = _attachment_label(message)
    return (
        f"**{received:%Y-%m-%d %H:%M:%S}** · "
        f"{' '.join(sender_label.split()).translate(_MARKDOWN_ESCAPES)} "
        f"(`{entry['chat_id']}`)  \n"
        f"{' '.join(details.split()).translate(_MARKDOWN_ESCAPES)}"
    )


def _attachment_label(message):
    for kind in ("photo", "document", "video", "voice", "audio", "sticker", "animation",
                 "video_note", "location", "contact"):
        if kind in message:
            return f"[{kind}]"
    return "—"


class RequestLogOptionsFlow(config_entries.OptionsFlow):
    """Browse the journal through the administrator-only options flow."""

    _chat_id = None
    _before_id = None
    _next_before_id = None

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["request_log"])

    async def async_step_request_log(self, user_input=None):
        if user_input is not None:
            chat_id = user_input.get("chat_id")
            if chat_id == self._chat_id and user_input.get("older") and self._next_before_id is not None:
                self._before_id = self._next_before_id
            else:
                self._before_id = None
            self._chat_id = chat_id

        path = request_log.storage_path(self.hass.config, self.config_entry.entry_id)
        try:
            page = await self.hass.async_add_executor_job(
                request_log.read, path, 25, self._before_id, self._chat_id
            )
        except (OSError, sqlite3.Error):
            return self.async_abort(reason="request_log_unavailable")

        self._next_before_id = page["next_before_id"]
        fields = {vol.Optional("chat_id"): vol.Coerce(int)}
        if self._next_before_id is not None:
            fields[vol.Optional("older", default=False)] = bool
        return self.async_show_form(
            step_id="request_log",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), {"chat_id": self._chat_id} if self._chat_id is not None else {}
            ),
            description_placeholders={
                "requests": "\n\n".join(_format_request(entry) for entry in page["requests"]) or "—"
            },
        )
