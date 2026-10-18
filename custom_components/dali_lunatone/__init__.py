"""The Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import asyncio
import logging
from typing import cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    CONF_SCAN_NEW_DEVICES_ON_STARTUP,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import LunatoneCoordinator
from .lunatone_api import LunatoneClient, line_device_identifier

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_RESCAN = "rescan_devices"
SERVICE_STEP_UP = "step_up"
SERVICE_STEP_DOWN = "step_down"
SERVICE_RECALL_MAX = "recall_max"
SERVICE_SET_FEEDBACK_LED = "set_feedback_led"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lunatone DALI-2 IoT from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    # Create client
    client = LunatoneClient(host, port)

    # Connect to device
    if not await client.connect():
        raise ConfigEntryNotReady(f"Unable to connect to Lunatone at {host}:{port}")

    # Create coordinator
    coordinator = LunatoneCoordinator(hass, client, entry.entry_id, entry)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Check if we should scan on startup (system option, default False)
    scan_on_startup = entry.options.get(CONF_SCAN_NEW_DEVICES_ON_STARTUP, False)
    
    if scan_on_startup:
        _LOGGER.info("Scan on startup enabled, performing device scan")
        await coordinator.async_rescan_devices()
    elif not coordinator.data:
        _LOGGER.warning("No stored devices found. Enable 'Scan new devices on startup' in options or use the Manual Scan button")
    else:
        _LOGGER.info("Loaded %d devices from storage", len(coordinator.data))

    # Store coordinator and client
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }

    # Register the gateway device
    device_registry = dr.async_get(hass)
    gateway_info = client.device_info if client.device_info else {}
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Lunatone",
        model="DALI-2 IoT Gateway",
        sw_version=gateway_info.get("version", "Unknown"),
        configuration_url=f"http://{host}:{port}",
    )

    # One "DALI Bus N" device per line, between the gateway and the line's DALI devices,
    # so each device shows which bus it is on. Includes lines that still have stored devices.
    bus_lines = set(client.lines) | {device.line for device in (coordinator.data or {}).values()}
    for line in sorted(bus_lines):
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, line_device_identifier(entry.entry_id, line))},
            name=f"DALI Bus {line}",
            manufacturer="Lunatone",
            model="DALI line",
            via_device=(DOMAIN, entry.entry_id),
        )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Schedule background state update after HASS finishes starting
    async def update_initial_states(_=None):
        """Update device states in background after startup."""
        if coordinator.data:
            _LOGGER.info("Starting background update of device states for %d devices", len(coordinator.data))
            try:
                await client.update_device_states()
                await coordinator.async_request_refresh()
                _LOGGER.info("Background state update completed")
            except Exception:
                _LOGGER.exception("Error during background state update")

    # If HASS already started, run immediately, otherwise wait for startup event
    if hass.state == CoreState.running:
        hass.async_create_task(update_initial_states())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, update_initial_states)

    # Register services
    async def handle_rescan_devices(call: ServiceCall) -> None:
        """Handle the rescan_devices service call."""
        _LOGGER.info("Rescanning DALI devices")
        await coordinator.async_rescan_devices()
        _LOGGER.info("Rescan complete: %d devices found", len(coordinator.data))

    async def handle_set_feedback_led(call: ServiceCall) -> None:
        """Handle the set_feedback_led service call."""
        entity_id = call.data.get("entity_id")
        state = call.data.get("state") == "on"
        
        if not entity_id:
            _LOGGER.error("No entity_id provided for set_feedback_led service")
            return
        
        # Parse entity_id to extract address and instance
        # Expected format: binary_sensor.dali2_{address}_button_{instance}
        try:
            parts = entity_id.split(".")[-1].split("_")
            if len(parts) >= 4 and parts[0] == "dali2" and parts[2] == "button":
                address = int(parts[1])
                instance = int(parts[3])
                
                _LOGGER.info(
                    "Setting feedback LED for address %d instance %d to %s",
                    address, instance, "ON" if state else "OFF"
                )
                
                success = await client.set_feedback_led(address, instance, state)
                if success:
                    # Update state in coordinator
                    coordinator.set_led_state(address, instance, state)
                    _LOGGER.info("Feedback LED command sent successfully")
                else:
                    _LOGGER.error("Failed to send feedback LED command")
            else:
                _LOGGER.error("Invalid entity_id format: %s", entity_id)
        except (ValueError, IndexError) as e:
            _LOGGER.error("Error parsing entity_id %s: %s", entity_id, e)

    # Register rescan service (only once, for all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_RESCAN):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESCAN,
            handle_rescan_devices,
        )

    # Register feedback LED service (only once, for all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_FEEDBACK_LED):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FEEDBACK_LED,
            handle_set_feedback_led,
        )

    _LOGGER.info(
        "Lunatone DALI-2 IoT integration setup complete for %s:%s with %d devices",
        host,
        port,
        len(coordinator.data),
    )

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms including group_light
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Disconnect client
        data = hass.data[DOMAIN].pop(entry.entry_id)
        client: LunatoneClient = data[DATA_CLIENT]
        await client.disconnect()

        _LOGGER.info("Lunatone DALI-2 IoT integration unloaded")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
