"""Токен директора: хранится в записи, перезапуск без облака, обновление с повторами и журналом."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ServerDisconnectedError
from freezegun.api import FrozenDateTimeFactory
from pyControl4.error_handling import BadToken
import pytest

from custom_components.control4 import RefreshTokensObject
from custom_components.control4.const import (
    CONF_DIRECTOR_SW_VERSION,
    CONF_TOKEN_EXPIRES,
    TOKEN_REFRESH_WINDOW_SEC,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from . import setup_integration
from .conftest import MOCK_HOST


@pytest.fixture
def platforms() -> list[Platform]:
    return []


def _with_saved_token(entry: MockConfigEntry, seconds_left: float) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data={
            **entry.data,
            CONF_TOKEN: "saved",
            CONF_TOKEN_EXPIRES: time.time() + seconds_left,
            CONF_DIRECTOR_SW_VERSION: "3.2.0",
        },
    )


@pytest.mark.usefixtures("mock_c4_director")
async def test_первый_запуск_берёт_токен_в_облаке_и_сохраняет(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_c4_account.get_director_bearer_token.assert_awaited_once()
    assert mock_config_entry.data[CONF_TOKEN] == "test"
    assert mock_config_entry.data[CONF_TOKEN_EXPIRES] == pytest.approx(
        time.time() + 86400, abs=60
    )
    assert mock_config_entry.data[CONF_DIRECTOR_SW_VERSION] == "3.2.0"


async def test_перезапуск_с_сохранённым_токеном_не_ходит_в_облако(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    mock_c4_director: MagicMock,
) -> None:
    """Главный сценарий: облако с объекта недоступно, а перезапуск HA ничего не ломает."""
    entry = _with_saved_token(mock_config_entry, seconds_left=20 * 3600)
    mock_c4_account.get_account_bearer_token.side_effect = TimeoutError
    mock_c4_account.get_director_bearer_token.side_effect = TimeoutError

    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    mock_c4_account.get_account_bearer_token.assert_not_awaited()
    mock_c4_account.get_director_bearer_token.assert_not_awaited()
    mock_c4_account.get_controller_os_version.assert_not_awaited()


@pytest.mark.usefixtures("mock_c4_director")
async def test_почти_истёкший_токен_обновляется_при_запуске(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    entry = _with_saved_token(mock_config_entry, seconds_left=10 * 60)

    await setup_integration(hass, entry)

    mock_c4_account.get_director_bearer_token.assert_awaited_once()
    assert entry.data[CONF_TOKEN] == "test"


async def test_директор_отверг_сохранённый_токен_берём_новый(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    mock_c4_director: MagicMock,
) -> None:
    entry = _with_saved_token(mock_config_entry, seconds_left=20 * 3600)
    items = mock_c4_director.get_all_item_info.return_value
    mock_c4_director.get_all_item_info.side_effect = [BadToken("Expired or invalid token"), items]

    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    mock_c4_account.get_director_bearer_token.assert_awaited_once()
    assert entry.data[CONF_TOKEN] == "test"


@pytest.mark.usefixtures("mock_c4_director")
async def test_зависшее_облако_не_держит_запуск_минутами(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    """С объекта: запрос токена висел ~4 минуты. Теперь — таймаут и повтор от HA."""

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    mock_c4_account.get_director_bearer_token.side_effect = hang

    with patch("custom_components.control4.token_store.CLOUD_REQUEST_TIMEOUT_SEC", 0.05):
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("mock_c4_director")
async def test_облако_закрыло_соединение_запрос_повторяется_сразу(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    """С объекта: запрос токена директора 3 раза подряд падал с Server disconnected."""
    mock_c4_account.get_director_bearer_token.side_effect = [
        ServerDisconnectedError(),
        {"token": "test", "validSeconds": 86400},
    ]

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_c4_account.get_director_bearer_token.await_count == 2


@pytest.mark.usefixtures("mock_c4_director")
async def test_облако_рвёт_соединение_дважды_это_повтор_настройки(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_c4_account: MagicMock
) -> None:
    mock_c4_account.get_director_bearer_token.side_effect = ServerDisconnectedError()

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_c4_account.get_director_bearer_token.await_count == 2


async def test_версия_директора_из_брокера_без_облака(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    mock_c4_director: MagicMock,
    aioclient_mock,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://{MOCK_HOST}/api/v1/version",
        json=[{"name": "Navigator", "version": "1"}, {"name": "Director", "version": "4.2.1.757028-res"}],
    )

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.data[CONF_DIRECTOR_SW_VERSION] == "4.2.1.757028-res"
    mock_c4_account.get_controller_os_version.assert_not_awaited()


@pytest.mark.usefixtures("mock_c4_director")
async def test_обновление_стартует_за_12_часов_до_истечения(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_integration(hass, mock_config_entry)
    mock_c4_account.get_director_bearer_token.reset_mock()

    freezer.tick(86400 - TOKEN_REFRESH_WINDOW_SEC - 60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_c4_account.get_director_bearer_token.assert_not_awaited()

    freezer.tick(120)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_c4_account.get_director_bearer_token.assert_awaited_once()


@pytest.mark.usefixtures("mock_c4_director")
async def test_неудачное_обновление_повторяется_и_пишется_в_журнал(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog,
) -> None:
    """РФ: облако недоступно часами — попытки идут с паузой до 30 мин, каждая в журнале."""
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(  # inside the refresh window: 10 h left
        mock_config_entry,
        data={**mock_config_entry.data, CONF_TOKEN_EXPIRES: time.time() + 10 * 3600},
    )
    mock_c4_account.get_director_bearer_token.reset_mock()
    mock_c4_account.get_director_bearer_token.side_effect = TimeoutError

    refresher = RefreshTokensObject(hass, mock_config_entry)
    await refresher.refresh_tokens(dt_util.utcnow())
    assert "Could not refresh the Control4 director token (attempt 1)" in caplog.text
    assert "the current token is valid for" in caplog.text

    for attempt in range(2, 5):
        freezer.tick(40 * 60)  # longer than any backoff step so far
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert f"(attempt {attempt})" in caplog.text
    assert mock_c4_account.get_director_bearer_token.await_count == 4

    mock_c4_account.get_director_bearer_token.side_effect = None
    freezer.tick(40 * 60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "refreshed after 4 failed attempts" in caplog.text
    mock_config_entry.runtime_data.cancel_token_refresh_callback()


@pytest.mark.usefixtures("mock_c4_director")
async def test_истёкший_токен_без_обновления_это_ошибка_в_журнале(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    caplog,
) -> None:
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_TOKEN_EXPIRES: time.time() - 60},
    )
    mock_c4_account.get_director_bearer_token.side_effect = TimeoutError

    await RefreshTokensObject(hass, mock_config_entry).refresh_tokens(dt_util.utcnow())

    assert "director token has EXPIRED" in caplog.text
    assert any(r.levelname == "ERROR" and "EXPIRED" in r.message for r in caplog.records)
    mock_config_entry.runtime_data.cancel_token_refresh_callback()


@pytest.mark.usefixtures("mock_c4_director")
async def test_повтор_после_сбоя_сокета_не_идёт_в_облако(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_account: MagicMock,
    mock_c4_websocket: MagicMock,
) -> None:
    """Токен уже свежий, упало только подключение сокета — повтор переводит сокет, облако не трогает."""
    await setup_integration(hass, mock_config_entry)
    mock_c4_account.get_director_bearer_token.reset_mock()

    await RefreshTokensObject(hass, mock_config_entry).refresh_tokens(dt_util.utcnow())

    mock_c4_account.get_director_bearer_token.assert_not_awaited()
    mock_c4_websocket.rotate_token.assert_awaited_with("test")


@pytest.mark.usefixtures("mock_c4_director", "mock_c4_account")
async def test_обновление_токена_не_рвёт_сокет(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_c4_websocket: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Плановое обновление переводит сокет на новый токен без разрыва, а не пересоздаёт его."""
    await setup_integration(hass, mock_config_entry)
    mock_c4_websocket.sio_connect.reset_mock()

    freezer.tick(86400 - TOKEN_REFRESH_WINDOW_SEC + 60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    mock_c4_websocket.rotate_token.assert_awaited_once_with("test")
    mock_c4_websocket.sio_connect.assert_not_awaited()
    mock_c4_websocket.sio_disconnect.assert_not_awaited()
    mock_config_entry.runtime_data.cancel_token_refresh_callback()
