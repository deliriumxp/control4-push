"""Provides data updates from the Control4 controller for platforms."""

from collections import defaultdict
from collections.abc import Callable, Coroutine
import logging
from typing import Any

from aiohttp import ClientError
from pyControl4.error_handling import BadToken

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady

from .const import Control4ConfigEntry

_LOGGER = logging.getLogger(__name__)

_TRUE_STRINGS = {"true", "1"}
_FALSE_STRINGS = {"false", "0"}


def to_bool(value: Any) -> bool | None:
    """Normalize a Control4 boolean-ish variable that may arrive as a string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        return None
    if value is None:
        return None
    return bool(value)


async def _with_token_refresh[T](
    hass: HomeAssistant,
    entry: Control4ConfigEntry,
    call: Callable[[], Coroutine[Any, Any, T]],
) -> T:
    """Call `call`, refreshing the director token once on BadToken."""
    try:
        return await call()
    except BadToken:
        pass

    async with entry.runtime_data.token_refresh_lock:
        try:
            return await call()
        except BadToken:
            _LOGGER.debug("Updating Control4 director token")
            from . import refresh_tokens  # noqa: PLC0415

            await refresh_tokens(hass, entry)

    return await call()


async def _update_variables_for_config_entry(
    entry: Control4ConfigEntry, variable_names: set[str]
) -> dict[int, dict[str, Any]]:
    director = entry.runtime_data.director
    try:
        data = await director.get_all_item_variable_value(variable_names)
    except ValueError:
        # pyControl4 raises on "[]": no item has any of these variables.
        return {}
    result_dict: defaultdict[int, dict[str, Any]] = defaultdict(dict)
    for item in data:
        result_dict[item["id"]][item["varName"]] = item["value"]
    return dict(result_dict)


async def update_variables_for_config_entry(
    hass: HomeAssistant, entry: Control4ConfigEntry, variable_names: set[str]
) -> dict[int, dict[str, Any]]:
    """Retrieve data from the Control4 director."""
    return await _with_token_refresh(
        hass, entry, lambda: _update_variables_for_config_entry(entry, variable_names)
    )


async def fetch_initial_variables(
    hass: HomeAssistant,
    entry: Control4ConfigEntry,
    variable_names: frozenset[str],
    item_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Variables of a platform's items, in ONE request to the Director.

    One bulk `/api/v1/items/variables?varnames=` call per platform, not a request
    per item: per-item fetching opened a TLS connection per item at startup and a
    freshly restarted Director timed every one of them out (seen on hardware:
    ~80 lights, all gone until the next restart). If the Director doesn't answer,
    the platform is not ready and HA retries its setup with backoff.

    The names are also registered for the periodic resync, which re-reads them
    for every subscribed item in one request as well.
    """
    entry.runtime_data.resync_variable_names.update(variable_names)
    try:
        variables_by_id = await update_variables_for_config_entry(
            hass, entry, set(variable_names)
        )
    except (TimeoutError, ClientError) as err:
        raise PlatformNotReady(
            f"Control4 Director did not return item variables: {err!r}"
        ) from err
    return {item_id: variables_by_id.get(item_id, {}) for item_id in item_ids}
