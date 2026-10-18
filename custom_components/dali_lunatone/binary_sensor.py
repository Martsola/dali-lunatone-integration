"""Binary sensor platform for Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .lunatone_api import device_label, bus_model, line_device_identifier, split_address
from .const import DOMAIN, FEATURE_TYPES
from .coordinator import LunatoneCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DALI2 binary sensors from a config entry."""
    coordinator: LunatoneCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    _LOGGER.info("Setting up DALI2 binary sensors")
    _LOGGER.debug("Coordinator devices: %s", list(coordinator.client.devices.keys()))

    entities = []
    for (protocol, address), device in coordinator.client.devices.items():
        _LOGGER.debug("Checking device %s %d: protocol=%s, has_instances=%s, instances=%s",
                     protocol, address, protocol, hasattr(device, 'instances'),
                     device.instances if hasattr(device, 'instances') else None)
        if protocol == "DALI2" and device.instances:
            for instance_num, instance_data in device.instances.items():
                instance_type = instance_data.get("type", 0)
                
                # Binary sensor instance types:
                # iT1 = Push Button (type 1)
                # iT2 = Absolute Input Device/Switch (type 2)
                # iT3 = Occupancy Sensor (type 3)
                if instance_type in (1, 2, 3):
                    # Ensure instance_num is int
                    inst_num = int(instance_num) if isinstance(instance_num, str) else instance_num
                    entities.append(
                        DaliBinarySensor(
                            coordinator,
                            protocol,
                            address,
                            inst_num,
                            instance_type,
                            device,
                            entry.entry_id,
                        )
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DALI2 binary sensors", len(entities))


class DaliBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a DALI2 binary sensor (pushbutton or occupancy)."""

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        protocol: str,
        address: int,
        instance_num: int,
        instance_type: int,
        device: Any,
        entry_id: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._protocol = protocol
        self._address = address
        self._instance_num = instance_num
        self._instance_type = instance_type
        self._device = device
        self._attr_has_entity_name = True

        # Set device class based on instance type
        if instance_type == 3:  # iT3 = Occupancy Sensor
            self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
            instance_name = "Occupancy"
        elif instance_type == 1:  # iT1 = Push Button
            self._attr_device_class = None
            instance_name = f"Button {instance_num}"
        elif instance_type == 2:  # iT2 = Switch
            self._attr_device_class = BinarySensorDeviceClass.POWER
            instance_name = f"Switch {instance_num}"
        else:
            self._attr_device_class = None
            instance_name = f"Input {instance_num}"

        # Unique ID
        self._attr_unique_id = (
            f"{entry_id}_{protocol}_{address}_instance_{instance_num}"
        )

        # Entity name
        self._attr_name = instance_name

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info, reading fresh data from coordinator."""
        device_key = (self._protocol, self._address)
        device_info_dict = {
            "identifiers": {(DOMAIN, f"{self._protocol}_{self._address}")},
            "name": device_label(self._protocol, self._address),
            "model": bus_model(self._address, self._device.device_name),
            "via_device": (
                DOMAIN,
                line_device_identifier(self.coordinator.config_entry.entry_id, split_address(self._address)[0]),
            ),
        }
        
        # Try to get fresh device data from coordinator
        if device_key in self.coordinator.data:
            device = self.coordinator.data[device_key]
            # Add extended device information if available
            if hasattr(device, "manufacturer") and device.manufacturer:
                device_info_dict["manufacturer"] = device.manufacturer
            if hasattr(device, "gtin_decimal") and device.gtin_decimal:
                device_info_dict["model"] = bus_model(self._address, f"{device.device_name} (GTIN: {device.gtin_decimal})")
            if hasattr(device, "firmware_version") and device.firmware_version:
                device_info_dict["sw_version"] = device.firmware_version
            if hasattr(device, "hardware_version") and device.hardware_version:
                device_info_dict["hw_version"] = device.hardware_version
            if hasattr(device, "serial_number") and device.serial_number:
                device_info_dict["serial_number"] = device.serial_number
        
        return device_info_dict

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        # Check coordinator data for this instance state
        device_key = (self._protocol, self._address)
        if device_key in self.coordinator.data:
            device = self.coordinator.data[device_key]
            if hasattr(device, "instances") and device.instances:
                # instances dict uses integer keys
                instance = device.instances.get(self._instance_num)
                if instance:
                    return instance.get("state", False)
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}
        device_key = (self._protocol, self._address)
        if device_key in self.coordinator.data:
            device = self.coordinator.data[device_key]
            if hasattr(device, "instances") and device.instances:
                instance = device.instances.get(self._instance_num)
                if instance:
                    # Add feedback LED capability info
                    has_feedback_led = instance.get("has_feedback_led", False)
                    if has_feedback_led:
                        attributes["has_feedback_led"] = True
                        attributes["led_controllable"] = True
                    
                    # Add feature list
                    features = instance.get("features", [])
                    if features:
                        feature_names = [FEATURE_TYPES.get(f, f"Type {f}") for f in features]
                        attributes["features"] = feature_names
                    
                    # Add last event type for buttons and switches
                    if self._instance_type in (1, 2):  # Push button or Switch
                        event_type = instance.get("event_type")
                        if event_type:
                            attributes["last_event_type"] = event_type
                        event_data = instance.get("event_data")
                        if event_data is not None:
                            attributes["last_event_data"] = event_data
                    # Add movement detection for occupancy sensors
                    elif self._instance_type == 3:  # Occupancy sensor
                        movement = instance.get("movement")
                        if movement is not None:
                            attributes["movement_detected"] = movement
        return attributes

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device_key = (self._protocol, self._address)
        return device_key in self.coordinator.data
