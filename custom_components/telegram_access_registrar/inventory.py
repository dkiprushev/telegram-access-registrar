"""Read Grocy and ask the configured AI Task to answer inventory questions."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class InventoryAssistant:
    """A deliberately read-only bridge between Telegram, Grocy and AI Task."""

    def __init__(self, hass) -> None:
        self._hass = hass

    async def async_answer(self, question: str) -> str:
        runtimes = list(self._hass.data.get("hisense_food_manager", {}).values())
        if len(runtimes) != 1 or "grocy" not in runtimes[0]:
            raise ValueError("Интеграция Grocy ConnectLife Sync не готова")
        grocy = runtimes[0]["grocy"]
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
