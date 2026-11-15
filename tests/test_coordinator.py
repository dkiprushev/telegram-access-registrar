"""Tests for registration, admin approval and access-request routing."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from telegram_access_registrar.coordinator import RegistrarCoordinator
from telegram_access_registrar.registry import AccessRegistry


class MemoryStore:
    async def async_load(self):
        return None

    async def async_save(self, data):
        self.data = data


class FakeApi:
    def __init__(self) -> None:
        self.messages = []
        self.answers = []

    async def async_send_message(self, chat_id, text, inline_keyboard=None):
        self.messages.append((chat_id, text, inline_keyboard))

    async def async_answer_callback(self, callback_id, text):
        self.answers.append((callback_id, text))


async def test_start_approval_and_access_command():
    api = FakeApi()
    registry = AccessRegistry(MemoryStore())
    events = []
    coordinator = RegistrarCoordinator(api, registry, 1, events.append)
    await registry.async_load()

    await coordinator.async_handle_update({"message": {"chat": {"id": 10}, "from": {"id": 10, "username": "ira", "first_name": "Ira"}, "text": "/start"}})
    assert api.messages[0][0] == 10
    assert api.messages[1][0] == 1
    assert api.messages[1][2][0][0]["callback_data"] == "tar:approve:10"

    await coordinator.async_handle_update({"callback_query": {"id": "cb1", "from": {"id": 1}, "data": "tar:approve:10"}})
    assert registry.get_approved_by_username("ira")["chat_id"] == 10
    assert api.answers == [("cb1", "Пользователь разрешён")]

    await coordinator.async_handle_update({"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/access @ira"}})
    assert events == [{"recipient": {"chat_id": 10, "username": "ira", "display_name": "Ira"}, "requested_by": 1}]


async def test_non_admin_cannot_request_access():
    api = FakeApi()
    registry = AccessRegistry(MemoryStore())
    coordinator = RegistrarCoordinator(api, registry, 1, lambda _: None)
    await registry.async_load()

    await coordinator.async_handle_update({"message": {"chat": {"id": 10}, "from": {"id": 10}, "text": "/access @ira"}})
    assert "Нажмите /start" in api.messages[0][1]
