"""Minimal Telegram Bot API client using Home Assistant's shared session."""
from __future__ import annotations

from typing import Any
import base64

from aiohttp import ClientError, ClientSession, FormData


class TelegramApiError(RuntimeError):
    """Telegram returned an invalid API response."""


class TelegramApi:
    """Small, dependency-free client for just the methods this integration uses."""

    def __init__(self, session: ClientSession, token: str) -> None:
        self._session = session
        self._base_url = f"https://api.telegram.org/bot{token}"

    async def _post(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            async with self._session.post(
                f"{self._base_url}/{method}", json=payload, timeout=35
            ) as response:
                body = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise TelegramApiError(f"Telegram {method} request failed") from err
        if not response.ok or not body.get("ok"):
            raise TelegramApiError(str(body.get("description", "Telegram API error")))
        return body.get("result")

    async def async_validate_token(self) -> dict[str, Any]:
        result = await self._post("getMe", {})
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram returned an invalid bot profile")
        return result

    async def async_get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._post("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def async_download_file(self, file_id: str) -> tuple[bytes, str]:
        """Download a Telegram attachment without persisting Telegram credentials."""
        result = await self._post("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path:
            raise TelegramApiError("Telegram did not return the attachment path")
        try:
            async with self._session.get(f"https://api.telegram.org/file/bot{self._base_url.rsplit('bot', 1)[1]}/{path}", timeout=35) as response:
                raw = await response.read()
        except (ClientError, TimeoutError) as err:
            raise TelegramApiError("Telegram attachment download failed") from err
        if not response.ok or not raw:
            raise TelegramApiError("Telegram attachment download failed")
        return raw, path

    async def async_send_message(
        self, chat_id: int, text: str, inline_keyboard: list[list[dict[str, str]]] | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        await self._post("sendMessage", payload)

    async def async_answer_callback(self, callback_id: str, text: str) -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def async_send_voice(self, chat_id: int, voice: bytes, *, caption: str = "") -> None:
        """Upload an Ogg Opus recording as a Telegram voice note."""
        form = FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("voice", voice, filename="smarthouse-news.ogg", content_type="audio/ogg")
        if caption:
            form.add_field("caption", caption)
        try:
            async with self._session.post(
                f"{self._base_url}/sendVoice", data=form, timeout=35, allow_redirects=False,
            ) as response:
                body = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise TelegramApiError("Telegram voice delivery failed") from err
        if not response.ok or not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramApiError("Telegram rejected the voice note")

    async def async_send_photo(self, chat_id: int, image: dict[str, str], caption: str) -> None:
        """Send a QR image returned by Akuvox's safe panel API."""
        try:
            raw = base64.b64decode(image["data"], validate=True)
            form = __import__("aiohttp").FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("caption", caption)
            form.add_field("photo", raw, filename="access-qr.png", content_type=image.get("content_type", "image/png"))
            async with self._session.post(f"{self._base_url}/sendPhoto", data=form, timeout=35) as response:
                body = await response.json(content_type=None)
        except (KeyError, ValueError, ClientError, TimeoutError) as err:
            raise TelegramApiError("Telegram QR delivery failed") from err
        if not response.ok or not body.get("ok"):
            raise TelegramApiError(str(body.get("description", "Telegram API error")))
