"""Config flow for Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_BACKGROUND_STATUS_POLLING,
    CONF_DALI_LINES,
    CONF_POLLING_INTERVAL,
    CONF_SCAN_NEW_DEVICES_ON_STARTUP,
    DALI_LINE_COUNT,
    DEFAULT_DALI_LINES,
    DEFAULT_NAME,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
)
from .lunatone_api import LunatoneClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    client = LunatoneClient(data[CONF_HOST], data[CONF_PORT])

    try:
        if not await client.connect():
            raise CannotConnect

        # Get device info for unique_id
        device_info = client.device_info
        await client.disconnect()

        return {
            "title": f"{DEFAULT_NAME} ({data[CONF_HOST]})",
            "unique_id": str(device_info.get("device", {}).get("serial", data[CONF_HOST])),
            "device_info": device_info,
        }
    except Exception as e:
        _LOGGER.error("Error connecting to Lunatone: %s", e)
        raise CannotConnect from e


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lunatone DALI-2 IoT."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Set unique ID to prevent duplicate entries
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for the integration."""

    def __init__(self) -> None:
        """Initialize options flow."""
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._get_options_schema(),
        )

    def _get_options_schema(self) -> vol.Schema:
        """Get options schema."""
        return vol.Schema(
            {
                vol.Optional(
                    CONF_BACKGROUND_STATUS_POLLING,
                    default=self.config_entry.options.get(
                        CONF_BACKGROUND_STATUS_POLLING,
                        False,
                    ),
                ): bool,
                vol.Optional(
                    CONF_POLLING_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_POLLING_INTERVAL,
                        DEFAULT_POLLING_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL)),
                vol.Optional(
                    CONF_DALI_LINES,
                    default=[
                        str(line)
                        for line in self.config_entry.options.get(CONF_DALI_LINES, DEFAULT_DALI_LINES)
                    ],
                ): cv.multi_select({str(line): f"Line {line}" for line in range(DALI_LINE_COUNT)}),
                vol.Optional(
                    CONF_SCAN_NEW_DEVICES_ON_STARTUP,
                    default=self.config_entry.options.get(
                        CONF_SCAN_NEW_DEVICES_ON_STARTUP,
                        False,
                    ),
                ): bool,
            }
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
