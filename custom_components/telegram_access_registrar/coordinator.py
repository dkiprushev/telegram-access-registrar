"""Telegram update loop and approval workflow."""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4
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
        answer_inventory: Callable[[int, str], Awaitable[bool]],
        propose_purchase: Callable[[str], Awaitable[list[dict[str, Any]]]],
        add_stock: Callable[[list[dict[str, Any]]], Awaitable[tuple[list[str], list[str]]]],
        propose_photo: Callable[[bytes, str], Awaitable[tuple[list[dict[str, Any]], str]]],
        revise_photo: Callable[[list[dict[str, Any]], str], Awaitable[list[dict[str, Any]]]],
        assist_reply: Callable[[int, str], Awaitable[bool]] | None = None,
        log_request: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._api = api
        self._registry = registry
        self._admin_chat_id = admin_chat_id
        self._fire_access_event = fire_access_event
        self._answer_inventory = answer_inventory
        self._propose_purchase, self._add_stock = propose_purchase, add_stock
        self._propose_photo = propose_photo
        self._revise_photo = revise_photo
        self._assist_reply = assist_reply
        self._log_request = log_request
        self._pending_stock: dict[int, tuple[str, list[dict[str, Any]]]] = {}
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def _send_draft(self, chat_id: int, items: list[dict[str, Any]], title: str) -> None:
        token = uuid4().hex
        lines = "\n".join(f"• {item['name']} — {item['amount']:g}, {item['location']}" for item in items)
        for start in range(0, len(lines), 1800):
            await self._api.async_send_message(chat_id, f"{title}\n{lines[start:start + 1800]}")
        await self._api.async_send_message(chat_id, "Подтвердить добавление?", [[
            {"text": "Добавить", "callback_data": f"tar:stock_add:{token}"},
            {"text": "Отмена", "callback_data": f"tar:stock_cancel:{token}"},
        ]])
        self._pending_stock[chat_id] = token, items

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
        if self._log_request is not None:
            try:
                await self._log_request(update)
            except Exception:
                _LOGGER.exception("Could not write Telegram request journal")
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
        private_user = chat.get("type") == "private" and user.get("id") == chat_id
        approved = self._registry.get_approved(chat_id)
        photos = message.get("photo")
        if private_user and approved and approved.get("can_control_home") and isinstance(photos, list) and photos:
            photo = photos[-1]
            if not isinstance(photo, dict) or not isinstance(photo.get("file_id"), str): return
            try:
                raw, _ = await self._api.async_download_file(photo["file_id"])
                items, _ = await self._propose_photo(raw, str(message.get("caption", "")))
            except Exception as err:
                _LOGGER.exception("Photo inventory analysis failed for chat %s", chat_id)
                await self._api.async_send_message(chat_id, f"Не смог разобрать фото (код: {type(err).__name__}).")
                return
            if not items:
                await self._api.async_send_message(chat_id, "На фото не удалось уверенно распознать предметы.")
                return
            await self._send_draft(chat_id, items, "Черновик по фото. Можно уточнить обычным сообщением:")
            return
        command, argument = self._command(str(message.get("text", "")))
        if command in {"/home_allow", "/home_revoke", "/home_users"}:
            if private_user and chat_id == self._admin_chat_id:
                await self._manage_home_users(chat_id, command, argument)
            return
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
        if private_user and approved and approved.get("can_control_home") and message.get("text"):
            if command == "/assist" and self._assist_reply is not None:
                try:
                    if not await self._assist_reply(chat_id, argument):
                        await self._api.async_send_message(chat_id, "Настрой разговорный агент conversation.smarthouse_telegram с инструментами SmartHouse Grocy.")
                except Exception as err:
                    _LOGGER.warning("Telegram Assist request failed: %s", type(err).__name__)
                    await self._api.async_send_message(chat_id, "Не удалось получить ответ Codex Assist. Проверь настройки разговорного агента.")
                return
            try:
                text = str(message["text"])
                pending = self._pending_stock.get(chat_id)
                if pending and pending[1][0].get("photo") and not command.startswith("/") and chat.get("type") == "private" and user.get("id") == chat_id:
                    revised = await self._revise_photo(pending[1], text)
                    if revised:
                        await self._send_draft(chat_id, revised, "Обновлённый черновик по фото:")
                    else:
                        await self._api.async_send_message(chat_id, "Не удалось применить уточнение. Текущий черновик сохранён.")
                    return
                if text.casefold().startswith(("купил", "купила", "добавь", "добавил", "/add")):
                    items = await self._propose_purchase(text)
                    if not items:
                        await self._api.async_send_message(chat_id, "Не увидел в сообщении понятных покупок. Напиши, например: «купил 2 пиццы и салфетки»." )
                        return
                    await self._send_draft(chat_id, items, "Черновик добавления в Grocy:")
                    return
                if await self._answer_inventory(chat_id, text):
                    return
                if self._assist_reply is not None and await self._assist_reply(chat_id, text):
                    return
                await self._api.async_send_message(chat_id, "Для этого вопроса нужен разговорный агент Home Assistant.")
            except Exception as err:  # The bot must remain available if Grocy/AI is down.
                _LOGGER.warning("Inventory question failed: %s", err)
                await self._api.async_send_message(chat_id, "Не смог получить данные Grocy. Попробуйте ещё раз позже.")
            return
        if chat_id != self._admin_chat_id:
            await self._api.async_send_message(chat_id, "Нажмите /start, чтобы отправить заявку на доступ.")

    async def _manage_home_users(self, chat_id: int, command: str, argument: str) -> None:
        if command == "/home_users":
            users = self._registry.snapshot()["approved"]
            lines = [
                f"{'✓' if row.get('can_control_home') else '—'} "
                f"@{row['username'] or 'без_username'} · {row['chat_id']}"
                for row in users
            ]
            for start in range(0, len(lines), 40):
                await self._api.async_send_message(chat_id, "Права на общение с домом:\n" + "\n".join(lines[start:start + 40]))
            if not lines:
                await self._api.async_send_message(chat_id, "Одобренных пользователей пока нет.")
            return
        target = (
            self._registry.get_approved(int(argument))
            if argument.isascii() and argument.isdecimal() and len(argument) <= 20
            else self._registry.get_approved_by_username(argument) if argument else None
        )
        if not target:
            await self._api.async_send_message(chat_id, "Укажи @username или ID одобренного пользователя. Список: /home_users")
            return
        allowed = command == "/home_allow"
        target_id = target["chat_id"]
        if target_id == self._admin_chat_id and not allowed:
            await self._api.async_send_message(chat_id, "Администратор сохраняет доступ к дому.")
            return
        updated = await self._registry.async_set_home_control(target_id, allowed)
        if not updated:
            await self._api.async_send_message(chat_id, "Пользователь больше не одобрен.")
            return
        if not allowed:
            self._pending_stock.pop(target_id, None)
        status = "разрешено" if allowed else "отозвано"
        await self._api.async_send_message(chat_id, f"Общение с домом для {target_id}: {status}.")

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
        if not isinstance(sender, dict) or not isinstance(callback_id, str):
            return
        sender_id = int(sender.get("id", 0))
        if isinstance(data, str) and data.startswith(("tar:stock_add:", "tar:stock_cancel:")):
            _, action, token = data.split(":", 2)
            pending = self._pending_stock.get(sender_id)
            approved = self._registry.get_approved(sender_id)
            if not pending or pending[0] != token or not approved or not approved.get("can_control_home"):
                await self._api.async_answer_callback(callback_id, "Черновик больше недоступен")
                return
            _, items = self._pending_stock.pop(sender_id)
            if action == "stock_cancel":
                await self._api.async_answer_callback(callback_id, "Отменено")
                return
            try:
                await self._api.async_answer_callback(callback_id, "Добавляю в Grocy…")
            except TelegramApiError:
                _LOGGER.debug("Telegram could not acknowledge the stock confirmation")
            try: added, removed = await self._add_stock(items)
            except Exception as err:
                _LOGGER.warning("Grocy stock add failed: %s", err)
                await self._api.async_send_message(sender_id, "Импорт остановился с ошибкой. Часть позиций могла сохраниться. Проверь остатки Grocy перед повтором.")
            else:
                text = "Добавлено в Grocy:\n" + "\n".join(f"• {x}" for x in added)
                if removed:
                    text += "\n\nУдалено из списка покупок:\n" + "\n".join(f"• {x}" for x in removed)
                await self._api.async_send_message(sender_id, text)
            return
        if sender_id != self._admin_chat_id:
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
