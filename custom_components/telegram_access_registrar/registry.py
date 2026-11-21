"""Persistent approved-user and pending-request registry."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from typing import Any, Protocol

MAX_NEWS_PROMPT_LENGTH = 3000


class AsyncStore(Protocol):
    async def async_load(self) -> dict[str, Any] | None: ...
    async def async_save(self, data: dict[str, Any]) -> None: ...


def _empty() -> dict[str, Any]:
    return {"version": 1, "approved": {}, "pending": {}, "news_subscribers": [], "news_prompts": {}}


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
            # A user can add or rename their Telegram username after approval.
            # Refresh presentation fields while retaining explicit permissions.
            identity["can_control_home"] = bool(self._data["approved"][key].get("can_control_home", False))
            self._data["approved"][key] = identity
            await self._save()
            return False, deepcopy(identity)
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

    async def async_ensure_admin(self, chat_id: int) -> dict[str, Any]:
        """Bootstrap the configured administrator without a self-approval step."""
        key = str(chat_id)
        identity = self._data["approved"].get(key, {
            "chat_id": chat_id,
            "username": "",
            "display_name": "Administrator",
        })
        identity["can_control_home"] = True
        self._data["approved"][key] = identity
        self._data["pending"].pop(key, None)
        await self._save()
        return deepcopy(identity)

    async def async_remove_approved(self, chat_id: int) -> None:
        """Remove registration and its associated home control permission."""
        if self._data["approved"].pop(str(chat_id), None) is not None:
            self._remove_news_subscription(chat_id)
            await self._save()

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
        if not allowed:
            self._remove_news_subscription(chat_id)
        await self._save()
        return deepcopy(item)

    def news_subscribers(self) -> tuple[int, ...]:
        return tuple(
            chat_id for chat_id in self._data["news_subscribers"]
            if self._data["approved"].get(str(chat_id), {}).get("can_control_home")
        )

    def news_prompt(self, chat_id: int) -> str:
        return self._data["news_prompts"].get(str(chat_id), "")

    async def async_set_news_prompt(self, chat_id: int, prompt: str) -> bool:
        if chat_id not in self.news_subscribers():
            return False
        prompt = prompt.strip()
        if len(prompt) > MAX_NEWS_PROMPT_LENGTH:
            raise ValueError("News prompt is too long")
        if prompt:
            self._data["news_prompts"][str(chat_id)] = prompt
        else:
            self._data["news_prompts"].pop(str(chat_id), None)
        await self._save()
        return True

    async def async_subscribe_to_news(self, chat_id: int) -> bool:
        """Subscribe an authorized chat. Return true for a new subscription."""
        if not self._data["approved"].get(str(chat_id), {}).get("can_control_home"):
            return False
        if chat_id in self._data["news_subscribers"]:
            return False
        self._data["news_subscribers"].append(chat_id)
        await self._save()
        return True

    def _remove_news_subscription(self, chat_id: int) -> bool:
        subscribers = self._data["news_subscribers"]
        if chat_id not in subscribers:
            return False
        subscribers.remove(chat_id)
        self._data["news_prompts"].pop(str(chat_id), None)
        return True

    async def async_unsubscribe_from_news(self, chat_id: int) -> bool:
        if not self._remove_news_subscription(chat_id):
            return False
        await self._save()
        return True

    async def async_request_key(self, chat, user):
        requests = self._data.setdefault("key_requests", {})
        for token, request in requests.items():
            if request["identity"]["chat_id"] == chat["id"] and request["status"] == "pending":
                return token, False
        token = uuid4().hex
        requests[token] = {"identity": self._identity(chat, user), "status": "pending"}
        await self._save()
        return token, True

    async def async_decide_key(self, token, allowed):
        request = self._data.setdefault("key_requests", {}).get(token)
        if request is None or request["status"] != "pending":
            return None
        request["status"] = "issuing" if allowed else "denied"
        await self._save()
        return deepcopy(request["identity"])

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "approved": [deepcopy(item) for item in self._data["approved"].values()],
            "pending": [deepcopy(item) for item in self._data["pending"].values()],
        }
