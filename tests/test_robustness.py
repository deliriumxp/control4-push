"""Сбои, после которых интеграция должна продолжать работать сама (находки ревью)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.control4 import RefreshTokensObject, _resync_items
from custom_components.control4.const import DOMAIN
from custom_components.control4.director_websocket import _DirectorSocketIOClient
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import setup_integration


@pytest.fixture
def platforms() -> list[Platform]:
    return []


async def test_сверка_не_обрывается_на_сбойном_элементе(hass: HomeAssistant) -> None:
    """Сбой загрузки (в т.ч. неудачная смена токена) и сбой записи состояния — только у своего элемента."""
    received: list[int] = []

    async def ok(item_id, message):
        received.append(item_id)

    async def broken(item_id, message):
        raise RuntimeError("fan_mode.lower() on a non-string")

    entry = MagicMock()
    entry.runtime_data.websocket.item_callbacks = {1: [ok], 2: [broken, ok], 3: [ok]}

    async def variables(hass, entry, item_id):
        if item_id == 1:
            raise ConfigEntryNotReady("token refresh failed")
        return {"LIGHT_LEVEL": 50}

    with patch("custom_components.control4.director_get_entry_variables", new=variables):
        await _resync_items(hass, entry)

    assert sorted(received) == [2, 3]


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director")
@pytest.mark.parametrize("error", [KeyError("validSeconds"), RuntimeError("cloud 500")])
async def test_неожиданная_ошибка_смены_токена_не_рвёт_цепочку(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, error: Exception
) -> None:
    await setup_integration(hass, mock_config_entry)
    runtime_data = mock_config_entry.runtime_data
    scheduled = runtime_data.cancel_token_refresh_callback

    with patch("custom_components.control4.refresh_tokens", new=AsyncMock(side_effect=error)):
        await RefreshTokensObject(hass, mock_config_entry).refresh_tokens(dt_util.utcnow())

    assert runtime_data.cancel_token_refresh_callback is not None
    assert runtime_data.cancel_token_refresh_callback is not scheduled
    runtime_data.cancel_token_refresh_callback()


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director")
async def test_неверный_пароль_при_смене_токена_запускает_повторный_вход(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await setup_integration(hass, mock_config_entry)

    with patch(
        "custom_components.control4.refresh_tokens",
        new=AsyncMock(side_effect=ConfigEntryAuthFailed("bad password")),
    ):
        await RefreshTokensObject(hass, mock_config_entry).refresh_tokens(dt_util.utcnow())
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.usefixtures("mock_c4_account", "mock_c4_director", "mock_setup_entry")
async def test_повторный_вход_обновляет_пароль_в_той_же_записи(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "test-username", CONF_PASSWORD: "new"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new"


@pytest.mark.usefixtures("mock_c4_director", "mock_setup_entry")
async def test_повторный_вход_чужим_контроллером_отклоняется(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_c4_account.get_account_controllers.return_value = {
        "controllerCommonName": "control4_model_00BB00BB00BB",
        "href": "https://example",
    }
    result = await mock_config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "other", CONF_PASSWORD: "x"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_controller"
    assert mock_config_entry.data[CONF_PASSWORD] == "test-password"


async def test_отключение_раньше_старта_цикла_переподключения(hass: HomeAssistant) -> None:
    """Цикл socketio начинает с clear() флага остановки — отключение до его старта не должно потеряться."""
    client = _DirectorSocketIOClient(connector=MagicMock())
    client.connection_url = "wss://director"
    client.connect = AsyncMock()
    client._reconnect_task = client.start_background_task(client._handle_reconnect)

    await client.disconnect()
    await hass.async_block_till_done()

    client.connect.assert_not_awaited()
