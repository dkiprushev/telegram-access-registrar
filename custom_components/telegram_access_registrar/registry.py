"""Persistent approved-user and pending-request registry."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol


class AsyncStore(Protocol):
    async def async_load(self) -> dict[str, Any] | None: ...
    async def async_save(self, data: dict[str, Any]) -> None: ...


def _empty() -> dict[str, Any]:
    return {"version": 1, "approved": {}, "pending": {}}


class AccessRegistry:
    """Store Telegram identities locally; usernames are never trusted as identity."""

    def __init__(self, store: AsyncStore) -> None:
        self._store = store
        self._data = _empty()

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._data = {**_empty(), **data}
            self._data["approved"] = dict(self._data.get("approved", {}))
            self._data["pending"] = dict(self._data.get("pending", {}))

    async def _save(self) -> None:
        await self._store.async_save(self._data)

    @staticmethod
    def _identity(chat: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        chat_id = int(chat["id"])
        username = str(user.get("username", "")).lstrip("@").lower()
        display_name = " ".join(
            value for value in (str(user.get("first_name", "")), str(user.get("last_name", ""))) if value
        ).strip()
        return {"chat_id": chat_id, "username": username, "display_name": display_name, "can_control_home": False}

    async def async_request(self, chat: dict[str, Any], user: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Record an unknown requester once; return whether an admin must be notified."""
        identity = self._identity(chat, user)
        key = str(identity["chat_id"])
        if key in self._data["approved"]:
            return False, deepcopy(self._data["approved"][key])
        existing = self._data["pending"].get(key)
        self._data["pending"][key] = identity
        await self._save()
        return existing is None, deepcopy(identity)

    async def async_approve(self, chat_id: int) -> dict[str, Any] | None:
        key = str(chat_id)
        identity = self._data["pending"].pop(key, None)
        if identity is None:
            return None
        self._data["approved"][key] = identity
        await self._save()
        return deepcopy(identity)

    async def async_reject(self, chat_id: int) -> dict[str, Any] | None:
        identity = self._data["pending"].pop(str(chat_id), None)
        if identity is not None:
            await self._save()
        return deepcopy(identity) if identity else None

    def get_approved_by_username(self, username: str) -> dict[str, Any] | None:
        needle = username.lstrip("@").lower()
        matches = [item for item in self._data["approved"].values() if item.get("username") == needle]
        return deepcopy(matches[0]) if len(matches) == 1 else None

    def get_approved(self, chat_id: int) -> dict[str, Any] | None:
        item = self._data["approved"].get(str(chat_id))
        return deepcopy(item) if item else None

    async def async_set_home_control(self, chat_id: int, allowed: bool) -> dict[str, Any] | None:
        item = self._data["approved"].get(str(chat_id))
        if item is None:
            return None
        item["can_control_home"] = allowed
        await self._save()
        return deepcopy(item)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "approved": [deepcopy(item) for item in self._data["approved"].values()],
            "pending": [deepcopy(item) for item in self._data["pending"].values()],
        }
