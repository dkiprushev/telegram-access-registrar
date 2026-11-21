"""Native HA shopping-list regression tests, without starting Home Assistant."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

spec = importlib.util.spec_from_file_location(
    "inventory_under_test", Path(__file__).parents[1] / "custom_components/telegram_access_registrar/inventory.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PhotoShoppingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.todo = [{"uid": "cucumber-uid", "summary": "Огурцы 3шт", "status": "needs_action"}]
        self.writes = []

        async def service(domain, service, data, **kwargs):
            if domain == "todo" and service == "get_items":
                return {"todo.shopping_list": {"items": self.todo}}
            if domain == "ai_task":
                self.assertIn("Огурцы 3шт", data["instructions"])
                return {"data": {"matches_json": json.dumps([
                    {"item_index": 0, "shopping_id": -1},
                    {"item_index": 0, "shopping_id": -999},
                ])}}
            self.writes.append(data)

        async def rows(kind):
            return {"products": [], "shopping_list": [], "locations": [{"id": 1, "name": "Fridge"}],
                    "quantity_units": [{"id": 1}]}[kind]

        self.grocy = SimpleNamespace(async_list=rows, async_create=AsyncMock(return_value=12),
                                     _request=AsyncMock(), async_delete=AsyncMock())
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: object()),
                               services=SimpleNamespace(async_call=service),
                               data={"hisense_food_manager": {"entry": {"grocy": self.grocy}}})
        self.inventory = module.InventoryAssistant(hass)
        self.item = {"name": "Огірок колючий", "amount": 2, "location": "Fridge"}

    async def test_native_list_match_and_complete_after_stock_write(self):
        items = await self.inventory._match_photo_shopping([self.item])
        self.assertEqual(items[0]["shopping_match"]["uid"], "cucumber-uid")
        self.assertEqual(self.writes, [])  # Preparing the draft never edits the list.
        added, removed = await self.inventory.async_add_stock(items)
        self.assertEqual(removed, ["Огурцы 3шт"])
        self.assertEqual(self.writes, [{"entity_id": "todo.shopping_list", "item": "cucumber-uid", "status": "completed"}])
        self.grocy._request.assert_awaited_once()

    async def test_changed_row_is_not_completed(self):
        items = await self.inventory._match_photo_shopping([self.item])
        self.todo[0]["summary"] = "Маринованные огурцы"
        _, removed = await self.inventory.async_add_stock(items)
        self.assertEqual(removed, [])
        self.assertEqual(self.writes, [])

    async def test_failed_stock_write_keeps_shopping_row(self):
        items = await self.inventory._match_photo_shopping([self.item])
        self.grocy._request.side_effect = RuntimeError("Grocy unavailable")
        with self.assertRaises(RuntimeError):
            await self.inventory.async_add_stock(items)
        self.assertEqual(self.writes, [])


    async def test_purchase_caption_prepares_draft_without_conversation(self):
        # Run the real method without importing HA's LLM runtime.
        source = Path(__file__).parents[1] / "custom_components/telegram_access_registrar/assist.py"
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelegramAssist")
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_reply")
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])), str(source), "exec"), namespace)
        api = SimpleNamespace(async_download_file=AsyncMock(return_value=(b"photo", "image/jpeg")))
        inventory = SimpleNamespace(async_propose_photo_purchase=AsyncMock(return_value=([self.item], "photo.jpg")))
        assistant = SimpleNamespace(api=api, inventory=inventory, pending_photos={})
        for caption in ("Купила", "купил!", "КУПИЛИ."):
            draft = AsyncMock()
            result = await namespace["async_reply"](assistant, 123, caption, photo_id="file", pending_items=None, show_draft=draft)
            self.assertTrue(result)
            draft.assert_awaited_once()
        api.async_download_file.assert_awaited()


if __name__ == "__main__":
    unittest.main()
