"""Config flow for Telegram Access Registrar."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelegramApi, TelegramApiError
from .const import CONF_ADMIN_CHAT_ID, CONF_BOT_TOKEN, DOMAIN


class TelegramAccessRegistrarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a dedicated Telegram registration bot."""

    VERSION = 1

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
                vol.Required(CONF_ADMIN_CHAT_ID): int,
            }),
            errors=errors,
        )
