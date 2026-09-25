"""Test Control4 Media Player."""

from unittest.mock import patch

import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import setup_integration

from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms which should be loaded during the test."""
    return [Platform.MEDIA_PLAYER]


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director", "mock_update_variables")
async def test_media_player_with_and_without_sources(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test that rooms with sources create entities and rooms without are skipped."""
    # The default mock_c4_director fixture provides multi-room data:
    # Room 1 has video source, Room 2 has no sources (thermostat-only room)
    await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director")
@pytest.mark.parametrize(
    ("room_data", "state", "volume"),
    [({1: {"POWER_STATE": True}}, "on", None), ({}, "idle", None), (None, "unavailable", None)],
    ids=["нет_переменных", "нет_комнаты", "первая_загрузка_не_удалась"],
)
async def test_комната_без_переменных_не_ломает_плеер(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, room_data, state, volume
) -> None:
    """Директор отдаёт только те переменные, что есть у комнаты; первая загрузка может упасть."""

    async def update_variables(*args, **kwargs):
        if room_data is None:
            raise TimeoutError
        return room_data

    with patch(
        "custom_components.control4.media_player.update_variables_for_config_entry",
        new=update_variables,
    ):
        await setup_integration(hass, mock_config_entry)

    entity = hass.states.get("media_player.living_room")
    assert entity is not None
    assert entity.state == state
    assert entity.attributes.get("volume_level") is volume
