"""Config flow for Control4 integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

from aiohttp.client_exceptions import ClientError
import voluptuous as vol
from pyControl4.account import C4Account
from pyControl4.director import C4Director
from pyControl4.error_handling import BadCredentials, NotFound, Unauthorized

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.device_registry import format_mac

from . import get_items_of_category, token_store
from .const import (
    AVAILABLE_PLATFORMS,
    CONF_CONTROLLER_UNIQUE_ID,
    CONF_DRY_CONTACT_COVERS,
    CONF_ENABLED_PLATFORMS,
    CONTROL4_COVER_CATEGORY,
    CONTROL4_ENTITY_TYPE,
    DEFAULT_ENABLED_PLATFORMS,
    DOMAIN,
    Control4ConfigEntry,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class Control4ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Control4."""

    VERSION = 1

    _connect_data: dict[str, Any]

    async def _async_try_connect(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, str]]:
        """Try to connect to Control4 and return errors, data, and placeholders."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}
        data: dict[str, Any] | None = None

        host = user_input[CONF_HOST]
        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]

        # Step 1: Authenticate with Control4 cloud API
        account = C4Account(username, password, token_store.cloud_session(self.hass))
        try:
            await account.get_account_bearer_token()

            account_controllers = await account.get_account_controllers()
            controller_unique_id = account_controllers["controllerCommonName"]

            director_bearer_token = (
                await account.get_director_bearer_token(controller_unique_id)
            )["token"]
        except BadCredentials, Unauthorized:
            errors["base"] = "invalid_auth"
            return errors, data, description_placeholders
        except NotFound:
            errors["base"] = "controller_not_found"
            return errors, data, description_placeholders
        except Exception:
            _LOGGER.exception(
                "Unexpected exception during Control4 account authentication"
            )
            errors["base"] = "unknown"
            return errors, data, description_placeholders

        # Step 2: Connect to local Control4 Director
        director_session = aiohttp_client.async_get_clientsession(
            self.hass, verify_ssl=False
        )
        director = C4Director(host, director_bearer_token, director_session)
        try:
            await director.get_all_item_info()
        except Unauthorized:
            errors["base"] = "director_auth_failed"
            return errors, data, description_placeholders
        except ClientError, TimeoutError:
            errors["base"] = "cannot_connect"
            description_placeholders["host"] = host
            return errors, data, description_placeholders
        except Exception:
            _LOGGER.exception(
                "Unexpected exception during Control4 director connection"
            )
            errors["base"] = "unknown"
            return errors, data, description_placeholders

        # Success - return the data needed for entry creation
        data = {
            CONF_HOST: host,
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_CONTROLLER_UNIQUE_ID: controller_unique_id,
        }

        return errors, data, description_placeholders

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            errors, data, description_placeholders = await self._async_try_connect(
                user_input
            )

            if not errors and data is not None:
                controller_unique_id = data[CONF_CONTROLLER_UNIQUE_ID]
                mac = (controller_unique_id.split("_", 3))[2]
                formatted_mac = format_mac(mac)
                await self.async_set_unique_id(formatted_mac)
                self._abort_if_unique_id_configured()
                self._connect_data = data
                return await self.async_step_platforms()

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_platforms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which entity types to import, before creating the entry."""
        if user_input is not None:
            data = self._connect_data
            return self.async_create_entry(
                title=data[CONF_CONTROLLER_UNIQUE_ID],
                data=data,
                options={CONF_ENABLED_PLATFORMS: user_input[CONF_ENABLED_PLATFORMS]},
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLED_PLATFORMS, default=DEFAULT_ENABLED_PLATFORMS
                ): cv.multi_select(AVAILABLE_PLATFORMS),
            }
        )
        return self.async_show_form(step_id="platforms", data_schema=schema)

    @override
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth: setup or a token refresh raised ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new account credentials; keep the entry, its entities and options."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            errors, data, description_placeholders = await self._async_try_connect(
                {CONF_HOST: reauth_entry.data[CONF_HOST], **user_input}
            )
            if not errors and data is not None:
                mac = (data[CONF_CONTROLLER_UNIQUE_ID].split("_", 3))[2]
                await self.async_set_unique_id(format_mac(mac))
                self._abort_if_unique_id_mismatch(reason="wrong_controller")
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=reauth_entry.data[CONF_USERNAME]
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: Control4ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """Pick the entity types and mark dry-contact covers. No polling interval: push."""

    async def _async_get_known_covers(self) -> dict[str, str]:
        """Return {item_id: name} for currently known Control4 cover items.

        Used to let the user mark specific covers as dry-contact (no position
        feedback) rather than applying that to every cover. Empty whenever the
        list can't be had - entry not loaded, Director offline, expired token -
        so Configure still opens then; the stored marks are kept (see init step).
        """
        try:
            items = await get_items_of_category(
                self.hass, self.config_entry, CONTROL4_COVER_CATEGORY
            )
        except Exception as err:  # noqa: BLE001 - any failure: platforms stay editable
            _LOGGER.debug("Cover list unavailable for the options flow: %s", err)
            return {}
        return {
            str(item["id"]): item["name"]
            for item in items
            if item.get("type") == CONTROL4_ENTITY_TYPE
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            # Merge, don't replace: without a cover list the dry-contact field isn't
            # on the form, and replacing would silently wipe the stored marks.
            options = {
                key: value
                for key, value in self.config_entry.options.items()
                if key != CONF_SCAN_INTERVAL  # left over from the polling versions
            }
            options.update(user_input)
            return self.async_create_entry(title="", data=options)

        known_covers = await self._async_get_known_covers()

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_ENABLED_PLATFORMS,
                default=self.config_entry.options.get(
                    CONF_ENABLED_PLATFORMS, DEFAULT_ENABLED_PLATFORMS
                ),
            ): cv.multi_select(AVAILABLE_PLATFORMS),
        }
        if known_covers:
            schema[
                vol.Optional(
                    CONF_DRY_CONTACT_COVERS,
                    # Only ids still in the project: multi_select rejects a default it
                    # doesn't list, so a removed cover would block saving the form.
                    default=[
                        item_id
                        for item_id in self.config_entry.options.get(
                            CONF_DRY_CONTACT_COVERS, []
                        )
                        if item_id in known_covers
                    ],
                )
            ] = cv.multi_select(known_covers)

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
