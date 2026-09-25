"""Test Control4 integration setup and core behaviors."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pyControl4.error_handling import BadToken
from aiohttp import ClientError
import pytest

from custom_components.control4 import RefreshTokensObject, _periodic_resync
from custom_components.control4.director_utils import (
    fetch_initial_variables,
    to_bool,
    update_variables_for_config_entry,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import setup_integration

from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture
def platforms() -> list[Platform]:
    """No platforms needed to test core integration behavior."""
    return []


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director")
async def test_periodic_resync_skips_when_already_running(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A periodic resync tick is skipped while a previous pass is still running."""
    await setup_integration(hass, mock_config_entry)

    resync_started = asyncio.Event()
    release_resync = asyncio.Event()

    async def _slow_resync_items(hass: HomeAssistant, entry: MockConfigEntry) -> None:
        resync_started.set()
        await release_resync.wait()

    with patch(
        "custom_components.control4._resync_items",
        new=AsyncMock(side_effect=_slow_resync_items),
    ) as mock_resync:
        first_tick = hass.async_create_task(
            _periodic_resync(hass, mock_config_entry, dt_util.utcnow()),
            "test first resync tick",
        )
        await resync_started.wait()

        await _periodic_resync(hass, mock_config_entry, dt_util.utcnow())
        mock_resync.assert_called_once()

        release_resync.set()
        await first_tick


@pytest.mark.usefixtures("mock_c4_account")
async def test_concurrent_bad_token_only_refreshes_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_director: MagicMock,
) -> None:
    """Concurrent BadToken hits (two bulk requests) serialize and refresh once."""
    await setup_integration(hass, mock_config_entry)

    token_valid = False
    sync_count = 0
    # Forces both initial calls to hit BadToken together, not one-then-other.
    both_calls_started = asyncio.Barrier(2)

    async def _get_all_item_variable_value(names) -> list[dict]:
        nonlocal sync_count
        if token_valid:
            return [{"id": 1, "varName": "LIGHT_LEVEL", "value": 1}]
        if sync_count < 2:
            sync_count += 1
            await both_calls_started.wait()
        raise BadToken("expired")

    async def _fake_refresh_tokens(
        hass: HomeAssistant, entry: MockConfigEntry, *, force: bool = False
    ) -> None:
        nonlocal token_valid
        token_valid = True

    mock_c4_director.get_all_item_variable_value = AsyncMock(
        side_effect=_get_all_item_variable_value
    )

    with patch(
        "custom_components.control4.refresh_tokens",
        new=AsyncMock(side_effect=_fake_refresh_tokens),
    ) as mock_refresh:
        await asyncio.wait_for(
            asyncio.gather(
                update_variables_for_config_entry(hass, mock_config_entry, {"LIGHT_LEVEL"}),
                update_variables_for_config_entry(hass, mock_config_entry, {"LIGHT_STATE"}),
            ),
            timeout=5,
        )
        mock_refresh.assert_awaited_once()


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director")
async def test_scheduled_refresh_skips_when_lock_already_held(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A scheduled refresh must not race a BadToken-triggered one held by another task.

    It must not drop the chain either: it schedules a retry, which the other refresh
    cancels on success and which takes over if that refresh fails.
    """
    await setup_integration(hass, mock_config_entry)

    lock = mock_config_entry.runtime_data.token_refresh_lock
    holder_has_lock = asyncio.Event()
    release_holder = asyncio.Event()

    async def _hold_lock() -> None:
        async with lock:
            holder_has_lock.set()
            await release_holder.wait()

    holder_task = hass.async_create_task(_hold_lock(), "test lock holder")
    await holder_has_lock.wait()

    with patch(
        "custom_components.control4.refresh_tokens", new=AsyncMock()
    ) as mock_refresh:
        obj = RefreshTokensObject(hass, mock_config_entry)
        await obj.refresh_tokens(dt_util.utcnow())
        mock_refresh.assert_not_called()

    retry = mock_config_entry.runtime_data.cancel_token_refresh_callback
    assert retry is not None
    retry()

    release_holder.set()
    await holder_task


@pytest.mark.usefixtures("mock_c4_account")
async def test_начальная_загрузка_одним_запросом(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_director: MagicMock,
) -> None:
    """Переменные всех элементов платформы — один массовый запрос, не запрос на элемент."""
    await setup_integration(hass, mock_config_entry)
    mock_c4_director.get_item_variables.reset_mock()
    mock_c4_director.get_all_item_variable_value = AsyncMock(
        return_value=[
            {"id": 200, "varName": "LIGHT_LEVEL", "value": 50},
            {"id": 300, "varName": "LIGHT_STATE", "value": 1},
            {"id": 999, "varName": "LIGHT_LEVEL", "value": 7},
        ]
    )

    result = await fetch_initial_variables(
        hass, mock_config_entry, frozenset({"LIGHT_LEVEL", "LIGHT_STATE"}), [100, 200, 300]
    )

    assert result == {100: {}, 200: {"LIGHT_LEVEL": 50}, 300: {"LIGHT_STATE": 1}}
    mock_c4_director.get_all_item_variable_value.assert_awaited_once()
    mock_c4_director.get_item_variables.assert_not_awaited()
    assert {"LIGHT_LEVEL", "LIGHT_STATE"} <= mock_config_entry.runtime_data.resync_variable_names


@pytest.mark.usefixtures("mock_c4_account")
@pytest.mark.parametrize("error", [TimeoutError(), ClientError("reset")])
async def test_директор_не_ответил_при_старте_платформа_не_готова(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_director: MagicMock,
    error: Exception,
) -> None:
    """С объекта: директор после перезапуска не ответил за 10 с — HA повторит, а не потеряет свет."""
    await setup_integration(hass, mock_config_entry)
    mock_c4_director.get_all_item_variable_value = AsyncMock(side_effect=error)

    with pytest.raises(PlatformNotReady):
        await fetch_initial_variables(
            hass, mock_config_entry, frozenset({"LIGHT_LEVEL"}), [100]
        )


@pytest.mark.usefixtures("mock_c4_account")
async def test_ни_у_кого_нет_переменных_пустой_результат(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_director: MagicMock,
) -> None:
    """pyControl4 бросает ValueError на «[]» — это «нет таких элементов», а не сбой."""
    await setup_integration(hass, mock_config_entry)
    mock_c4_director.get_all_item_variable_value = AsyncMock(side_effect=ValueError("[]"))

    result = await fetch_initial_variables(
        hass, mock_config_entry, frozenset({"Level"}), [100]
    )

    assert result == {100: {}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        (" true ", True),
        ("garbage", None),
        ("", None),
        (None, None),
        (1, True),
        (0, False),
    ],
)
def test_to_bool(value: object, expected: bool | None) -> None:
    """to_bool normalizes both real booleans and Control4's string encoding."""
    assert to_bool(value) is expected
