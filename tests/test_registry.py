"""Tests for the local identity registry."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from telegram_access_registrar.registry import AccessRegistry


class MemoryStore:
    def __init__(self) -> None:
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


async def test_request_is_pending_once_and_can_be_approved():
    store = MemoryStore()
    registry = AccessRegistry(store)
    await registry.async_load()

    first, request = await registry.async_request(
        {"id": 42}, {"username": "Visitor", "first_name": "Visitor"}
    )
    repeat, _ = await registry.async_request(
        {"id": 42}, {"username": "Visitor", "first_name": "Visitor"}
    )

    assert first is True
    assert repeat is False
    assert request["username"] == "visitor"
    assert len(registry.snapshot()["pending"]) == 1

    approved = await registry.async_approve(42)
    assert approved == {"chat_id": 42, "username": "visitor", "display_name": "Visitor", "can_control_home": False}
    assert registry.get_approved_by_username("@VISITOR") == approved
    assert registry.snapshot()["pending"] == []


async def test_reject_does_not_create_an_approved_user():
    store = MemoryStore()
    registry = AccessRegistry(store)
    await registry.async_load()
    await registry.async_request({"id": 77}, {"username": "guest"})

    assert (await registry.async_reject(77))["chat_id"] == 77
    assert registry.get_approved(77) is None
    assert registry.snapshot() == {"approved": [], "pending": []}
