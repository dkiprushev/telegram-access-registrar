"""Telegram update loop and approval workflow."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .api import TelegramApi, TelegramApiError
from .const import CALLBACK_PREFIX, COMMAND_ACCESS, COMMAND_START, EVENT_ACCESS_REQUESTED
from .registry import AccessRegistry

_LOGGER = logging.getLogger(__name__)


class RegistrarCoordinator:
    """Receive registrations and let only the configured admin request access."""

    def __init__(
        self,
        api: TelegramApi,
        registry: AccessRegistry,
        admin_chat_id: int,
        fire_access_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self._api = api
        self._registry = registry
        self._admin_chat_id = admin_chat_id
        self._fire_access_event = fire_access_event
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def async_start(self) -> None:
        await self._registry.async_load()
        self._task = asyncio.create_task(self._async_poll(), name="telegram-access-registrar")

    async def async_stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _async_poll(self) -> None:
        while True:
            try:
                for update in await self._api.async_get_updates(self._offset):
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    await self.async_handle_update(update)
            except asyncio.CancelledError:
                raise
            except TelegramApiError as err:
                _LOGGER.warning("Telegram update polling failed: %s", err)
                await asyncio.sleep(5)

    @staticmethod
    def _command(text: str) -> tuple[str, str]:
        first, _, arguments = text.strip().partition(" ")
        return first.lower().split("@", 1)[0], arguments.strip()

    async def async_handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("message"), dict):
            await self._async_handle_message(update["message"])
        elif isinstance(update.get("callback_query"), dict):
            await self._async_handle_callback(update["callback_query"])

    async def _async_handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        user = message.get("from")
        if not isinstance(chat, dict) or not isinstance(user, dict) or not isinstance(chat.get("id"), int):
            return
        chat_id = int(chat["id"])
        command, argument = self._command(str(message.get("text", "")))
        if command == "/whoami":
            is_admin = user.get("id") == self._admin_chat_id
            await self._api.async_send_message(
                chat_id,
                f"Telegram User ID: {user.get('id')}\nChat ID: {chat_id}\nАдминистратор: {'да' if is_admin else 'нет'}",
            )
            return
        if command == COMMAND_START:
            needs_review, identity = await self._registry.async_request(chat, user)
            if needs_review:
                await self._api.async_send_message(chat_id, "Заявка отправлена администратору. Мы сообщим о решении.")
                await self._notify_admin_of_request(identity)
            else:
                await self._api.async_send_message(chat_id, "Вы уже зарегистрированы.")
            return
        # Authorisation belongs to the Telegram user, not to the destination
        # chat.  This keeps admin commands valid outside a private chat too.
        if command == COMMAND_ACCESS and user.get("id") == self._admin_chat_id:
            await self._async_request_access(argument)
            return
        if chat_id != self._admin_chat_id:
            await self._api.async_send_message(chat_id, "Нажмите /start, чтобы отправить заявку на доступ.")

    async def _notify_admin_of_request(self, identity: dict[str, Any]) -> None:
        handle = f"@{identity['username']}" if identity.get("username") else "без username"
        text = f"Новая заявка: {identity.get('display_name') or 'Пользователь'} ({handle}), ID {identity['chat_id']}"
        chat_id = str(identity["chat_id"])
        keyboard = [[
            {"text": "Разрешить", "callback_data": f"{CALLBACK_PREFIX}:approve:{chat_id}"},
            {"text": "Отклонить", "callback_data": f"{CALLBACK_PREFIX}:reject:{chat_id}"},
        ]]
        await self._api.async_send_message(self._admin_chat_id, text, keyboard)

    async def _async_handle_callback(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from")
        callback_id = callback.get("id")
        data = callback.get("data")
        if not isinstance(sender, dict) or sender.get("id") != self._admin_chat_id or not isinstance(callback_id, str):
            return
        try:
            prefix, decision, raw_chat_id = str(data).split(":", 2)
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            await self._api.async_answer_callback(callback_id, "Некорректная заявка")
            return
        if prefix != CALLBACK_PREFIX or decision not in {"approve", "reject"}:
            await self._api.async_answer_callback(callback_id, "Некорректная заявка")
            return
        identity = await (self._registry.async_approve(chat_id) if decision == "approve" else self._registry.async_reject(chat_id))
        if identity is None:
            await self._api.async_answer_callback(callback_id, "Заявка уже обработана")
            return
        if decision == "approve":
            await self._api.async_send_message(chat_id, "Заявка одобрена. Администратор может выдать вам временный ключ.")
            await self._api.async_answer_callback(callback_id, "Пользователь разрешён")
        else:
            await self._api.async_send_message(chat_id, "Заявка отклонена.")
            await self._api.async_answer_callback(callback_id, "Заявка отклонена")

    async def _async_request_access(self, argument: str) -> None:
        identity = self._registry.get_approved_by_username(argument)
        if identity is None:
            await self._api.async_send_message(self._admin_chat_id, "Получатель не одобрен или у него нет Telegram username.")
            return
        self._fire_access_event({"recipient": identity, "requested_by": self._admin_chat_id})
        await self._api.async_send_message(self._admin_chat_id, f"Запрос доступа для @{identity['username']} передан в Home Assistant.")
