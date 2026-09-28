"""Constants for the Control4 integration."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pyControl4.account import C4Account
from pyControl4.director import C4Director
from .director_websocket import DirectorWebsocket

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE

DOMAIN = "control4"


@dataclass
class Control4RuntimeData:
    """Runtime data for a Control4 config entry; fields past account/director/websocket are set once during async_setup_entry."""

    account: C4Account
    director: C4Director
    websocket: DirectorWebsocket
    controller_unique_id: str = ""
    director_sw_version: str = ""
    director_model: str = ""
    director_all_items: list[dict[str, Any]] = field(default_factory=list)
    ui_configuration: dict[str, Any] | None = None
    cancel_token_refresh_callback: CALLBACK_TYPE | None = None
    cancel_periodic_resync_callback: CALLBACK_TYPE | None = None
    token_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    resync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Variable names the platforms read; the periodic resync re-reads them in one request.
    resync_variable_names: set[str] = field(default_factory=set)
    # What async_setup_entry actually forwarded. Unload must use this, not the
    # options: the options flow saves new options *before* its reload unloads, so
    # a just-enabled platform would be "unloaded" without ever having been loaded.
    platforms: list[Platform] = field(default_factory=list)


type Control4ConfigEntry = ConfigEntry[Control4RuntimeData]

CONF_CONTROLLER_UNIQUE_ID = "controller_unique_id"

CONTROL4_ENTITY_TYPE = 7
CONTROL4_COVER_CATEGORY = "blinds_shades"

# Entity types a household imports: lights/climate may already come from KNX etc.
CONF_ENABLED_PLATFORMS = "enabled_platforms"

# Item IDs (as strings, for JSON/options-flow storage) of covers that are
# driven by a dry contact / relay with no real position feedback. For these,
# Control4's reported status is shown but must never gate the open/close
# buttons - see Control4Cover.assumed_state in cover.py.
CONF_DRY_CONTACT_COVERS = "dry_contact_covers"

# value -> display label, in the order shown in the flows.
AVAILABLE_PLATFORMS = {
    Platform.COVER.value: "Covers (blinds/shades)",
    Platform.LIGHT.value: "Lights",
    Platform.CLIMATE.value: "Climate",
    Platform.MEDIA_PLAYER.value: "Media players",
}

# All of them, as the built-in integration: an entry installed over it (no options
# yet) keeps every entity it had.
DEFAULT_ENABLED_PLATFORMS = list(AVAILABLE_PLATFORMS)

# Director token lifecycle (token_store.py). The token lives validSeconds (24 h).
CONF_TOKEN_EXPIRES = "token_expires"
CONF_DIRECTOR_SW_VERSION = "director_sw_version"
# Start with a saved token unless it expires within this margin (see stored_token).
MIN_STORED_TOKEN_LIFE_SEC = 60
# Refresh starts this long before expiry: half the token's life is left for retries,
# because access to Control4 resources from Russia can be down for hours.
TOKEN_REFRESH_WINDOW_SEC = 12 * 3600
# Failed refresh: retry after 1, 2, 4 ... minutes, then every 30 minutes until it works.
TOKEN_RETRY_FIRST_SEC = 60
TOKEN_RETRY_MAX_SEC = 1800
CLOUD_REQUEST_TIMEOUT_SEC = 30

DEFAULT_SCAN_INTERVAL = 5
WEBSOCKET_RESYNC_INTERVAL_SEC = 60
