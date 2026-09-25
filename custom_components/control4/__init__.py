"""The Control4 integration."""

from datetime import datetime, timedelta
import functools
import logging
import random
from typing import Any

from aiohttp import client_exceptions
from pyControl4.director import C4Director
from pyControl4.error_handling import BadToken, C4Exception, InvalidCategory

from . import token_store
from .director_websocket import DirectorWebsocket

from homeassistant.const import (
    CONF_HOST,
    CONF_TOKEN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, device_registry as dr
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    CONF_CONTROLLER_UNIQUE_ID,
    DOMAIN,
    TOKEN_REFRESH_WINDOW_SEC,
    TOKEN_RETRY_FIRST_SEC,
    TOKEN_RETRY_MAX_SEC,
    WEBSOCKET_RESYNC_INTERVAL_SEC,
    Control4ConfigEntry,
    Control4RuntimeData,
)
from .director_utils import update_variables_for_config_entry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.COVER, Platform.LIGHT, Platform.MEDIA_PLAYER]


def _director(hass: HomeAssistant, entry: Control4ConfigEntry, token: str) -> C4Director:
    return C4Director(
        entry.data[CONF_HOST],
        token,
        aiohttp_client.async_get_clientsession(hass, verify_ssl=False),
    )


def _schedule_next_refresh(hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
    """Schedule the token refresh and, once, the periodic resync poll.

    The refresh starts TOKEN_REFRESH_WINDOW_SEC before the saved token expires, leaving
    that much time for retries when the Control4 cloud can't be reached.
    """
    runtime_data = entry.runtime_data
    if runtime_data.cancel_token_refresh_callback is not None:
        runtime_data.cancel_token_refresh_callback()
    delay = max(token_store.seconds_left(entry) - TOKEN_REFRESH_WINDOW_SEC, 0)
    runtime_data.cancel_token_refresh_callback = async_call_later(
        hass=hass, delay=delay, action=RefreshTokensObject(hass, entry).refresh_tokens
    )
    # Only needed once, on initial setup.
    if runtime_data.cancel_periodic_resync_callback is None:
        runtime_data.cancel_periodic_resync_callback = async_track_time_interval(
            hass,
            functools.partial(_periodic_resync, hass, entry),
            timedelta(seconds=WEBSOCKET_RESYNC_INTERVAL_SEC),
        )


async def async_setup_entry(hass: HomeAssistant, entry: Control4ConfigEntry) -> bool:
    """Set up Control4 from a config entry.

    Starts from the token saved in the entry when it has life left, so a restart needs
    only the Director; the cloud is asked when there is no usable token or the Director
    rejects the saved one (see token_store).
    """
    token = token_store.stored_token(entry)
    if token is None:
        account, token_dict = await token_store.fetch_cloud_token(hass, entry)
        token = token_dict[CONF_TOKEN]
    else:
        account = token_store.account_for(hass, entry)
        _LOGGER.debug(
            "Starting with the saved director token (%.1f h left)",
            token_store.seconds_left(entry) / 3600,
        )
    director = _director(hass, entry, token)

    if hasattr(entry, "runtime_data"):
        # A retry of a previously-failed setup reuses that attempt's WebSocket.
        runtime_data = entry.runtime_data
        runtime_data.account = account
        runtime_data.director = director
    else:
        connection_tracker = C4WebsocketConnectionTracker(hass, entry)
        websocket = DirectorWebsocket(
            entry.data[CONF_HOST],
            aiohttp_client.async_get_clientsession(hass, verify_ssl=False),
            connection_tracker.connect_callback,
            connection_tracker.disconnect_callback,
        )
        runtime_data = Control4RuntimeData(
            account=account, director=director, websocket=websocket
        )
        entry.runtime_data = runtime_data

    try:
        controller_unique_id = entry.data[CONF_CONTROLLER_UNIQUE_ID]
        try:
            try:
                director_all_items = await runtime_data.director.get_all_item_info()
            except BadToken:
                _LOGGER.info(
                    "The Director rejected the saved token; getting a new one from"
                    " the Control4 cloud"
                )
                account, token_dict = await token_store.fetch_cloud_token(hass, entry)
                runtime_data.account = account
                runtime_data.director = _director(hass, entry, token_dict[CONF_TOKEN])
                director_all_items = await runtime_data.director.get_all_item_info()
        except (TimeoutError, client_exceptions.ClientError, C4Exception) as err:
            raise ConfigEntryNotReady(err) from err

        try:
            await runtime_data.websocket.sio_connect(
                runtime_data.director.director_bearer_token
            )
        except Exception as err:
            raise ConfigEntryNotReady(err) from err

        _schedule_next_refresh(hass, entry)

        director_sw_version = await token_store.director_version(
            hass, entry, runtime_data.account
        )

        _, model, mac_address = controller_unique_id.split("_", 3)
        director_model = model.upper()
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, controller_unique_id)},
            connections={(dr.CONNECTION_NETWORK_MAC, mac_address)},
            manufacturer="Control4",
            name=controller_unique_id,
            model=director_model,
            sw_version=director_sw_version,
        )

        # Control4 OS 2 controllers do not support the UI configuration endpoint.
        ui_configuration = None
        if int(director_sw_version.split(".")[0]) >= 3:
            try:
                ui_configuration = await runtime_data.director.get_ui_configuration()
            except (TimeoutError, client_exceptions.ClientError) as err:
                raise ConfigEntryNotReady(err) from err
    except BaseException:
        # Torn down here since async_unload_entry never runs for a failed setup.
        await runtime_data.websocket.sio_disconnect()
        if runtime_data.cancel_token_refresh_callback is not None:
            runtime_data.cancel_token_refresh_callback()
        if runtime_data.cancel_periodic_resync_callback is not None:
            runtime_data.cancel_periodic_resync_callback()
            # Retried setup reuses this runtime_data; only reschedule when unset.
            runtime_data.cancel_periodic_resync_callback = None
        raise

    # All pieces gathered - fill in the rest of runtime_data now that we have them.
    runtime_data.controller_unique_id = controller_unique_id
    runtime_data.director_sw_version = director_sw_version
    runtime_data.director_model = director_model
    runtime_data.director_all_items = director_all_items
    runtime_data.ui_configuration = ui_configuration

    # Platform setup failures are caught inside HA's own entity_platform
    # setup, never raised here, so this needs no cleanup block.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Control4ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    runtime_data = entry.runtime_data
    _LOGGER.debug("Disconnecting C4Websocket for config entry unload")
    await runtime_data.websocket.sio_disconnect()
    if runtime_data.cancel_token_refresh_callback is not None:
        _LOGGER.debug("Cancelling scheduled token refresh for config entry unload")
        runtime_data.cancel_token_refresh_callback()
    if runtime_data.cancel_periodic_resync_callback is not None:
        _LOGGER.debug("Cancelling periodic resync poll for config entry unload")
        runtime_data.cancel_periodic_resync_callback()
        # A reload reuses this runtime_data; only reschedule when unset.
        runtime_data.cancel_periodic_resync_callback = None
    return unload_ok


async def get_items_of_category(
    hass: HomeAssistant, entry: Control4ConfigEntry, category: str
) -> list[dict[str, Any]]:
    """Return a list of all Control4 items with the specified category."""
    director = entry.runtime_data.director
    try:
        return await director.get_all_items_by_category(category)
    except InvalidCategory:
        _LOGGER.warning(
            "Category %s does not exist on this Control4 system, "
            "entities from this domain will not be set up",
            category,
        )
        return []


async def refresh_tokens(
    hass: HomeAssistant, entry: Control4ConfigEntry, *, force: bool = False
) -> None:
    """Get a new director token and reconnect the WebSocket with it.

    Without `force`, a saved token that is still outside the refresh window is reused -
    the retry after a failed WebSocket reconnect must not go to the cloud again. `force`
    is for the Director having rejected the current token (BadToken).
    """
    runtime_data = entry.runtime_data
    if not force and token_store.seconds_left(entry) > TOKEN_REFRESH_WINDOW_SEC:
        token = entry.data[CONF_TOKEN]
    else:
        account, token_dict = await token_store.fetch_cloud_token(hass, entry)
        runtime_data.account = account
        token = token_dict[CONF_TOKEN]
    runtime_data.director = _director(hass, entry, token)

    try:
        await runtime_data.websocket.sio_connect(token)
    except Exception as err:
        raise ConfigEntryNotReady(err) from err

    _schedule_next_refresh(hass, entry)


async def _resync_items(hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
    """Re-fetch and push current variable state for every WebSocket-subscribed item.

    One bulk request for the variables the platforms use (see
    fetch_initial_variables), not a request per item.
    """
    runtime_data = entry.runtime_data
    item_callbacks = runtime_data.websocket.item_callbacks
    if not item_callbacks or not runtime_data.resync_variable_names:
        return
    try:
        variables_by_id = await update_variables_for_config_entry(
            hass, entry, set(runtime_data.resync_variable_names)
        )
    except Exception as err:  # noqa: BLE001 - incl. a failed token refresh; next pass retries
        _LOGGER.warning("Failed to resync Control4 items: %r", err)
        return

    for item_id, callbacks in list(item_callbacks.items()):
        item_attributes = variables_by_id.get(item_id)
        if not item_attributes:
            # No data means removed/offline; don't mark it available with stale attributes.
            # Debug, not warning: parent devices without these variables hit this every pass.
            _LOGGER.debug(
                "Resync for item %s returned no data, leaving unavailable", item_id
            )
            continue
        message = {
            "evtName": "OnDataToUI",
            "iddevice": item_id,
            "data": item_attributes,
        }
        for callback in list(callbacks):
            try:
                await callback(item_id, message)
            except Exception:
                _LOGGER.exception("Error applying resync data for item %s", item_id)


async def _periodic_resync(
    hass: HomeAssistant, entry: Control4ConfigEntry, _now: datetime
) -> None:
    """Safety-net poll to catch any WebSocket push events that were missed."""
    resync_lock = entry.runtime_data.resync_lock
    if resync_lock.locked():
        _LOGGER.debug("Skipping periodic Control4 resync: previous pass still running")
        return
    async with resync_lock:
        await _resync_items(hass, entry)


class C4WebsocketConnectionTracker:
    """Refresh entity states on WebSocket reconnect and mark entities unavailable on disconnect."""

    def __init__(self, hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self._was_disconnected = False

    async def connect_callback(self) -> None:
        """Re-fetch entity state from director after a reconnect."""
        if not self._was_disconnected:
            return
        _LOGGER.info("WebSocket connection to Control4 re-established")
        self._was_disconnected = False
        resync_lock = self.entry.runtime_data.resync_lock
        if resync_lock.locked():
            # An in-progress resync (possibly this same call, re-entered) covers this.
            return
        async with resync_lock:
            await _resync_items(self.hass, self.entry)

    async def disconnect_callback(self) -> None:
        """Mark all entities unavailable on WebSocket disconnect."""
        _LOGGER.warning(
            "WebSocket connection to Control4 lost, attempting reconnection"
        )
        self._was_disconnected = True
        item_callbacks = self.entry.runtime_data.websocket.item_callbacks
        for item_id, callbacks in list(item_callbacks.items()):
            for callback in list(callbacks):
                await callback(item_id, False)


class RefreshTokensObject:
    """Callable target for async_call_later token refresh with exponential backoff."""

    def __init__(self, hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.retries = 0

    def _schedule_retry(self, delay: float) -> None:
        # Only one pending refresh may exist: unload and the next refresh cancel just this one.
        if (cancel := self.entry.runtime_data.cancel_token_refresh_callback) is not None:
            cancel()
        self.entry.runtime_data.cancel_token_refresh_callback = async_call_later(
            hass=self.hass, delay=delay, action=self.refresh_tokens
        )

    def _retry_later(self, err: BaseException) -> None:
        self.retries += 1
        delay = min(
            TOKEN_RETRY_FIRST_SEC * 2 ** (self.retries - 1), TOKEN_RETRY_MAX_SEC
        ) * random.uniform(0.8, 1.2)
        left = token_store.seconds_left(self.entry)
        if left > 0:
            _LOGGER.warning(
                "Could not refresh the Control4 director token (attempt %d): %s."
                " Next attempt in %.0f min; the current token is valid for %.1f h more",
                self.retries,
                err,
                delay / 60,
                left / 3600,
            )
        else:
            _LOGGER.error(
                "The Control4 director token has EXPIRED and could not be refreshed"
                " (attempt %d): %s. Push updates and commands stop until it is;"
                " next attempt in %.0f min",
                self.retries,
                err,
                delay / 60,
            )
        self._schedule_retry(delay)

    async def refresh_tokens(self, _datetime: Any) -> None:
        """Refresh the token; on failure retry with backoff and say so in the log.

        Every path either reschedules or hands over to reauth: a dropped chain means the
        Director silently stops pushing once the token expires.
        """
        token_refresh_lock = self.entry.runtime_data.token_refresh_lock
        if token_refresh_lock.locked():
            # A BadToken-triggered refresh is running. On success it cancels this retry
            # and schedules the next refresh itself; if it fails, the retry takes over.
            self._schedule_retry(TOKEN_RETRY_FIRST_SEC)
            return
        async with token_refresh_lock:
            try:
                await refresh_tokens(self.hass, self.entry)
            except ConfigEntryAuthFailed:
                _LOGGER.error(
                    "Control4 rejected the account credentials; the director token"
                    " was not refreshed. Enter the new password in the repair prompt"
                )
                self.entry.async_start_reauth(self.hass)
            except Exception as err:  # noqa: BLE001 - anything else: keep the chain alive
                self._retry_later(err)
            else:
                if self.retries:
                    _LOGGER.info(
                        "Control4 director token refreshed after %d failed attempts",
                        self.retries,
                    )
