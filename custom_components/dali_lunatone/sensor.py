"""Sensor platform for Lunatone DALI-2 IoT integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .lunatone_api import device_label, bus_model, line_device_identifier, split_address
from .const import DOMAIN
from .coordinator import LunatoneCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DALI2 sensors from a config entry."""
    coordinator: LunatoneCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = []
    for (protocol, address), device in coordinator.client.devices.items():
        if protocol == "DALI2" and device.instances:
            for instance_num, instance_data in device.instances.items():
                instance_type = instance_data.get("type", 0)
                
                # iT4 = Light Sensor (type 4)
                if instance_type == 4:
                    # Ensure instance_num is int
                    inst_num = int(instance_num) if isinstance(instance_num, str) else instance_num
                    entities.append(
                        DaliLightSensor(
                            coordinator,
                            protocol,
                            address,
                            inst_num,
                            device,
                            entry.entry_id,
                        )
                    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DALI2 sensors", len(entities))


class DaliLightSensor(CoordinatorEntity, SensorEntity):
    """Representation of a DALI2 light sensor."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX

    def __init__(
        self,
        coordinator: LunatoneCoordinator,
        protocol: str,
        address: int,
        instance_num: int,
        device: Any,
        entry_id: str,
    ) -> None:
        """Initialize the light sensor."""
        super().__init__(coordinator)
        self._protocol = protocol
        self._address = address
        self._instance_num = instance_num
        self._device = device
        self._attr_has_entity_name = True

        # Unique ID
        self._attr_unique_id = (
            f"{entry_id}_{protocol}_{address}_instance_{instance_num}"
        )

        # Entity name
        self._attr_name = "Illuminance"

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
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        # Check coordinator data for this instance state
        device_key = (self._protocol, self._address)
        if device_key in self.coordinator.data:
            device = self.coordinator.data[device_key]
            if hasattr(device, "instances") and device.instances:
                # instances dict uses integer keys
                instance = device.instances.get(self._instance_num)
                if instance:
                    return instance.get("value")
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device_key = (self._protocol, self._address)
        return device_key in self.coordinator.data
