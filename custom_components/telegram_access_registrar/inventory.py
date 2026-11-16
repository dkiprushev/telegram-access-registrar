"""Read Grocy and ask the configured AI Task to answer inventory questions."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class InventoryAssistant:
    """A deliberately read-only bridge between Telegram, Grocy and AI Task."""

    def __init__(self, hass) -> None:
        self._hass = hass

    def _grocy(self):
        runtimes = list(self._hass.data.get("hisense_food_manager", {}).values())
        if len(runtimes) != 1 or "grocy" not in runtimes[0]:
            raise ValueError("Интеграция Grocy ConnectLife Sync не готова")
        return runtimes[0]["grocy"]

    async def async_propose_purchase(self, text: str) -> list[dict[str, Any]]:
        """Extract a small, reviewable stock-add draft; never write Grocy here."""
        grocy = self._grocy()
        locations = await grocy.async_list("locations")
        names = [str(row["name"]) for row in locations if row.get("name")]
        result = await self._hass.services.async_call(
            "ai_task", "generate_data",
            {"entity_id": "ai_task.codex_assist_ai_task", "task_name": "Purchase draft", "instructions": (
                "Extract purchased household goods from this Russian message. Return a JSON array only, "
                "each object with name (string), amount (positive number), location (one exact provided location). "
                "Use amount 1 if absent. Choose a cold-storage location for food only when an appropriate location exists; "
                "otherwise use the first provided location. Do not add anything not explicitly bought. "
                f"Locations: {json.dumps(names, ensure_ascii=False)}. Message: {text}"),
             "structure": {"items_json": {"description": "JSON array of purchase items", "required": True, "selector": {"text": {}}}}},
            blocking=True, return_response=True,
        )
        raw = result.get("data", {}).get("items_json") if isinstance(result, dict) else None
        try:
            items = json.loads(raw) if isinstance(raw, str) else []
        except json.JSONDecodeError as err:
            raise ValueError("Не удалось разобрать список покупок") from err
        valid = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("location") not in names:
                continue
            try: amount = float(item.get("amount", 1))
            except (TypeError, ValueError): continue
            if 0 < amount <= 10000 and item["name"].strip():
                valid.append({"name": item["name"].strip()[:120], "amount": amount, "location": item["location"]})
        return valid[:30]

    async def async_add_stock(self, items: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Write the confirmed draft, then remove matching active shopping rows."""
        grocy = self._grocy()
        products, locations, units = await asyncio.gather(grocy.async_list("products"), grocy.async_list("locations"), grocy.async_list("quantity_units"))
        if not units: raise ValueError("В Grocy нет единицы измерения")
        location_ids = {str(row["name"]): int(row["id"]) for row in locations if row.get("id") is not None}
        product_ids = {str(row["name"]).casefold(): int(row["id"]) for row in products if row.get("id") is not None and row.get("name")}
        added, purchased_ids = [], set()
        for item in items:
            location_id = location_ids[item["location"]]
            product_id = product_ids.get(item["name"].casefold())
            if product_id is None:
                product_id = await grocy.async_create("products", {"name": item["name"], "location_id": location_id, "qu_id_purchase": units[0]["id"], "qu_id_stock": units[0]["id"], "min_stock_amount": 0})
                product_ids[item["name"].casefold()] = product_id
            await grocy._request("POST", f"/api/stock/products/{product_id}/add", json={"amount": item["amount"], "location_id": location_id})
            purchased_ids.add(product_id)
            added.append(f"{item['name']} — {item['amount']:g} ({item['location']})")
        shopping = await grocy.async_list("shopping_list")
        removed = []
        for row in shopping:
            if row.get("product_id") in purchased_ids and not (row.get("done") or row.get("done_date")) and row.get("id") is not None:
                await grocy.async_delete("shopping_list", int(row["id"]))
                removed.append(names.get(int(row["product_id"]), str(row["product_id"])))
        return added, removed

    async def async_answer(self, question: str) -> str:
        grocy = self._grocy()
        products, locations, units, stock, shopping = await asyncio.gather(
            grocy.async_list("products"), grocy.async_list("locations"),
            grocy.async_list("quantity_units"), grocy.async_list("stock"),
            grocy.async_list("shopping_list"),
        )
        names = {int(row["id"]): str(row["name"]) for row in products if row.get("id") is not None and row.get("name")}
        places = {int(row["id"]): str(row["name"]) for row in locations if row.get("id") is not None and row.get("name")}
        unit_names = {int(row["id"]): str(row["name"]) for row in units if row.get("id") is not None and row.get("name")}
        inventory = []
        for row in stock:
            product_id, location_id = row.get("product_id"), row.get("location_id")
            amount = float(row.get("amount") or 0)
            if amount > 0 and product_id in names and location_id in places:
                inventory.append({"name": names[product_id], "amount": amount, "unit": unit_names.get(row.get("qu_id"), ""), "location": places[location_id]})
        shopping_items = [
            {"name": names.get(row.get("product_id"), ""), "amount": row.get("amount"), "done": bool(row.get("done") or row.get("done_date"))}
            for row in shopping if row.get("product_id") in names
        ]
        context = json.dumps({"inventory": inventory[:500], "shopping_list": shopping_items[:250]}, ensure_ascii=False, separators=(",", ":"))
        instructions = (
            "You are a home inventory assistant. Answer in Russian, briefly and helpfully. "
            "Use ONLY the supplied Grocy snapshot: never invent stock. Include location and quantity when useful. "
            "If requested item is absent, say so, then mention only genuinely relevant alternatives from the snapshot. "
            "This is read-only: do not claim to add, remove, reserve, or buy anything.\n"
            f"Question: {question}\nGrocy snapshot: {context}"
        )
        result = await self._hass.services.async_call(
            "ai_task", "generate_data",
            {"entity_id": "ai_task.codex_assist_ai_task", "task_name": "Grocy inventory answer", "instructions": instructions,
             "structure": {"reply": {"description": "Short Russian answer grounded only in the snapshot", "required": True, "selector": {"text": {}}}}},
            blocking=True, return_response=True,
        )
        reply = result.get("data", {}).get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Codex Assist did not return an answer")
        return reply.strip()
