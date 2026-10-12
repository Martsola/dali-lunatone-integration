"""Light platform for Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ADDRESS,
    ATTR_DEVICE_NAME,
    ATTR_DEVICE_TYPE,
    ATTR_FIRMWARE_VERSION,
    ATTR_PROTOCOL,
    DATA_COORDINATOR,
    DOMAIN,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
)
from .coordinator import LunatoneCoordinator
from .lunatone_api import DaliDevice

_LOGGER = logging.getLogger(__name__)


def _aggregate_state(devices: list[DaliDevice]) -> tuple[bool | None, int | None]:
    """Combine member gear state into (is_on, brightness 0-255) for a group-like entity.

    On if any member is on; brightness is the mean of the members that are on,
    matching Home Assistant's own light groups.
    """
    if not devices:
        return None, None
    levels = [device.brightness for device in devices if device.is_on]
    if not levels:
        return False, None
    known = [level for level in levels if level is not None]
    if not known:
        return True, None
    return True, round(sum(known) / len(known) / 100 * 255)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lunatone DALI light entities."""
    from .const import DATA_CLIENT
    
    coordinator: LunatoneCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    client = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    # Create light entities for all discovered devices
    entities = []
    for (protocol, address), device in coordinator.data.items():
        # Only create lights for DALI devices (DT6=LED, DT7=Switching, DT8=Colour)
        if protocol == "DALI" and device.device_type in [6, 7, 8]:
            entities.append(LunatoneDaliLight(coordinator, protocol, address, entry))
        
        # Create feedback LED light entities for DALI2 instances with LED support
        if protocol == "DALI2" and device.instances:
            for instance_num, instance_data in device.instances.items():
                if instance_data.get("has_feedback_led"):
                    entities.append(
                        FeedbackLedLight(coordinator, client, protocol, address, instance_num, entry)
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DALI light entities", len(entities))

    # Track which (protocol, address) keys already have a light entity so we can
    # dynamically add entities for devices that become light-capable after a scan.
    _known_light_keys: set[tuple[str, int]] = {
        (protocol, address)
        for (protocol, address), device in coordinator.data.items()
        if protocol == "DALI" and device.device_type in [6, 7, 8]
    }

    @callback
    def _async_check_new_light_entities() -> None:
        """Create light entities for devices that became DT6/7/8 after a rescan."""
        new_entities: list[LunatoneDaliLight] = []
        for (proto, addr), dev in (coordinator.data or {}).items():
            key = (proto, addr)
            if key not in _known_light_keys and proto == "DALI" and dev.device_type in [6, 7, 8]:
                _known_light_keys.add(key)
                new_entities.append(LunatoneDaliLight(coordinator, proto, addr, entry))
        if new_entities:
            _LOGGER.info(
                "Dynamically adding %d new DALI light entities after scan",
                len(new_entities),
            )
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_check_new_light_entities))

    # Find groups with devices
    groups_with_devices = set()
    for device in coordinator.data.values():
        if device.protocol == "DALI" and device.groups:
            groups_with_devices.update(device.groups)

    # Create group light entities for groups 0-15 that have devices
    group_entities = []
    for group_num in sorted(groups_with_devices):
        if group_num <= 15:
            group_entities.append(DaliGroupLight(coordinator, client, group_num, entry))
    
    # Add broadcast entity
    group_entities.append(DaliBroadcastLight(coordinator, client, entry))
    
    if group_entities:
        async_add_entities(group_entities)
        _LOGGER.info("Added %d DALI group entities (%d groups + broadcast)", 
                     len(group_entities), len(group_entities) - 1)

    # Register platform services
    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        "step_up",
        {},
        "async_step_up",
    )

    platform.async_register_entity_service(
        "step_down",
        {},
        "async_step_down",
    )

    platform.async_register_entity_service(
        "recall_max",
        {},
        "async_recall_max",
    )


class LunatoneDaliLight(CoordinatorEntity[LunatoneCoordinator], LightEntity):
    """Representation of a Lunatone DALI light."""

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        protocol: str,
        address: int,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._protocol = protocol
        self._address = address
        self._device_key = (protocol, address)
        
        self._attr_unique_id = f"{entry.entry_id}_{protocol}_{address}"
        # Set name in format: "DALI Address 4" or "DALI2 Address 0"
        self._attr_name = f"{protocol} Address {address}"

        # Default to brightness-only; _update_color_mode upgrades to CCT if DT8
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS
        self._update_color_mode()

    def _update_color_mode(self) -> None:
        """Re-evaluate color mode capabilities from current coordinator data."""
        device = self._device
        if device is None:
            return
        if device.supports_color_temp:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS

    def _handle_coordinator_update(self) -> None:
        """Handle updated coordinator data, refreshing color mode if device type changed."""
        self._update_color_mode()
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info, reading fresh data from coordinator."""
        # Get current device from coordinator
        device = self._device
        protocol = self._protocol
        address = self._address
        
        # Build model label: show all types for multi-type devices, e.g. "DALI DT6,8"
        types_label = device.device_types_label if hasattr(device, "device_types_label") else f"DT{device.device_type}"
        model_label = f"{protocol} {types_label}"

        device_info_dict = DeviceInfo(
            identifiers={(DOMAIN, f"{protocol}_{address}")},
            name=f"{protocol} Address {address}",
            model=model_label,
            via_device=(DOMAIN, self.coordinator.config_entry.entry_id),
        )
        
        # Add extended device information if available
        if hasattr(device, "manufacturer") and device.manufacturer:
            device_info_dict["manufacturer"] = device.manufacturer
        if hasattr(device, "gtin_decimal") and device.gtin_decimal:
            device_info_dict["model"] = f"{model_label} (GTIN: {device.gtin_decimal})"
        if hasattr(device, "firmware_version") and device.firmware_version:
            device_info_dict["sw_version"] = device.firmware_version
        if hasattr(device, "hardware_version") and device.hardware_version:
            device_info_dict["hw_version"] = device.hardware_version
        if hasattr(device, "serial_number") and device.serial_number:
            device_info_dict["serial_number"] = device.serial_number
        
        return device_info_dict

    @property
    def _device(self) -> DaliDevice | None:
        """Get current device state from coordinator, or None if removed."""
        return (self.coordinator.data or {}).get(self._device_key)

    @property
    def is_on(self) -> bool:
        """Return true if light is on."""
        return self._device.is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness of this light (0-255)."""
        if self._device.brightness is None:
            return None
        # Convert from 0-100% to 0-255
        return round((self._device.brightness / 100) * 255)

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        if self._device.supports_color_temp and self._device.color_temp:
            return self._device.color_temp
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            ATTR_ADDRESS: self._address,
            ATTR_PROTOCOL: self._protocol,
            ATTR_DEVICE_TYPE: self._device.device_type,
            ATTR_DEVICE_NAME: self._device.device_name,
        }
        # Expose all supported device types for multi-type gear
        if hasattr(self._device, "device_types") and self._device.device_types:
            attrs["device_types"] = self._device.device_types
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        # Handle color temperature change (if supported)
        if color_temp_kelvin is not None and self._device.supports_color_temp:
            # Clamp to valid range
            kelvin = max(
                MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, color_temp_kelvin)
            )
            
            success = await self.coordinator.async_set_color_temp(
                self._address, kelvin
            )
            if not success:
                _LOGGER.error(
                    "Failed to set color temperature for device %d", self._address
                )

        # Handle brightness change or turn on
        if brightness is not None:
            # Convert from 0-255 to 0-100%
            brightness_pct = round((brightness / 255) * 100)
            success = await self.coordinator.async_set_brightness(
                self._address, brightness_pct
            )
            if not success:
                _LOGGER.error(
                    "Failed to set brightness for device %d", self._address
                )
        elif color_temp_kelvin is None:
            # Only turn on if neither brightness nor color temp was specified
            success = await self.coordinator.async_turn_on(self._address)
            if not success:
                _LOGGER.error(
                    "Failed to turn on device %d", self._address
                )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        success = await self.coordinator.async_turn_off(self._address)
        if not success:
            _LOGGER.error("Failed to turn off device %d", self._address)

    async def async_step_up(self) -> None:
        """Step up brightness by one step."""
        await self.coordinator.async_step_up(self._address)

    async def async_step_down(self) -> None:
        """Step down brightness by one step."""
        await self.coordinator.async_step_down(self._address)

    async def async_recall_max(self) -> None:
        """Recall maximum brightness level."""
        await self.coordinator.async_recall_max(self._address)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.client.connected
            and self._device_key in (self.coordinator.data or {})
        )


class FeedbackLedLight(CoordinatorEntity[LunatoneCoordinator], LightEntity):
    """Representation of a DALI2 instance feedback LED."""

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        client,
        protocol: str,
        address: int,
        instance_num: int,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the feedback LED light."""
        super().__init__(coordinator)
        self._client = client
        self._protocol = protocol
        self._address = address
        self._instance_num = instance_num
        self._device_key = (protocol, address)
        
        # Get device reference
        device = coordinator.data[self._device_key]
        
        # Unique ID
        self._attr_unique_id = f"{entry.entry_id}_{protocol}_{address}_led_{instance_num}"
        
        # Name
        self._attr_name = f"LED {instance_num}"
        self._attr_has_entity_name = True
        
        # Initialize state - None means unknown (lightning bolt icon)
        initial_state = coordinator.get_led_state(address, instance_num)
        self._attr_is_on = initial_state  # Keep None if unknown

    @property
    def icon(self) -> str | None:
        """Return icon based on state."""
        # Show lightning bolt when state is unknown, toggle switch when state is known
        if self._attr_is_on is None:
            return "mdi:lightning-bolt"
        return None  # Use default toggle icon

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        device = (self.coordinator.data or {}).get(self._device_key)
        model = device.device_name if device else "DALI2 Device"

        device_info_dict = DeviceInfo(
            identifiers={(DOMAIN, f"{self._protocol}_{self._address}")},
            name=f"{self._protocol} Address {self._address}",
            model=model,
            via_device=(DOMAIN, self.coordinator.config_entry.entry_id),
        )

        if device:
            if hasattr(device, "manufacturer") and device.manufacturer:
                device_info_dict["manufacturer"] = device.manufacturer
            if hasattr(device, "gtin_decimal") and device.gtin_decimal:
                device_info_dict["model"] = f"{model} (GTIN: {device.gtin_decimal})"
            if hasattr(device, "firmware_version") and device.firmware_version:
                device_info_dict["sw_version"] = device.firmware_version
            if hasattr(device, "hardware_version") and device.hardware_version:
                device_info_dict["hw_version"] = device.hardware_version
            if hasattr(device, "serial_number") and device.serial_number:
                device_info_dict["serial_number"] = device.serial_number

        return device_info_dict

    @property
    def is_on(self) -> bool | None:
        """Return true if the LED is on."""
        # Get tracked state from coordinator
        state = self.coordinator.get_led_state(self._address, self._instance_num)
        # Update internal state for UI consistency
        if state is not None:
            self._attr_is_on = state
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the feedback LED."""
        success = await self._client.set_feedback_led(
            self._address, self._instance_num, True
        )
        if success:
            # Update state in coordinator and internal state
            self.coordinator.set_led_state(self._address, self._instance_num, True)
            self._attr_is_on = True
            self.async_write_ha_state()
        else:
            _LOGGER.error(
                "Failed to turn on feedback LED for device %d instance %d",
                self._address,
                self._instance_num,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the feedback LED."""
        success = await self._client.set_feedback_led(
            self._address, self._instance_num, False
        )
        if success:
            # Update state in coordinator and internal state
            self.coordinator.set_led_state(self._address, self._instance_num, False)
            self._attr_is_on = False
            self.async_write_ha_state()
        else:
            _LOGGER.error(
                "Failed to turn off feedback LED for device %d instance %d",
                self._address,
                self._instance_num,
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = (self.coordinator.data or {}).get(self._device_key)
        if device is None:
            return {"address": self._address, "instance": self._instance_num}
        instance_data = device.instances.get(self._instance_num, {})

        return {
            "address": self._address,
            "instance": self._instance_num,
            "features": instance_data.get("features", []),
            "instance_type": instance_data.get("type"),
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.client.connected
            and self._device_key in (self.coordinator.data or {})
        )


class DaliGroupLight(CoordinatorEntity[LunatoneCoordinator], LightEntity):
    """Representation of a DALI group light (groups 0-15)."""

    _attr_should_poll = False  # State is derived from member lights

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        client,
        group_number: int,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the group light."""
        super().__init__(coordinator)
        self._client = client
        self._group_number = group_number
        self._attr_unique_id = f"{entry.entry_id}_group_{group_number}"
        self._attr_name = f"DALI Group {group_number}"
        
        # Check if any device in this group supports color temp
        supports_color_temp = False
        for device in coordinator.data.values():
            if device.protocol == "DALI" and device.groups and group_number in device.groups:
                if device.supports_color_temp:
                    supports_color_temp = True
                    break
        
        # COLOR_TEMP implies brightness control — never combine with BRIGHTNESS
        if supports_color_temp:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        
        # Device info for DALI Groups
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_groups")},
            name="DALI Groups",
            model="DALI Group Controller",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        # List devices in this group
        devices_in_group = []
        for (protocol, address), device in self.coordinator.data.items():
            if protocol == "DALI" and device.groups and self._group_number in device.groups:
                devices_in_group.append({
                    "address": address,
                    "device_type": device.device_type,
                    "name": device.device_name,
                })
        
        return {
            "group_number": self._group_number,
            "devices": devices_in_group,
            "device_count": len(devices_in_group),
        }

    def _members(self) -> list[DaliDevice]:
        """Return the DALI gear in this group."""
        return [
            device
            for device in (self.coordinator.data or {}).values()
            if device.protocol == "DALI" and self._group_number in device.groups
        ]

    @property
    def is_on(self) -> bool | None:
        """Return true if any light in the group is on."""
        return _aggregate_state(self._members())[0]

    @property
    def brightness(self) -> int | None:
        """Return the mean brightness of the lights in the group that are on."""
        return _aggregate_state(self._members())[1]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the group."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        # Handle color temperature change (if supported)
        if color_temp_kelvin is not None and ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            kelvin = max(
                MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, color_temp_kelvin)
            )
            await self._client.set_group_color_temp(self._group_number, kelvin)

        # Handle brightness change or turn on
        if brightness is not None:
            # Convert from 0-255 to 0-254 DALI level
            dali_level = round((brightness / 255) * 254)
            await self._client.set_group_brightness(self._group_number, dali_level)
        elif color_temp_kelvin is None:
            # Only turn on to max if neither brightness nor color temp was specified
            await self._client.set_group_brightness(self._group_number, 254)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the group."""
        await self._client.set_group_brightness(self._group_number, 0)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._client.connected


class DaliBroadcastLight(CoordinatorEntity[LunatoneCoordinator], LightEntity):
    """Representation of DALI broadcast control (all devices)."""

    _attr_should_poll = False  # State is derived from all lights

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        client,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the broadcast light."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_broadcast"
        self._attr_name = "DALI Broadcast (All Devices)"
        
        # Check if any device supports color temp
        supports_color_temp = any(
            device.supports_color_temp
            for device in coordinator.data.values()
            if device.protocol == "DALI"
        )
        
        # COLOR_TEMP implies brightness control — never combine with BRIGHTNESS
        if supports_color_temp:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        
        # Device info for DALI Groups
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_groups")},
            name="DALI Groups",
            model="DALI Group Controller",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        # Count DALI devices
        dali_device_count = sum(
            1 for device in self.coordinator.data.values()
            if device.protocol == "DALI"
        )
        
        return {
            "description": "Controls all DALI devices simultaneously",
            "device_count": dali_device_count,
        }

    def _members(self) -> list[DaliDevice]:
        """Return all DALI gear."""
        return [
            device
            for device in (self.coordinator.data or {}).values()
            if device.protocol == "DALI"
        ]

    @property
    def is_on(self) -> bool | None:
        """Return true if any DALI light is on."""
        return _aggregate_state(self._members())[0]

    @property
    def brightness(self) -> int | None:
        """Return the mean brightness of the DALI lights that are on."""
        return _aggregate_state(self._members())[1]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on all devices via broadcast."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        # Handle color temperature change (if supported)
        if color_temp_kelvin is not None and ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            kelvin = max(
                MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, color_temp_kelvin)
            )
            await self._client.set_broadcast_color_temp(kelvin)

        # Handle brightness change or turn on
        if brightness is not None:
            # Convert from 0-255 to 0-254 DALI level
            dali_level = round((brightness / 255) * 254)
            await self._client.set_broadcast_brightness(dali_level)
        elif color_temp_kelvin is None:
            # Only turn on to max if neither brightness nor color temp was specified
            await self._client.set_broadcast_brightness(254)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off all devices via broadcast."""
        await self._client.set_broadcast_brightness(0)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._client.connected

