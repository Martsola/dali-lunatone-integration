"""DataUpdateCoordinator for Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BACKGROUND_STATUS_POLLING,
    CONF_DALI_LINES,
    CONF_POLLING_INTERVAL,
    DALI_EVENT,
    DEFAULT_DALI_LINES,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
)
from .lunatone_api import DaliDevice, LunatoneClient
from .storage import DeviceStorage

_LOGGER = logging.getLogger(__name__)

# Momentary event types that should auto-reset the binary sensor state
MOMENTARY_EVENTS = ("short_press", "double_press")
MOMENTARY_RESET_DELAY = 0.5  # seconds


class LunatoneCoordinator(DataUpdateCoordinator[dict[tuple[str, int], DaliDevice]]):
    """Class to manage fetching Lunatone DALI data."""

    def __init__(self, hass: HomeAssistant, client: LunatoneClient, entry_id: str, config_entry: Any) -> None:
        """Initialize coordinator."""
        # Get polling settings from options
        background_polling = config_entry.options.get(CONF_BACKGROUND_STATUS_POLLING, False)
        polling_interval = config_entry.options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
        
        # Only set update_interval if background polling is enabled
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=polling_interval) if background_polling else None,
        )
        self.client = client
        # Options store the selected lines as strings (multi-select keys)
        self.client.lines = sorted(
            int(line) for line in config_entry.options.get(CONF_DALI_LINES, DEFAULT_DALI_LINES)
        ) or list(DEFAULT_DALI_LINES)
        self.storage = DeviceStorage(hass, entry_id)
        self._devices_loaded = False
        self._background_polling = background_polling
        self._polling_interval = polling_interval
        self._last_full_scan = 0.0  # Timestamp of last full state scan
        self._led_states: dict[tuple[int, int], bool] = {}  # (address, instance) -> is_on
        self._scan_in_progress = False
        
        # Register callback for real-time events
        self.client.add_callback(self._handle_device_event)

    async def _async_update_data(self) -> dict[tuple[str, int], DaliDevice]:
        """Fetch data from Lunatone."""
        try:
            if not self.client.connected:
                raise UpdateFailed("Not connected to Lunatone device")

            # Load devices from storage on first run
            if not self._devices_loaded:
                _LOGGER.info("Loading devices from storage")
                stored_devices = await self.storage.async_load()
                
                if stored_devices:
                    # Restore devices from storage
                    restored_devices = self.storage.deserialize_devices(stored_devices)
                    self.client.devices.update(restored_devices)
                    _LOGGER.info("Restored %d devices from storage", len(restored_devices))
                else:
                    _LOGGER.info("No stored devices found, use rescan_devices service to discover devices")
                
                self._devices_loaded = True

            # Check if we should do a full state scan based on configured polling interval
            # Only poll if background polling is enabled
            if self._background_polling:
                import time
                current_time = time.time()
                if current_time - self._last_full_scan >= self._polling_interval:
                    if self.client.devices:
                        _LOGGER.debug("Performing periodic state scan (interval: %ds)", self._polling_interval)
                        await self.client.update_device_states()
                        self._last_full_scan = current_time

            return self.client.devices

        except Exception as err:
            raise UpdateFailed(f"Error communicating with Lunatone: {err}") from err

    async def async_set_brightness(
        self, address: int, brightness: int
    ) -> bool:
        """Set device brightness."""
        success = await self.client.set_brightness(address, brightness)
        if success:
            # Update device state immediately for DALI device without scanning
            key = ("DALI", address)
            if key in self.client.devices:
                self.client.devices[key].brightness = brightness
                self.client.devices[key].is_on = brightness > 0
                # Notify coordinator of state change without triggering a scan
                self.async_set_updated_data(self.client.devices)
        return success

    async def async_turn_on(self, address: int) -> bool:
        """Turn on device."""
        success = await self.client.turn_on(address)
        if success:
            key = ("DALI", address)
            if key in self.client.devices:
                self.client.devices[key].is_on = True
                # Notify coordinator of state change without triggering a scan
                self.async_set_updated_data(self.client.devices)
        return success

    async def async_turn_off(self, address: int) -> bool:
        """Turn off device."""
        success = await self.client.turn_off(address)
        if success:
            key = ("DALI", address)
            if key in self.client.devices:
                self.client.devices[key].is_on = False
                self.client.devices[key].brightness = 0
                # Notify coordinator of state change without triggering a scan
                self.async_set_updated_data(self.client.devices)
        return success

    async def async_set_color_temp(
        self, address: int, kelvin: int
    ) -> bool:
        """Set device color temperature."""
        success = await self.client.set_color_temp(address, kelvin)
        if success:
            key = ("DALI", address)
            if key in self.client.devices:
                self.client.devices[key].color_temp = kelvin
                # Notify coordinator of state change without triggering a scan
                self.async_set_updated_data(self.client.devices)
        return success

    async def async_rescan_devices(self) -> dict[tuple[str, int], DaliDevice]:
        """Rescan for devices on the bus."""
        if self._scan_in_progress:
            _LOGGER.warning("Scan already in progress, ignoring request")
            persistent_notification.async_create(
                self.hass,
                "A scan is already running. Please wait for it to complete before starting a new one.",
                title="DALI Scan Already In Progress",
                notification_id=f"{DOMAIN}_scan_already_running",
            )
            return self.client.devices

        self._scan_in_progress = True
        _LOGGER.info("Rescanning for DALI devices")

        persistent_notification.async_create(
            self.hass,
            "Manual scan started. This may take up to a minute while the DALI bus is queried.",
            title="DALI Scan In Progress",
            notification_id=f"{DOMAIN}_scan_progress",
        )
        
        # Get old devices for comparison
        old_devices = set(self.client.devices.keys())
        
        try:
            # Perform scan
            devices = await self.client.scan_all_lines()
        except ConnectionError as e:
            _LOGGER.error("Cannot rescan: %s", e)
            self._scan_in_progress = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_scan_progress")
            persistent_notification.async_create(
                self.hass,
                f"Rescan failed: {e}. Please check that the gateway is online and try again.",
                title="DALI Rescan Failed",
                notification_id=f"{DOMAIN}_rescan_failed",
            )
            return self.client.devices
        except Exception as e:
            _LOGGER.error("Error during rescan: %s", e)
            self._scan_in_progress = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_scan_progress")
            persistent_notification.async_create(
                self.hass,
                f"Rescan error: {e}",
                title="DALI Rescan Error",
                notification_id=f"{DOMAIN}_rescan_error",
            )
            return self.client.devices
        
        # Get new devices after scan
        new_devices = set(self.client.devices.keys())
        
        # Find newly discovered and missing devices
        added_devices = new_devices - old_devices
        missing_devices = old_devices - new_devices
        
        # Save to storage
        serialized_devices = {}
        for key, device in self.client.devices.items():
            serialized_devices[key] = self.storage.serialize_device(device)
        
        await self.storage.async_save(serialized_devices, datetime.now().isoformat())
        _LOGGER.info("Saved %d devices to storage", len(serialized_devices))
        
        # Report changes
        if added_devices:
            await self._report_new_devices(added_devices)
        
        if missing_devices:
            await self._report_missing_devices(missing_devices)
        
        # Dismiss in-progress notification and show result
        self._scan_in_progress = False
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_scan_progress")
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_scan_already_running")
        added_count = len(added_devices)
        missing_count = len(missing_devices)
        total_count = len(self.client.devices)
        if added_count or missing_count:
            summary = (
                f"Scan complete: {total_count} devices found.\n"
                + (f"  • {added_count} newly discovered\n" if added_count else "")
                + (f"  • {missing_count} no longer present\n" if missing_count else "")
            )
        else:
            summary = f"Scan complete: {total_count} devices found, no changes."
        persistent_notification.async_create(
            self.hass,
            summary,
            title="DALI Scan Complete",
            notification_id=f"{DOMAIN}_scan_complete",
        )

        await self.async_request_refresh()
        return devices
    
    async def _report_new_devices(self, device_keys: set[tuple[str, int]]) -> None:
        """Create notifications for newly discovered devices."""
        for key in device_keys:
            device = self.client.devices.get(key)
            if device:
                types_label = device.device_types_label if hasattr(device, "device_types_label") else f"DT{device.device_type}"
                persistent_notification.async_create(
                    self.hass,
                    f"New {device.protocol} device found:\n"
                    f"Type: {types_label}\n"
                    f"Address: {device.address}\n"
                    f"Name: {device.device_name}",
                    "DALI Device Discovered",
                    f"dali_new_device_{device.protocol}_{device.address}",
                )
                _LOGGER.info(
                    "New device discovered: %s %s at address %d",
                    device.protocol,
                    types_label,
                    device.address,
                )
    
    async def _report_missing_devices(self, device_keys: set[tuple[str, int]]) -> None:
        """Create repair issues for missing devices."""
        from homeassistant.helpers import issue_registry as ir
        
        for key in device_keys:
            protocol, address = key
            
            # Look up device name/type from old backup (stored in client before scan cleared it)
            # After scan, client.devices has the NEW devices, so missing ones won't be there.
            # We use the coordinator data which still has the pre-scan snapshot.
            old_device = (self.data or {}).get(key)
            device_name = old_device.device_name if old_device else "Unknown"
            device_type = old_device.device_type if old_device else "Unknown"

            # Skip repair issue creation for devices that were never properly identified
            # (stale storage artefacts from failed scans show device_name="Unknown")
            if device_name in ("Unknown", None) or str(device_name).startswith("Unknown ("):
                _LOGGER.debug(
                    "Stale/unidentified device removed from storage: %s at address %d",
                    protocol, address,
                )
                continue

            # Create repair issue for real, previously known devices
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"missing_device_{protocol}_{address}",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_device",
                translation_placeholders={
                    "protocol": protocol,
                    "address": str(address),
                    "device_name": device_name,
                    "device_type": str(device_type),
                },
            )
            
            _LOGGER.warning(
                "Device missing from bus: %s %s at address %d - created repair issue",
                protocol,
                device_type,
                address,
            )
    
    async def async_remove_device(self, protocol: str, address: int) -> bool:
        """Remove a device from storage after user confirmation."""
        from homeassistant.helpers import issue_registry as ir
        
        key = (protocol, address)
        if key in self.client.devices:
            device = self.client.devices.pop(key)
            
            # Save updated devices to storage
            serialized_devices = {}
            for k, dev in self.client.devices.items():
                serialized_devices[k] = self.storage.serialize_device(dev)
            
            await self.storage.async_save(serialized_devices)
            
            # Remove the repair issue
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"missing_device_{protocol}_{address}",
            )
            
            _LOGGER.info(
                "Removed device: %s %s at address %d",
                protocol,
                device.device_type,
                address,
            )
            
            await self.async_request_refresh()
            return True
        
        return False

    async def async_step_up(self, address: int) -> bool:
        """Step up brightness."""
        success = await self.client.step_up(address)
        if success:
            # For step commands, we need to query the actual brightness
            # Schedule a refresh on next update cycle
            await self.async_request_refresh()
        return success

    async def async_step_down(self, address: int) -> bool:
        """Step down brightness."""
        success = await self.client.step_down(address)
        if success:
            # For step commands, we need to query the actual brightness
            # Schedule a refresh on next update cycle
            await self.async_request_refresh()
        return success

    async def async_recall_max(self, address: int) -> bool:
        """Recall maximum level."""
        success = await self.client.recall_max_level(address)
        if success:
            key = ("DALI", address)
            if key in self.client.devices:
                self.client.devices[key].is_on = True
                self.client.devices[key].brightness = 100
                # Notify coordinator of state change without triggering a scan
                self.async_set_updated_data(self.client.devices)
        return success

    def _handle_device_event(self, event_type: str, *args: Any) -> None:
        """Handle device events from the WebSocket connection."""
        if event_type == "state_update":
            # DALI device state changed (external command from wall switch, etc.)
            device = args[0] if args else None
            if device:
                _LOGGER.debug(
                    "External state update: %s address %d, brightness=%d%%, on=%s",
                    device.protocol,
                    device.address,
                    device.brightness,
                    device.is_on,
                )
                # Trigger coordinator update to notify all entities
                self.async_set_updated_data(self.client.devices)
        
        elif event_type == "dali2_event":
            # DALI2 instance event (pushbutton, sensor, etc.)
            device = args[0] if len(args) > 0 else None
            instance = args[1] if len(args) > 1 else None
            instance_info = args[2] if len(args) > 2 else None
            
            if device and instance is not None and instance_info:
                instance_type = instance_info.get("type", 0)
                button_event_type = instance_info.get("event_type", "")
                _LOGGER.debug(
                    "DALI2 event: address %d, instance %d, type %d, event=%s, data=%s",
                    device.address,
                    instance,
                    instance_type,
                    button_event_type,
                    instance_info,
                )
                
                # Fire HA event bus event for button/switch/occupancy events
                if instance_type in (1, 2, 3) and button_event_type:
                    # Look up HA device_id for device trigger matching
                    device_reg = dr.async_get(self.hass)
                    ha_device = device_reg.async_get_device(
                        identifiers={(DOMAIN, f"{device.protocol}_{device.address}")}
                    )
                    ha_device_id = ha_device.id if ha_device else None

                    self.hass.bus.async_fire(
                        DALI_EVENT,
                        {
                            "device_id": ha_device_id,
                            "type": button_event_type,
                            "device_address": device.address,
                            "instance": instance,
                            "instance_type": instance_type,
                            "event_type": button_event_type,
                            "event_data": instance_info.get("event_data"),
                        },
                    )
                
                # Trigger coordinator update to notify binary_sensor and sensor entities
                self.async_set_updated_data(self.client.devices)
                
                # Auto-reset momentary events (short_press, double_press)
                # These set state=True briefly, then reset to False
                if button_event_type in MOMENTARY_EVENTS:
                    self.hass.async_create_task(
                        self._async_reset_momentary_state(
                            device, instance, instance_info
                        )
                    )

    def get_led_state(self, address: int, instance: int) -> bool | None:
        """Get the current state of a feedback LED."""
        return self._led_states.get((address, instance))

    async def _async_reset_momentary_state(
        self, device: DaliDevice, instance: int, instance_info: dict
    ) -> None:
        """Reset binary sensor state after a momentary button event."""
        await asyncio.sleep(MOMENTARY_RESET_DELAY)
        # Only reset if the state hasn't been changed by another event
        current_event = instance_info.get("event_type", "")
        if current_event in MOMENTARY_EVENTS:
            instance_info["state"] = False
            _LOGGER.debug(
                "Auto-reset momentary state: address %d, instance %d, event was %s",
                device.address,
                instance,
                current_event,
            )
            self.async_set_updated_data(self.client.devices)

    def set_led_state(self, address: int, instance: int, is_on: bool) -> None:
        """Set the state of a feedback LED and notify listeners."""
        self._led_states[(address, instance)] = is_on
        # Trigger coordinator update to notify LED entities
        self.async_set_updated_data(self.client.devices)
