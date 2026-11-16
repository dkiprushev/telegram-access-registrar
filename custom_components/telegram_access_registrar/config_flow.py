"""Config flow for Telegram Access Registrar."""
from __future__ import annotations

from datetime import datetime
import sqlite3
from string import punctuation

import probatio
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig
from homeassistant.util import dt as dt_util

from . import request_log
from .api import TelegramApi, TelegramApiError
from .const import CONF_ADMIN_CHAT_ID, CONF_BOT_TOKEN, DOMAIN


class TelegramAccessRegistrarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a dedicated Telegram registration bot."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return RegistrarOptionsFlow()

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


class RegistrarOptionsFlow(config_entries.OptionsFlow):
    """Manage this bot through the administrator-only options flow."""

    _chat_id = None
    _before_id = None
    _next_before_id = None

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return await {
                "journal": self.async_step_journal,
                "administrator": self.async_step_administrator,
                "users": self.async_step_users,
            }[user_input["section"]]()
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema({
                probatio.Required("section"): SelectSelector(SelectSelectorConfig(
                    options=["journal", "administrator", "users"], translation_key="options_section",
                )),
            }),
        )

    async def async_step_administrator(self, user_input=None):
        entry = self.config_entry
        chat_id_input = str(entry.data[CONF_ADMIN_CHAT_ID])
        errors = {}
        if user_input is not None:
            chat_id_input = user_input["chat_id"].strip()
            try:
                chat_id = int(chat_id_input)
                if not 0 < chat_id < 2**63:
                    raise ValueError("Invalid private Telegram chat ID")
            except ValueError:
                errors["chat_id"] = "invalid_chat_id"
            else:
                if chat_id != entry.data[CONF_ADMIN_CHAT_ID]:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_ADMIN_CHAT_ID: chat_id}
                    )
                    if not await self.hass.config_entries.async_reload(entry.entry_id):
                        return self.async_abort(reason="admin_saved_reload_failed")
                return self.async_abort(reason="admin_saved")

        return self.async_show_form(
            step_id="administrator",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Required("chat_id"): str}), {"chat_id": chat_id_input}
            ),
            errors=errors,
        )

    async def async_step_users(self, user_input=None):
        entry = self.config_entry
        runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if runtime is None:
            return self.async_abort(reason="integration_unavailable")
        registry = runtime["registry"]
        snapshot = registry.snapshot()
        admin = int(entry.data[CONF_ADMIN_CHAT_ID])
        approved = {row["chat_id"]: row for row in snapshot["approved"]}
        pending = {row["chat_id"]: row for row in snapshot["pending"]}
        errors = {}
        if user_input is not None:
            chat_id = int(user_input["user"])
            action = user_input["action"]
            if chat_id == admin:
                errors["user"] = "protected_admin"
            elif chat_id in approved and action in ("allow", "revoke"):
                await registry.async_set_home_control(chat_id, action == "allow")
                return await self.async_step_users()
            elif chat_id in approved and action == "remove":
                await registry.async_remove_approved(chat_id)
                return await self.async_step_users()
            elif chat_id in pending and action == "approve":
                await registry.async_approve(chat_id)
                return await self.async_step_users()
            elif chat_id in pending and action == "remove":
                await registry.async_reject(chat_id)
                return await self.async_step_users()
            else:
                errors["action"] = "invalid_user_action"

        options = []
        for row in [*approved.values(), *pending.values()]:
            chat_id = row["chat_id"]
            label = row.get("display_name") or str(chat_id)
            if row.get("username"):
                label += f" (@{row['username']})"
            options.append({"value": str(chat_id), "label": f"{label} · {chat_id}"})
        if not options:
            return self.async_abort(reason="no_users")

        sections = {"administrator": [], "allowed": [], "registered": [], "pending": []}
        for option in options:
            chat_id = int(option["value"])
            if chat_id == admin:
                section = "administrator"
            elif chat_id in pending:
                section = "pending"
            else:
                section = "allowed" if approved[chat_id].get("can_control_home") else "registered"
            label = " ".join(option["label"].split()).translate(_MARKDOWN_ESCAPES)
            sections[section].append(label)
        return self.async_show_form(
            step_id="users",
            data_schema=probatio.Schema({
                probatio.Required("user"): SelectSelector(SelectSelectorConfig(options=options)),
                probatio.Required("action"): SelectSelector(SelectSelectorConfig(
                    options=["allow", "revoke", "approve", "remove"], translation_key="user_action",
                )),
            }),
            errors=errors,
            description_placeholders={key: ", ".join(labels) or "—" for key, labels in sections.items()},
        )

    async def async_step_journal(self, user_input=None):
        errors = {}
        chat_id_input = str(self._chat_id) if self._chat_id is not None else ""
        if user_input is not None:
            chat_id_input = user_input.get("chat_id", "").strip()
            try:
                chat_id = int(chat_id_input) if chat_id_input else None
                if chat_id is not None and not -(2**63) <= chat_id < 2**63:
                    raise ValueError("Chat ID is out of range")
            except ValueError:
                errors["chat_id"] = "invalid_chat_id"
            else:
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
        fields = {probatio.Optional("chat_id"): str}
        if self._next_before_id is not None:
            fields[probatio.Optional("older", default=False)] = bool
        return self.async_show_form(
            step_id="journal",
            errors=errors,
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(fields), {"chat_id": chat_id_input} if chat_id_input else {}
            ),
            description_placeholders={
                "requests": "\n\n".join(_format_request(entry) for entry in page["requests"]) or "—"
            },
        )
