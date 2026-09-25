"""Diagnostics: what the Director reports for lights and covers, as Home Assistant sees it.

Settings → Devices & services → Control4 → ⋮ → Download diagnostics. Built to answer
questions that only a live Director can: e.g. which variables a relay (switch) light
exposes over REST - the dimmer/switch decision in light.py hinges on them.
"""

import asyncio
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import Control4ConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_TOKEN}
CATEGORIES = ("lights", "blinds_shades")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Control4ConfigEntry
) -> dict[str, Any]:
    """Return item info and raw variables of every light and cover item."""
    runtime_data = entry.runtime_data
    items = [
        item
        for item in runtime_data.director_all_items
        if set(item.get("categories") or []) & set(CATEGORIES)
    ]

    async def variables(item_id: int) -> Any:
        try:
            return await runtime_data.director.get_item_variables(item_id)
        except Exception as err:  # noqa: BLE001 - diagnostics must not fail on one item
            return f"error: {type(err).__name__}: {err}"

    results = await asyncio.gather(*(variables(item["id"]) for item in items))
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "director": {
            "model": runtime_data.director_model,
            "sw_version": runtime_data.director_sw_version,
        },
        "items": [
            {"item": item, "variables": result}
            for item, result in zip(items, results, strict=True)
        ],
    }
