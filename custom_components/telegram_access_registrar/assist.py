"""Read-only Grocy tools and attachment delivery for Telegram conversations."""
from __future__ import annotations

import asyncio
from base64 import b64encode
from contextvars import ContextVar
import logging
from urllib.parse import quote
from uuid import uuid4

import probatio
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

_LOGGER = logging.getLogger(__name__)
_media_request: ContextVar[set[int] | None] = ContextVar("smarthouse_media", default=None)
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ReadGrocy(llm.Tool):
    name = "read_grocy"
    description = "Read current Grocy inventory and shopping list. Includes product IDs and photo availability."
    integration = "telegram_access_registrar"

    def __init__(self, inventory):
        self.inventory = inventory

    async def async_call(self, hass, tool_input, llm_context):
        if _media_request.get() is None:
            raise HomeAssistantError("This tool is available in authorized SmartHouse Telegram chats.")
        return llm.ToolResult(data=await self.inventory.async_snapshot(include_product_ids=True))


class AttachGrocyPhotos(llm.Tool):
    name = "attach_grocy_photos"
    description = "Attach product photos to the current Telegram reply. Use only IDs from read_grocy and when the user asks for photos."
    integration = "telegram_access_registrar"
    parameters = probatio.Schema({probatio.Required("product_ids"): [int]})

    async def async_call(self, hass, tool_input, llm_context):
        requested = _media_request.get()
        if requested is None:
            raise HomeAssistantError("No authorized Telegram reply is active.")
        for product_id in tool_input.tool_args["product_ids"][:10]:
            if len(requested) >= 10:
                break
            requested.add(product_id)
        return llm.ToolResult(data={"queued": True, "note": "Delivery is checked after the text reply."})


class SmartHouseAPI(llm.API):
    def __init__(self, hass, inventory, entry_id):
        super().__init__(hass=hass, id=f"smarthouse-{entry_id}", name="SmartHouse Grocy")
        self.inventory = inventory

    async def async_get_api_instance(self, llm_context):
        return llm.APIInstance(
            api=self,
            llm_context=llm_context,
            api_prompt="Use read_grocy for current household stock and shopping. For photo requests, use attach_grocy_photos with actual product IDs.",
            tools=[ReadGrocy(self.inventory), AttachGrocyPhotos()],
        )


class TelegramAssist:
    def __init__(self, hass, api, inventory):
        self.hass = hass
        self.api = api
        self.inventory = inventory
        self.sessions: dict[int, str] = {}
        self.inventory_context: dict[int, tuple[str, str]] = {}
        self.stock_changes: dict[int, tuple[str, dict]] = {}

    async def async_inventory_reply(self, chat_id: int, question: str) -> bool:
        reply, photo_ids, change = await self.inventory.async_inventory_reply(
            question, previous=self.inventory_context.get(chat_id)
        )
        if reply == "NOT_INVENTORY":
            self.inventory_context.pop(chat_id, None)
            return False
        self.inventory_context[chat_id] = (question[:1000], reply[:2000])
        if change:
            token = uuid4().hex
            name, amount, source = (change[key] for key in ("name", "amount", "source"))
            if change["kind"] == "transfer":
                summary = f"Переместить «{name}», {amount:g}, из {source} в {change['destination']}?"
            else:
                summary = f"Изменить остаток «{name}» в {source} с {change['before']:g} на {amount:g}?"
            await self.api.async_send_message(chat_id, summary, [[
                {"text": "Подтвердить", "callback_data": f"tar:edit_apply:{token}"},
                {"text": "Отмена", "callback_data": f"tar:edit_cancel:{token}"},
            ]])
            self.stock_changes[chat_id] = token, change
            return True
        await self.api.async_send_message(chat_id, reply[:4000])
        if photo_ids:
            await self._send_photos(chat_id, photo_ids)
        return True

    async def async_confirm_change(self, chat_id: int, callback_id: str, action: str, token: str) -> None:
        pending = self.stock_changes.get(chat_id)
        if pending is None or pending[0] != token:
            await self.api.async_answer_callback(callback_id, "Черновик больше недоступен")
            return
        _, change = self.stock_changes.pop(chat_id)
        if action == "edit_cancel":
            await self.api.async_answer_callback(callback_id, "Отменено")
            return
        await self.api.async_answer_callback(callback_id, "Проверяю остатки…")
        try:
            applied = await self.inventory.async_apply_change(change)
        except Exception as err:
            _LOGGER.warning("Grocy stock edit failed: %s", type(err).__name__)
            await self.api.async_send_message(chat_id, "Grocy не подтвердил изменение. Проверь остатки перед повтором.")
            return
        self.inventory_context.pop(chat_id, None)
        await self.api.async_send_message(
            chat_id, "Изменение внесено в Grocy." if applied else "Остатки изменились. Запроси новый черновик."
        )

    async def async_reply(self, chat_id: int, question: str) -> bool:
        if not question:
            await self.api.async_send_message(chat_id, "Напиши запрос после /assist.")
            return True
        agent_id = "conversation.smarthouse_telegram"
        state = self.hass.states.get(agent_id)
        if state is None or state.state == "unavailable":
            return False
        request = {"agent_id": agent_id, "text": question}
        if chat_id in self.sessions:
            request["conversation_id"] = self.sessions[chat_id]
        requested: set[int] = set()
        token = _media_request.set(requested)
        try:
            result = await self.hass.services.async_call(
                "conversation", "process", request, blocking=True, return_response=True
            )
        finally:
            _media_request.reset(token)
        response = result.get("response", {})
        if response.get("response_type") == "error":
            raise HomeAssistantError("Codex Assist returned a conversation error")
        session = result.get("conversation_id")
        if isinstance(session, str):
            self.sessions[chat_id] = session
        speech = response.get("speech", {}).get("plain", {}).get("speech")
        if speech:
            await self.api.async_send_message(chat_id, speech[:4000])
        if requested:
            await self._send_photos(chat_id, requested)
        elif not speech:
            await self.api.async_send_message(chat_id, "Ассистент вернул пустой ответ.")
        return True

    async def _send_photos(self, chat_id: int, requested: set[int]) -> None:
        grocy = self.inventory._grocy()
        products = await grocy.async_list("products")
        pictures: dict[str, list[str]] = {}
        for product in products:
            filename = product.get("picture_file_name")
            if int(product["id"]) in requested and isinstance(filename, str) and filename:
                pictures.setdefault(filename, []).append(product["name"])
        sent = 0
        for filename, names in list(pictures.items())[:10]:
            encoded = b64encode(filename.encode()).decode("ascii")
            try:
                async with asyncio.timeout(15):
                    url = grocy._url(f"/api/files/productpictures/{quote(encoded, safe='')}")
                    async with grocy._session.get(url, headers=grocy._headers, allow_redirects=False) as response:
                        if response.status != 200 or response.content_type not in {"image/jpeg", "image/png"}:
                            continue
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > _MAX_IMAGE_BYTES:
                                break
                        content_type = response.content_type
                if not body or len(body) > _MAX_IMAGE_BYTES:
                    continue
                await self.api.async_send_photo(
                    chat_id, {"data": b64encode(body).decode("ascii"), "content_type": content_type},
                    ", ".join(names)[:500],
                )
            except Exception as err:
                _LOGGER.warning("Could not deliver a Grocy picture: %s", type(err).__name__)
            else:
                sent += 1
        if not sent:
            await self.api.async_send_message(chat_id, "Для выбранных товаров не удалось получить фото из Grocy.")
