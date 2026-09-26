"""The director token: kept in the config entry, fetched from the cloud only when needed.

The Director validates its bearer token itself: for the token's whole life (validSeconds,
24 h) nothing but the Director is contacted. So a restart must not throw a live token
away - fetching a new one at every setup made the integration depend on the Control4 cloud
at every restart, and on 2026-09-25 an object's link to that endpoint hung for minutes
(access to Control4 resources from Russia is unreliable) while the Director itself was fine.

What stays in entry.data: the token, its expiry (UTC timestamp) and the Director version.
The account password is already stored there in plain text, so the token adds no new risk.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import Any

from aiohttp import ClientError, ServerDisconnectedError
from pyControl4.account import C4Account
from pyControl4.error_handling import BadCredentials, C4Exception

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from .const import (
    CLOUD_REQUEST_TIMEOUT_SEC,
    CONF_CONTROLLER_UNIQUE_ID,
    CONF_DIRECTOR_SW_VERSION,
    CONF_TOKEN_EXPIRES,
    MIN_STORED_TOKEN_LIFE_SEC,
    Control4ConfigEntry,
)

_LOGGER = logging.getLogger(__name__)


def stored_token(entry: Control4ConfigEntry) -> str | None:
    """The saved director token, if it has enough life left to start with."""
    token = entry.data.get(CONF_TOKEN)
    if token and seconds_left(entry) > MIN_STORED_TOKEN_LIFE_SEC:
        return token
    return None


def seconds_left(entry: Control4ConfigEntry) -> float:
    """Life left on the saved token; negative once expired, 0 if none is saved."""
    expires = entry.data.get(CONF_TOKEN_EXPIRES)
    return expires - time.time() if expires else 0


def save_token(
    hass: HomeAssistant, entry: Control4ConfigEntry, token: str, valid_seconds: int
) -> None:
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_TOKEN: token,
            CONF_TOKEN_EXPIRES: time.time() + valid_seconds,
        },
    )


def account_for(hass: HomeAssistant, entry: Control4ConfigEntry) -> C4Account:
    """An account client; it only goes to the cloud when asked for a token."""
    return C4Account(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        aiohttp_client.async_get_clientsession(hass),
    )


async def _cloud_call[T](call: Callable[[], Awaitable[T]]) -> T:
    """One time-boxed cloud request, repeated once if the server dropped the connection.

    apis.control4.com leaves the connection open after a response, then often closes it
    (TLS close_notify) when the next request arrives on it: 2 of 3 reuses, checked with
    curl on 2026-09-26. curl and browsers resend such a request on a fresh connection;
    aiohttp does it only for idempotent methods, and the token requests are POSTs - so the
    director-token request, reusing the account request's connection, failed with
    ServerDisconnectedError (3 setups in a row on an object). A token request is safe to
    repeat.
    """
    try:
        async with asyncio.timeout(CLOUD_REQUEST_TIMEOUT_SEC):
            return await call()
    except ServerDisconnectedError:
        _LOGGER.debug("Control4 cloud closed the connection; repeating the request")
    async with asyncio.timeout(CLOUD_REQUEST_TIMEOUT_SEC):
        return await call()


async def fetch_cloud_token(
    hass: HomeAssistant, entry: Control4ConfigEntry
) -> tuple[C4Account, dict[str, Any]]:
    """Account + director tokens from the Control4 cloud, each call time-boxed.

    pyControl4 sets no timeout on cloud calls; a hanging link held setup for ~4 minutes.
    Raises ConfigEntryAuthFailed on bad credentials, ConfigEntryNotReady otherwise.
    """
    account = account_for(hass, entry)
    try:
        await _cloud_call(account.get_account_bearer_token)
        token_dict = await _cloud_call(
            lambda: account.get_director_bearer_token(
                entry.data[CONF_CONTROLLER_UNIQUE_ID]
            )
        )
    except BadCredentials as err:
        raise ConfigEntryAuthFailed(err) from err
    except (TimeoutError, ClientError, C4Exception, KeyError) as err:
        raise ConfigEntryNotReady(
            f"Control4 cloud did not issue a director token: {err!r}"
        ) from err
    save_token(hass, entry, token_dict[CONF_TOKEN], token_dict["validSeconds"])
    return account, token_dict


async def director_version(
    hass: HomeAssistant, entry: Control4ConfigEntry, account: C4Account
) -> str:
    """The Director's OS version: the controller's broker first, the cloud last.

    The broker answers `/api/v1/version` locally without a token (checked on a CORE1,
    OS 4.2.1: a list of {name, version}, one of them "Director"). The value is remembered,
    so a restart needs neither the broker nor the cloud for it.
    """
    session = aiohttp_client.async_get_clientsession(hass, verify_ssl=False)
    try:
        async with asyncio.timeout(10):
            async with session.get(
                f"https://{entry.data[CONF_HOST]}/api/v1/version"
            ) as resp:
                components = await resp.json(content_type=None)
        version = next(
            str(item["version"])
            for item in components
            if isinstance(item, dict) and item.get("name") == "Director"
        )
    except (TimeoutError, ClientError, ValueError, TypeError, KeyError, StopIteration):
        version = entry.data.get(CONF_DIRECTOR_SW_VERSION)
        if not version:
            try:
                async with asyncio.timeout(CLOUD_REQUEST_TIMEOUT_SEC):
                    if not getattr(account, "account_bearer_token", None):
                        await _cloud_call(account.get_account_bearer_token)
                    href = (await account.get_account_controllers())["href"]
                    version = await account.get_controller_os_version(href)
            except (TimeoutError, ClientError, C4Exception, KeyError) as err:
                raise ConfigEntryNotReady(
                    f"Control4 Director version unavailable: {err!r}"
                ) from err
    if entry.data.get(CONF_DIRECTOR_SW_VERSION) != version:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DIRECTOR_SW_VERSION: version}
        )
    return version
