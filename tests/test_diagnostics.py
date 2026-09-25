"""Диагностика: элементы света и штор с сырыми переменными, без учётных данных."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.control4.diagnostics import async_get_config_entry_diagnostics
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import setup_integration


@pytest.fixture
def platforms() -> list[Platform]:
    return [Platform.LIGHT]


@pytest.mark.usefixtures("mock_c4_account")
async def test_диагностика_отдаёт_переменные_света_без_пароля(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_director: MagicMock
) -> None:
    await setup_integration(hass, mock_config_entry)
    variables = [{"varName": "LIGHT_STATE", "value": 1, "id": 1000}]

    async def get_item_variables(item_id: int):
        if item_id == 346:
            raise TimeoutError
        return variables

    mock_c4_director.get_item_variables = AsyncMock(side_effect=get_item_variables)

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["data"]["username"] == "**REDACTED**"
    by_id = {entry["item"]["id"]: entry for entry in result["items"]}
    assert by_id[345]["variables"] == variables
    assert by_id[346]["variables"].startswith("error: TimeoutError")
    assert 123 not in by_id  # термостат — не свет и не штора
