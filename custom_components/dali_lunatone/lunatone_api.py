"""Lunatone DALI-2 IoT WebSocket API client."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import websockets
from websockets.client import WebSocketClientProtocol

from .const import (
    CMD_ACTIVATE,
    CMD_CONTINUOUS_DOWN,
    CMD_CONTINUOUS_UP,
    CMD_DOWN,
    CMD_DT8_QUERY_COLOUR_TYPE,
    CMD_ENABLE_DT8,
    CMD_GO_TO_LAST,
    CMD_GO_TO_SCENE_0,
    CMD_GO_TO_SCENE_15,
    CMD_OFF,
    CMD_ON_AND_STEP_UP,
    CMD_QUERY_ACTUAL_LEVEL,
    CMD_QUERY_CONTENT_DTR,
    CMD_QUERY_DEVICE_CAPABILITIES,
    CMD_QUERY_DEVICE_TYPE,
    CMD_QUERY_FEATURE_TYPE,
    CMD_QUERY_GROUPS_0_7,
    CMD_QUERY_GROUPS_8_15,
    CMD_QUERY_INSTANCE_TYPE,
    CMD_QUERY_NEXT_DEVICE_TYPE,
    CMD_QUERY_NEXT_FEATURE_TYPE,
    CMD_QUERY_NUMBER_OF_INSTANCES,
    CMD_QUERY_STATUS,
    CMD_READ_MEMORY_LOCATION,
    CMD_RECALL_MAX,
    CMD_RECALL_MAX_LEVEL,
    CMD_RECALL_MIN,
    CMD_SET_DTR0,
    CMD_SET_DTR1,
    CMD_SET_TEMP_COLOR_TEMP,
    CMD_STEP_DOWN,
    CMD_STEP_DOWN_AND_OFF,
    CMD_STEP_UP,
    CMD_STORE_ACTUAL_LEVEL_IN_DTR,
    CMD_UP,
    CONNECTION_TIMEOUT,
    DALI_LINE_SPAN,
    DALI_MASK,
    EXTERNAL_REFRESH_DELAY,
    FEATURE_TYPES,
    FEEDBACK_LED_OFF,
    FEEDBACK_LED_ON,
    GTIN_MANUFACTURERS,
    RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)


def line_address(line: int, short_address: int) -> int:
    """Return the device key address for a short address on a DALI line."""
    return line * DALI_LINE_SPAN + short_address


def split_address(address: int, line: int = 0) -> tuple[int, int]:
    """Return (line, short address) for a device key address.

    Key addresses below DALI_LINE_SPAN are plain line-0 short addresses, so the
    explicit ``line`` argument is only used for those.
    """
    if address >= DALI_LINE_SPAN:
        return address // DALI_LINE_SPAN, address % DALI_LINE_SPAN
    return line, address


def line_device_identifier(entry_id: str, line: int) -> str:
    """Return the device-registry identifier of the "DALI Bus N" device for a line."""
    return f"{entry_id}_line_{line}"


def bus_model(address: int, model: str) -> str:
    """Prefix a device model with its DALI bus, e.g. "Bus 1 · DALI DT6", so the bus shows in device lists."""
    return f"Bus {split_address(address)[0]} · {model}"


def device_label(protocol: str, address: int) -> str:
    """Return the default device name, e.g. "DALI Address 4" or "DALI Line 1 Address 4"."""
    line, short_address = split_address(address)
    if line == 0:
        return f"{protocol} Address {short_address}"
    return f"{protocol} Line {line} Address {short_address}"


def gtin_hex_to_decimal(gtin_hex: str) -> str | None:
    """Convert GTIN from hex string to decimal.
    
    Args:
        gtin_hex: GTIN as hex string (e.g., "009AA6327EF6")
        
    Returns:
        GTIN as decimal string (e.g., "4062172072212"), or None if invalid/empty
    """
    if not gtin_hex or gtin_hex == "000000000000" or all(c in "0Ff" for c in gtin_hex):
        return None
    
    try:
        # Convert hex to integer, then to decimal string
        gtin_int = int(gtin_hex, 16)
        if gtin_int == 0:
            return None
        return str(gtin_int)
    except (ValueError, TypeError):
        return None


def get_manufacturer_from_gtin(gtin_decimal: str) -> str | None:
    """Get manufacturer name from GTIN decimal string.
    
    Args:
        gtin_decimal: GTIN as decimal string (e.g., "4062172072212")
        
    Returns:
        Manufacturer name (e.g., "Osram"), or None if not found
    """
    if not gtin_decimal:
        return None
    
    # Check each known prefix
    for prefix, manufacturer in GTIN_MANUFACTURERS.items():
        if gtin_decimal.startswith(prefix):
            return manufacturer
    
    return None


def format_firmware_version(fw_bytes: list[int]) -> str | None:
    """Format firmware version bytes into readable string.
    
    Args:
        fw_bytes: List of 4 bytes representing firmware version
        
    Returns:
        Formatted version (e.g., "3.12"), or None if all zeros
    """
    if not fw_bytes or all(b == 0 for b in fw_bytes):
        return None
    
    # For 4-byte firmware: major.minor (ignore patch/build if zero)
    if len(fw_bytes) >= 2:
        major, minor = fw_bytes[0], fw_bytes[1]
        if len(fw_bytes) >= 4 and (fw_bytes[2] != 0 or fw_bytes[3] != 0):
            return f"{major}.{minor}.{fw_bytes[2]}.{fw_bytes[3]}"
        return f"{major}.{minor}"
    
    return None


def format_hardware_version(hw_bytes: list[int]) -> str | None:
    """Format hardware version bytes into readable string.
    
    Args:
        hw_bytes: List of 2 bytes representing hardware version
        
    Returns:
        Formatted version (e.g., "2.0"), or None if all zeros
    """
    if not hw_bytes or all(b == 0 for b in hw_bytes):
        return None
    
    if len(hw_bytes) >= 2:
        return f"{hw_bytes[0]}.{hw_bytes[1]}"
    
    return None


def format_serial_number(serial_hex: str) -> str | None:
    """Format serial number for display.
    
    The 64-bit ID contains the actual serial number in bytes 2-5 (middle 4 bytes)
    encoded as a big-endian 32-bit integer.
    
    Args:
        serial_hex: Serial as hex string (e.g., "00040001D0B10300")
        
    Returns:
        Serial as decimal string (e.g., "118961"), or None if all zeros
    """
    if not serial_hex or all(c in "0Ff" for c in serial_hex):
        return None
    
    try:
        # Extract bytes 2-5 (middle 4 bytes) from the 64-bit ID
        # Example: 00040001D0B10300 -> bytes 2-5 = 0001D0B1 = 118961
        if len(serial_hex) >= 12:
            middle_bytes = serial_hex[4:12]  # Bytes 2-5 (skip first 2 bytes, take next 4)
            import struct
            serial_int = struct.unpack('>I', bytes.fromhex(middle_bytes))[0]  # Big-endian 32-bit
            if serial_int == 0:
                return None
            return str(serial_int)
        else:
            # Fallback for shorter IDs (shouldn't happen with 64-bit IDs)
            serial_int = int(serial_hex, 16)
            if serial_int == 0:
                return None
            return str(serial_int)
    except (ValueError, TypeError):
        return None


class DaliDevice:
    """Represents a DALI device on the bus."""

    def __init__(
        self,
        address: int,
        protocol: str,
        device_type: int,
        device_name: str,
        capabilities: int | None = None,
    ) -> None:
        """Initialize DALI device."""
        self.address = address
        self.protocol = protocol
        self.device_type = device_type
        self.device_name = device_name
        self.capabilities = capabilities
        self.brightness: int | None = None
        self.is_on = False
        self.color_temp: int | None = None
        self.groups: list[int] = []  # List of group numbers (0-15) this device belongs to
        self.instances: dict[int, dict[str, Any]] = {}  # DALI2 instances {instance_num: {type, enabled, etc}}
        self.num_instances: int = 0
        
        # Multi-type device support: list of all DALI device types this gear supports.
        # Single-type devices have an empty list; the primary type is always device_type.
        # Example: a device reporting DT6+DT8 will have device_type=8, device_types=[6, 8]
        self.device_types: list[int] = []
        
        # Extended device information (from Memory Bank 0)
        self.gtin: str | None = None  # Global Trade Item Number (hex format)
        self.firmware_version: str | None = None  # e.g., "7.0"
        self.hardware_version: str | None = None  # e.g., "1.0"
        self.identification_number: str | None = None  # 64-bit unique ID (hex format)

    @property
    def unique_id(self) -> str:
        """Return unique identifier for this device."""
        return f"{self.protocol.lower()}_{self.address}"
    
    @property
    def gtin_decimal(self) -> str | None:
        """Return GTIN as decimal string."""
        return gtin_hex_to_decimal(self.gtin) if self.gtin else None
    
    @property
    def manufacturer(self) -> str | None:
        """Return manufacturer name based on GTIN."""
        gtin_dec = self.gtin_decimal
        return get_manufacturer_from_gtin(gtin_dec) if gtin_dec else None
    
    @property
    def serial_number(self) -> str | None:
        """Return serial number as decimal string."""
        return format_serial_number(self.identification_number) if self.identification_number else None

    @property
    def device_types_label(self) -> str:
        """Return a human-readable label of all supported device types.
        
        For single-type devices returns e.g. "DT8".
        For multi-type devices returns e.g. "DALI DT6,8".
        """
        if self.device_types and len(self.device_types) > 1:
            return "DALI DT" + ",".join(str(t) for t in sorted(self.device_types))
        return f"DT{self.device_type}"

    @property
    def is_multitype(self) -> bool:
        """Return True if this device supports multiple DALI device types."""
        return len(self.device_types) > 1

    @property
    def supports_color_temp(self) -> bool:
        """Check if device supports color temperature (DT8)."""
        return self.device_type == 8
    
    @property
    def is_dali2(self) -> bool:
        """Check if device supports DALI2."""
        return self.protocol == "DALI2"

    @property
    def line(self) -> int:
        """Return the DALI line (bus) the device is on."""
        return split_address(self.address)[0]

    @property
    def short_address(self) -> int:
        """Return the device's short address on its line (0-63)."""
        return split_address(self.address)[1]

    def __repr__(self) -> str:
        """Return string representation."""
        groups_str = f", groups={self.groups}" if self.groups else ""
        instances_str = f", instances={self.num_instances}" if self.num_instances else ""
        types_str = f", all_types={self.device_types}" if self.is_multitype else ""
        return (
            f"DaliDevice(addr={self.address}, type={self.device_type}, "
            f"name={self.device_name}, protocol={self.protocol}{types_str}{groups_str}{instances_str})"
        )


class LunatoneClient:
    """Client for Lunatone DALI-2 IoT interface via WebSocket."""

    def __init__(self, host: str, port: int = 80) -> None:
        """Initialize the Lunatone client."""
        self.host = host
        self.port = port
        self.url = f"ws://{host}:{port}"
        self._ws: WebSocketClientProtocol | None = None
        self._request_id = 0
        self._connected = False
        self._reconnect_task: asyncio.Task | None = None
        self._message_handler_task: asyncio.Task | None = None
        self._devices: dict[tuple[str, int], DaliDevice] = {}  # Key: (protocol, address)
        self._device_info: dict[str, Any] = {}
        self._callbacks: list[Callable[[str, Any], None]] = []
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._response_queue: asyncio.Queue = asyncio.Queue()  # Queue for daliFrame responses
        self._recent_monitor_frames: dict[str, float] = {}  # Cache for deduplicating daliMonitor frames
        self.lines: list[int] = [0]  # DALI lines to scan and follow (set from the config entry options)
        self._pending_refresh: set[int] = set()  # Short addresses to re-query after external commands
        self._refresh_due: float = 0.0
        self._refresh_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        """Return connection status."""
        return self._connected and self._ws is not None

    @property
    def devices(self) -> dict[tuple[str, int], DaliDevice]:
        """Return discovered devices."""
        return self._devices

    @property
    def device_info(self) -> dict[str, Any]:
        """Return Lunatone device information."""
        return self._device_info

    def _extract_response_value(self, result: dict[str, Any] | None) -> int | None:
        """Extract value from DALI response (handles daliAnswer and daliFrame formats).
        
        Returns None if no device responded (no daliData field in response).
        The gateway returns a response even when no device answers, but without daliData field.
        """
        if not result:
            return None
        
        # Check for daliData field - its presence indicates a device responded
        # daliAnswer format: {"line": 0, "result": X, "daliData": value}
        if "daliData" in result:
            if isinstance(result["daliData"], int):
                return result["daliData"]
            # daliFrame format (8-bit): {"numberOfBits": 8, "daliData": [value]}
            if isinstance(result["daliData"], list) and result["daliData"]:
                return result["daliData"][0]
        
        # No daliData field means no device responded
        return None

    def add_callback(self, callback: Callable[[str, Any], None]) -> None:
        """Add callback for device updates."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[str, Any], None]) -> None:
        """Remove callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def connect(self) -> bool:
        """Connect to Lunatone device."""
        try:
            _LOGGER.debug("Connecting to Lunatone at %s", self.url)
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url), timeout=CONNECTION_TIMEOUT
            )
            self._connected = True
            _LOGGER.info("Connected to Lunatone DALI-2 IoT interface at %s", self.url)

            # Start message handler - it will process the initial info message
            self._message_handler_task = asyncio.create_task(self._message_handler())

            # Wait a moment for initial info message to be processed
            await asyncio.sleep(0.5)

            return True

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout connecting to Lunatone at %s", self.url)
            return False
        except Exception as e:
            _LOGGER.error("Error connecting to Lunatone: %s", e)
            return False

    async def disconnect(self) -> None:
        """Disconnect from Lunatone device."""
        self._connected = False

        # Cancel tasks
        if self._message_handler_task:
            self._message_handler_task.cancel()
            try:
                await self._message_handler_task
            except asyncio.CancelledError:
                pass

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._pending_refresh.clear()

        # Close WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        _LOGGER.info("Disconnected from Lunatone")

    async def _message_handler(self) -> None:
        """Handle incoming WebSocket messages."""
        while self._connected and self._ws:
            try:
                message = await self._ws.recv()
                data = json.loads(message)
                # Log all received messages for debugging
                _LOGGER.debug("Raw WebSocket message: %s", message)
                await self._handle_message(data)
            except websockets.ConnectionClosed:
                _LOGGER.warning("WebSocket connection closed")
                self._connected = False
                asyncio.create_task(self._reconnect())
                break
            except Exception as e:
                _LOGGER.error("Error handling message: %s", e)

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Process incoming message."""
        message_type = data.get("type")
        
        # Handle info messages (device information)
        if message_type == "info":
            if "data" in data:
                self._device_info = data["data"]
                _LOGGER.info(
                    "Device: %s, Version: %s, Serial: %s",
                    self._device_info.get("name"),
                    self._device_info.get("version"),
                    self._device_info.get("device", {}).get("serial"),
                )
            return
        
        # Handle daliAnswer messages (query responses)
        if message_type == "daliAnswer":
            frame_data = data.get("data", {})
            _LOGGER.debug("Received daliAnswer: %s", frame_data)
            # Put answer in response queue
            await self._response_queue.put(frame_data)
            return
        
        # Handle daliMonitor messages (unsolicited bus events from gateway)
        if message_type == "daliMonitor":
            frame_data = data.get("data", {})
            num_bits = frame_data.get("bits", 16)
            dali_data = frame_data.get("data", [])
            
            _LOGGER.debug("Received daliMonitor (%d bits): %s", num_bits, dali_data)
            
            # Deduplicate frames - gateway sends duplicates for reliability
            # Create unique key from frame data and timestamp
            line = frame_data.get("line", 0)
            frame_key = f"{line}:{num_bits}:{','.join(map(str, dali_data))}"
            current_time = frame_data.get("timestamp", 0)
            
            # Check if we've seen this frame very recently (within 0.5 seconds)
            if frame_key in self._recent_monitor_frames:
                last_time = self._recent_monitor_frames[frame_key]
                if current_time - last_time < 0.5:
                    _LOGGER.debug("Skipping duplicate daliMonitor frame: %s", frame_key)
                    return
            
            # Store this frame in cache
            self._recent_monitor_frames[frame_key] = current_time
            
            # Clean old entries from cache (older than 2 seconds)
            expired_keys = [k for k, t in self._recent_monitor_frames.items() if current_time - t > 2.0]
            for k in expired_keys:
                del self._recent_monitor_frames[k]
            
            # Devices are only known on the configured lines; frames from other
            # lines would be attributed to the wrong gear
            if line not in self.lines:
                return

            # Check if this is a DALI2 event (24-bit frame)
            if num_bits == 24 and len(dali_data) == 3:
                # Reformat to match expected structure
                event_data = {"daliData": dali_data, "line": line}
                await self._handle_dali2_event(event_data)
            elif num_bits == 16 and len(dali_data) == 2:
                # Handle 16-bit DALI command for external device control monitoring
                # Only process external commands (not our own queries)
                is_external = frame_data.get("externalSource", False)
                is_query = frame_data.get("query", False)
                if is_external and not is_query:
                    event_data = {"daliData": dali_data, "line": line}
                    await self._handle_dali_command(event_data)
            return
        
        # Handle daliFrame messages (command echoes)
        if message_type == "daliFrame":
            frame_data = data.get("data", {})
            num_bits = frame_data.get("numberOfBits", 16)
            _LOGGER.debug("Received daliFrame (%d bits): %s", num_bits, frame_data)
            
            # These are usually command echoes, not events
            # Only process if they contain daliData
            dali_data = frame_data.get("daliData")
            if dali_data:
                # Check if this is a DALI2 event (24-bit frame)
                if num_bits == 24:
                    await self._handle_dali2_event(frame_data)
                else:
                    # Handle 16-bit DALI command for external device control monitoring
                    await self._handle_dali_command(frame_data)

    async def _handle_dali2_event(self, data: dict[str, Any]) -> None:
        """Handle DALI2 instance controller events (24-bit frames)."""
        try:
            dali_data = data.get("daliData", [])
            if not dali_data or len(dali_data) != 3:
                return

            # Parse 24-bit DALI2 event
            dali_address = dali_data[0] >> 1  # Remove LSB bit
            instance_byte = dali_data[1]
            event_data = dali_data[2]

            # Check if this is an instance event (instanceByte >= 128)
            if instance_byte < 128:
                return

            # Extract instance number with bit shifting
            instance = (instance_byte - 128) >> 2
            
            _LOGGER.debug("DALI2 event: address=%d, instance=%d, event_data=%d", dali_address, instance, event_data)

            # Find the DALI2 device
            device_key = ("DALI2", line_address(data.get("line", 0), dali_address))
            if device_key not in self._devices:
                _LOGGER.debug("DALI2 event for unknown device: address=%d", dali_address)
                return

            device = self._devices[device_key]
            
            # Check if device has instances attribute
            if not hasattr(device, 'instances'):
                _LOGGER.error("DALI2 device at address %d has no instances attribute!", dali_address)
                return
            
            if not device.instances:
                _LOGGER.debug("DALI2 device at address %d has no instances configured, ignoring event", dali_address)
                return
            
            # Get instance info (instances stored with integer keys)
            if instance not in device.instances:
                _LOGGER.debug("DALI2 event for unknown instance: address=%d, instance=%d, known instances=%s", dali_address, instance, list(device.instances.keys()))
                return

            instance_info = device.instances[instance]
            instance_type = instance_info.get("type", 0)

            # Update instance state based on type
            if instance_type == 1:
                # iT1: Push Button - Per IEC 62386-301, the event information
                # uniquely defines the event. No separate counter/timing field.
                # The full byte value IS the event type identifier.
                # Values: 0=released, 1=pressed, 2=short_press, 5=double_press,
                #   9=long_press_start, 11=long_press_repeat, 12=long_press_stop,
                #   14=button_free, 15=button_stuck
                event_type = event_data  # Full byte is the event identifier
                # State is True for any "press" event (active button interaction)
                # Short press (2) and double press (5) are momentary - coordinator
                # will auto-reset them to False after a brief delay
                instance_info["state"] = event_type in (1, 2, 5, 9, 11)
                instance_info["event_data"] = event_data
                instance_info["event_type"] = self._decode_button_event(event_type)
            elif instance_type == 2:
                # iT2: Absolute Input Device/Switch - same event info structure
                event_type = event_data  # Full byte is the event identifier
                instance_info["state"] = event_type in (1, 2, 5, 9, 11)
                instance_info["event_data"] = event_data
                instance_info["event_type"] = self._decode_button_event(event_type)
            elif instance_type == 3:
                # iT3: Occupancy Sensor
                bits21 = (event_data >> 1) & 0x03
                instance_info["state"] = bits21 in (0b01, 0b11)  # Occupied or Still Occupied
                movement = (event_data & 0x01) == 1
                instance_info["movement"] = movement
                instance_info["event_data"] = event_data
                instance_info["event_type"] = self._decode_occupancy_event(event_data)
            elif instance_type == 4:
                # iT4: Light Sensor
                instance_info["value"] = event_data  # Lux value
                instance_info["event_data"] = event_data
            elif instance_type in (5, 6):
                # iT5: Colour Sensor, iT6: General Purpose Sensor
                instance_info["value"] = event_data
                instance_info["event_data"] = event_data

            # Notify callbacks for state updates
            for callback in self._callbacks:
                callback("dali2_event", device, instance, instance_info)
                
        except Exception as e:
            _LOGGER.error("Error in _handle_dali2_event: %s", e, exc_info=True)


    def _decode_occupancy_event(self, event_data: int) -> str:
        """Decode occupancy sensor event type from IEC 62386-303 event info.

        Bit 0: movement (1=detected, 0=no movement)
        Bits 2:1: occupancy state
            00 = vacant
            01 = occupied
            10 = unknown
            11 = still occupied
        """
        movement = (event_data & 0x01) == 1
        occupancy_bits = (event_data >> 1) & 0x03

        if occupancy_bits == 0b01:
            return "occupied"
        elif occupancy_bits == 0b11:
            return "still_occupied"
        elif occupancy_bits == 0b00:
            return "vacant"
        elif movement:
            return "movement_detected"
        else:
            return "no_movement"

    def _decode_button_event(self, event_type: int) -> str:
        """Decode pushbutton event type from IEC 62386-301 event info.
        
        Per the DALI-2 standard, the event information uniquely defines
        the event for push buttons - there is no separate counter field.
        
        Event info values:
        0  - Button released
        1  - Button pressed
        2  - Short press
        5  - Double press
        9  - Long press start
        11 - Long press repeat
        12 - Long press stop
        14 - Button free (was stuck, now released)
        15 - Button stuck
        """
        from .const import BUTTON_EVENT_TYPES
        return BUTTON_EVENT_TYPES.get(event_type, f"unknown_event_{event_type}")

    def _resolve_command_targets(self, address_byte: int, line: int = 0) -> list[DaliDevice] | None:
        """Return the DALI gear on ``line`` addressed by the address byte of a forward frame.

        Returns None for special commands (0xA0-0xFD), which do not address gear.
        """
        lights = [
            device
            for (protocol, _), device in self._devices.items()
            if protocol == "DALI" and device.line == line
        ]
        if address_byte <= 0x7F:  # Short address: 0AAAAAAS
            address = address_byte >> 1
            return [device for device in lights if device.short_address == address]
        if address_byte <= 0x9F:  # Group address: 100GGGGS
            group_number = (address_byte >> 1) & 0x0F
            return [device for device in lights if group_number in device.groups]
        if address_byte >= 0xFE:  # Broadcast: 1111111S
            return lights
        return None

    def _apply_gear_state(
        self, devices: list[DaliDevice], is_on: bool, brightness: int | None
    ) -> None:
        """Update tracked state of gear changed by a bus command and notify."""
        for device in devices:
            old_state = (device.is_on, device.brightness)
            device.is_on = is_on
            if brightness is not None:
                device.brightness = brightness
            elif is_on and not device.brightness:
                # Turned on to a level we don't know yet; the scheduled refresh fills it in
                device.brightness = None
            if (device.is_on, device.brightness) != old_state:
                for callback in self._callbacks:
                    callback("state_update", device)

    async def _handle_dali_command(self, data: dict[str, Any]) -> None:
        """Handle DALI command notifications from the bus (external device control).

        Commands with a known outcome (DAPC, OFF) update state immediately. Commands
        whose resulting level depends on gear settings (RECALL MAX/MIN, scenes, steps,
        dimming) schedule a re-query of only the addressed gear.
        """
        dali_data = data.get("daliData", [])
        if not dali_data or len(dali_data) != 2:
            return

        address_byte, opcode = dali_data
        targets = self._resolve_command_targets(address_byte, data.get("line", 0))
        if not targets:
            return

        # Direct arc power control (S bit = 0): the second byte is the target level
        if (address_byte & 0x01) == 0:
            if opcode == DALI_MASK:
                return
            _LOGGER.debug(
                "External DAPC: address byte 0x%02X, level=%d, %d device(s)",
                address_byte, opcode, len(targets),
            )
            self._apply_gear_state(targets, opcode > 0, round((opcode / 254) * 100))
            return

        if opcode == CMD_OFF:
            _LOGGER.debug("External OFF: address byte 0x%02X, %d device(s)", address_byte, len(targets))
            self._apply_gear_state(targets, False, 0)
        elif opcode in (CMD_RECALL_MAX, CMD_RECALL_MIN, CMD_ON_AND_STEP_UP, CMD_GO_TO_LAST):
            _LOGGER.debug(
                "External ON-type command %d: address byte 0x%02X, %d device(s)",
                opcode, address_byte, len(targets),
            )
            self._apply_gear_state(targets, True, None)
            self._schedule_refresh(targets)
        elif opcode in (
            CMD_UP,
            CMD_DOWN,
            CMD_STEP_UP,
            CMD_STEP_DOWN,
            CMD_STEP_DOWN_AND_OFF,
            CMD_CONTINUOUS_UP,
            CMD_CONTINUOUS_DOWN,
        ) or CMD_GO_TO_SCENE_0 <= opcode <= CMD_GO_TO_SCENE_15:
            _LOGGER.debug(
                "External level command %d: address byte 0x%02X, %d device(s)",
                opcode, address_byte, len(targets),
            )
            self._schedule_refresh(targets)

    def _schedule_refresh(self, devices: list[DaliDevice]) -> None:
        """Re-query the given gear once external commands have been quiet for a moment."""
        self._pending_refresh.update(device.address for device in devices)
        self._refresh_due = asyncio.get_running_loop().time() + EXTERNAL_REFRESH_DELAY
        # The message handler must not wait for query answers itself (it is what
        # receives them), so the queries run in their own task
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._run_external_refresh())

    async def _run_external_refresh(self) -> None:
        """Query actual levels for gear touched by external commands."""
        loop = asyncio.get_running_loop()
        while self._pending_refresh:
            # Commands arriving while waiting push the deadline out (debounce)
            while (remaining := self._refresh_due - loop.time()) > 0:
                await asyncio.sleep(remaining)

            addresses = sorted(self._pending_refresh)
            self._pending_refresh.clear()
            for address in addresses:
                device = self._devices.get(("DALI", address))
                if device is None or not self.connected:
                    continue
                brightness = await self.query_brightness(address)
                if brightness is None:
                    _LOGGER.debug("External refresh: no answer from address %d", address)
                    continue
                self._apply_gear_state([device], brightness > 0, brightness)

    async def _reconnect(self) -> None:
        """Attempt to reconnect to Lunatone."""
        attempt = 0
        while not self._connected:
            attempt += 1
            _LOGGER.info("Reconnection attempt %d...", attempt)
            
            await asyncio.sleep(RECONNECT_DELAY)
            
            try:
                if await self.connect():
                    _LOGGER.info("Reconnected successfully")
                    # Rescan devices after reconnection
                    await self.scan_all_lines()
                    break
            except Exception as e:
                _LOGGER.error("Reconnection failed: %s", e)

    async def _send_request(self, message_type: str, data: dict[str, Any], wait_for_response: bool = False, expected_query: list[int] | None = None) -> Any:
        """Send request in Lunatone format and optionally wait for response."""
        if not self._ws or not self._connected:
            raise ConnectionError("Not connected to Lunatone")

        request = {
            "type": message_type,
            "data": data,
        }

        try:
            await self._ws.send(json.dumps(request))
            _LOGGER.debug("Sent request: %s", request)
            
            # If waiting for response, get it from the queue
            if wait_for_response:
                import time as _time
                deadline = _time.monotonic() + 2.0  # overall 2-second ceiling
                while True:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        _LOGGER.debug("No response received (overall timeout) for query %s", expected_query)
                        return None
                    try:
                        response = await asyncio.wait_for(
                            self._response_queue.get(), timeout=min(1.5, remaining)
                        )
                        # If we know which query this answer belongs to, validate it.
                        # The gateway echoes the original query bytes in the "queryData" field
                        # of every daliAnswer.  Without this check a delayed answer from a
                        # previous address scan can silently corrupt the current result.
                        if expected_query is not None:
                            query_data = response.get("queryData")
                            if query_data != expected_query:
                                _LOGGER.debug(
                                    "Discarding stale daliAnswer: expected queryData=%s, got queryData=%s",
                                    expected_query, query_data,
                                )
                                continue  # pull the next entry from the queue
                        _LOGGER.debug("Received response: %s", response)
                        return response
                    except asyncio.TimeoutError:
                        _LOGGER.debug("No response received (timeout) for query %s", expected_query)
                        return None
            else:
                # Small delay for command processing
                await asyncio.sleep(0.05)
                return None
                
        except Exception as e:
            _LOGGER.error("Error sending request: %s", e)
            raise

    async def _send_dali_frame(
        self,
        line: int,
        dali_data: list[int],
        num_bits: int = 16,
        send_twice: bool = False,
        wait_for_answer: bool = False,
    ) -> Any:
        """Send DALI frame to the bus."""
        data = {
            "line": line,
            "numberOfBits": num_bits,
            "mode": {
                "sendTwice": send_twice,
                "waitForAnswer": wait_for_answer,
                "priority": 3,
            },
            "daliData": dali_data,
        }
        
        return await self._send_request(
            "daliFrame", data,
            wait_for_response=wait_for_answer,
            expected_query=dali_data if wait_for_answer else None,
        )

    async def scan_devices(self, line: int = 0, scan_groups: bool = True, scan_instances: bool = True) -> dict[tuple[str, int], DaliDevice]:
        """Scan for DALI devices on the bus."""
        # Check connection before scanning
        if not self._connected or not self._ws:
            _LOGGER.error("Cannot scan: not connected to Lunatone gateway")
            raise ConnectionError("Not connected to Lunatone gateway")
        
        _LOGGER.info("Scanning for DALI devices on line %d", line)
        
        # Save current devices in case scan fails
        devices_backup = self._devices.copy()
        # Only this line is rescanned; devices found earlier on other lines are kept
        for key in [k for k in self._devices if split_address(k[1])[0] == line]:
            del self._devices[key]

        try:
            # Phase 1: Scan for DALI devices (addresses 0-63)
            _LOGGER.info("Phase 1: Scanning DALI device types...")
            for address in range(64):
                try:
                    # Query device type (command 153)
                    dali_data = [(address << 1) | 1, CMD_QUERY_DEVICE_TYPE]
                    result = await self._send_dali_frame(
                        line, dali_data, wait_for_answer=True
                    )

                    device_type = self._extract_response_value(result)

                    # Handle multi-type DALI gear (IEC 62386-102 §9.10.8):
                    # Gear supporting multiple Device Types responds with MASK (255) to
                    # "Query Device Type".  Confirm presence via "Query Status", then
                    # detect the best supported type.  We prefer DT8 (colour control)
                    # when available because it offers the richest control.
                    if device_type is None:
                        continue  # No device at this address

                    from .const import DALI_DEVICE_TYPES
                    detected_types: list[int] = []

                    if device_type == 255:
                        # --- Multi-type gear detected ---
                        # IEC 62386-102: QUERY DEVICE TYPE returns MASK (255) when gear
                        # supports more than one device type.
                        #
                        # CRITICAL: QUERY NEXT DEVICE TYPE (cmd 167) MUST follow
                        # QUERY DEVICE TYPE without any intervening DALI commands.
                        # ANY other command — including commands from another DALI
                        # controller sharing the same bus/gateway — invalidates the
                        # device type iterator.
                        #
                        # The iterator can be disrupted at ANY point mid-enumeration.
                        # For example, device reports [6, 8, 50, 51, 52, 254].
                        # If the bus is disturbed after returning 6, we receive [6]
                        # then None — and would wrongly accept just [DT6].
                        #
                        # Fix: only consider enumeration complete when the device
                        # sends the 254 end-of-list sentinel.  A None/no-response
                        # exit means the iterator was disrupted mid-enumeration and
                        # we must retry (re-send QUERY DEVICE TYPE to reset the
                        # firmware iterator, then wait for bus to clear).
                        MAX_SCAN_RETRIES = 3
                        iterator_complete = False
                        for _scan_retry in range(MAX_SCAN_RETRIES):
                            if _scan_retry > 0:
                                # Wait for competing bus traffic to clear, then re-init
                                # the iterator by re-sending QUERY DEVICE TYPE.
                                await asyncio.sleep(0.5)
                                retry_result = await self._send_dali_frame(
                                    line,
                                    [(address << 1) | 1, CMD_QUERY_DEVICE_TYPE],
                                    wait_for_answer=True,
                                )
                                if self._extract_response_value(retry_result) != 255:
                                    # Device no longer returns MASK — unexpected; stop.
                                    break
                                _LOGGER.debug(
                                    "Multi-type address %d: retrying iterator (attempt %d/%d)",
                                    address, _scan_retry + 1, MAX_SCAN_RETRIES,
                                )

                            detected_types = []
                            iterator_complete = False
                            for _attempt in range(10):  # at most 10 device types
                                await asyncio.sleep(0.02)
                                ndt_result = await self._send_dali_frame(
                                    line,
                                    [(address << 1) | 1, CMD_QUERY_NEXT_DEVICE_TYPE],
                                    wait_for_answer=True,
                                )
                                ndt_val = self._extract_response_value(ndt_result)
                                if ndt_val == 254:
                                    iterator_complete = True  # clean end-of-list
                                    break
                                if ndt_val is None:
                                    break  # no response — iterator disrupted
                                if ndt_val != 255 and ndt_val not in detected_types:
                                    detected_types.append(ndt_val)

                            # Strip bus-collision artefact 0x55 (wired-AND of all slaves)
                            detected_types = [t for t in detected_types if t != 0x55]

                            if iterator_complete:
                                break  # full list received — no need to retry

                            _LOGGER.debug(
                                "Multi-type address %d: iterator disrupted mid-enumeration "
                                "(got %s so far), retrying",
                                address, detected_types,
                            )

                        # DT8 explicit probe: if the iterator didn't enumerate DT8,
                        # try ENABLE DEVICE TYPE 8 + Query Colour Type as a fallback.
                        if 8 not in detected_types:
                            await asyncio.sleep(0.1)
                            await self._send_dali_frame(
                                line, [0xC1, 8], send_twice=True
                            )
                            await asyncio.sleep(0.05)
                            ct_result = await self._send_dali_frame(
                                line,
                                [(address << 1) | 1, CMD_DT8_QUERY_COLOUR_TYPE],
                                wait_for_answer=True,
                            )
                            if self._extract_response_value(ct_result) not in (None, 255):
                                detected_types.append(8)

                        # Still nothing after retries + DT8 probe: device definitely
                        # exists (MASK was returned) but iterator was never cooperative.
                        # Default to DT6 (LED) so the device at least appears in HA.
                        if not detected_types:
                            _LOGGER.warning(
                                "Multi-type address %d: iterator disrupted on all %d attempts, "
                                "defaulting to DT6",
                                address, MAX_SCAN_RETRIES,
                            )
                            detected_types = [6]

                        # Choose primary type: prefer DT8 > DT6 > first found
                        PREFERRED_ORDER = [8, 6]
                        device_type = next(
                            (t for t in PREFERRED_ORDER if t in detected_types),
                            detected_types[0] if detected_types else 6,
                        )
                        _LOGGER.info(
                            "Multi-type device at address %d: detected_types=%s, using type %d (%s)",
                            address, detected_types, device_type,
                            DALI_DEVICE_TYPES.get(device_type, "Unknown"),
                        )
                    else:
                        detected_types = [device_type]

                    device_name = DALI_DEVICE_TYPES.get(device_type, f"Unknown ({device_type})")

                    # Create DALI device
                    device = DaliDevice(
                        address=line_address(line, address),
                        protocol="DALI",
                        device_type=device_type,
                        device_name=device_name,
                    )
                    # Store all detected types for UI display (e.g. "DALI DT6,8")
                    device.device_types = detected_types

                    self._devices[("DALI", line_address(line, address))] = device
                    _LOGGER.debug("Found DALI device at address %d: type %d (%s)", address, device_type, device_name)

                    # Read extended device information from Memory Bank 0 (DALI1)
                    # Memory Bank 0 structure is the same for DALI1 and DALI2
                    await asyncio.sleep(0.1)
                    device_info = await self.read_dali1_device_info(address, line)
                    
                    if device_info and (device_info.get("gtin") or device_info.get("firmware_version")):
                        # Store hex values internally, formatted values are in properties
                        device.gtin = device_info.get("gtin_hex")  # Hex format for internal storage
                        device.firmware_version = device_info.get("firmware_version")
                        device.hardware_version = device_info.get("hardware_version")
                        device.identification_number = device_info.get("id_hex")  # Hex format for internal storage
                        
                        _LOGGER.debug(
                            "DALI1 Device %d stored: GTIN=%s (decimal=%s), FW=%s, Manufacturer=%s",
                            address,
                            device.gtin,
                            device_info.get("gtin"),
                            device.firmware_version,
                            device.manufacturer or "Unknown"
                        )

                    await asyncio.sleep(0.2)  # Delay between queries

                except Exception as e:
                    _LOGGER.debug("No device at address %d: %s", address, e)

            _LOGGER.info("Phase 1 complete. Found %d DALI devices", len(self._devices))

            # Phase 2: Scan ALL addresses (0-63) for DALI2 capabilities
            # Note: DALI2 devices might not respond to DALI device type queries
            _LOGGER.info("Phase 2: Scanning DALI2 capabilities (all addresses)...")
            
            for address in range(64):
                try:
                    await asyncio.sleep(0.2)
                    
                    # Query DALI2 capabilities (24-bit command)
                    dali2_data = [(address << 1) | 1, 0xFE, CMD_QUERY_DEVICE_CAPABILITIES]
                    result_d2 = await self._send_dali_frame(
                        line, dali2_data, num_bits=24, wait_for_answer=True
                    )
                    
                    capabilities = self._extract_response_value(result_d2)
                    if capabilities is not None and capabilities != 255:
                        # Create DALI2 device (separate from DALI address space)
                        device = DaliDevice(
                            address=line_address(line, address),
                            protocol="DALI2",
                            device_type=capabilities,
                            device_name=self._get_dali2_device_name(capabilities),
                            capabilities=capabilities,
                        )
                        self._devices[("DALI2", line_address(line, address))] = device
                        
                        _LOGGER.debug("Device %d has DALI2 capabilities: 0x%02x", address, capabilities)
                        
                        # Query DALI2 instances if device supports instance controller
                        has_instance_controller = (capabilities & 0x02) == 0x02
                        if scan_instances and has_instance_controller:
                            await asyncio.sleep(0.2)
                            num_instances = await self.query_num_instances(address, line)
                            if num_instances and num_instances > 0:
                                device.num_instances = num_instances
                                _LOGGER.debug("Device %d has %d instances", address, num_instances)
                                # Query each instance type (DALI2 instances are 0-indexed)
                                for inst in range(0, num_instances):
                                    await asyncio.sleep(0.1)
                                    inst_type = await self.query_instance_type(address, inst, line)
                                    if inst_type is not None:
                                        device.instances[inst] = {"type": inst_type}
                                        _LOGGER.debug(
                                            "Device %d instance %d: type %d",
                                            address, inst, inst_type
                                        )
                                    
                                    # Query instance features to detect feedback LED support
                                    await asyncio.sleep(0.1)
                                    features = await self.query_instance_features(address, inst, line)
                                    if features and inst in device.instances:
                                        device.instances[inst]["features"] = features
                                        device.instances[inst]["has_feedback_led"] = 32 in features
                                
                                # Read extended device information (GTIN, firmware, hardware, ID)
                                await asyncio.sleep(0.1)
                                device_info = await self.read_device_info(address, line)
                                if device_info:
                                    _LOGGER.debug(
                                        "Device %d info: GTIN=%s, FW=%s, HW=%s",
                                        address,
                                        device_info.get("gtin"),
                                        device_info.get("firmware_version"),
                                        device_info.get("hardware_version")
                                    )
                                        
                except Exception as e:
                    _LOGGER.debug("Error querying DALI2 for address %d: %s", address, e)

            dali2_count = sum(1 for d in self._devices.values() if d.protocol == "DALI2")
            _LOGGER.info("Phase 2 complete. Found %d DALI2 devices", dali2_count)

            # Phase 3: Query group membership for DALI devices only
            if scan_groups:
                _LOGGER.info("Phase 3: Scanning group membership...")
                for (protocol, address) in list(self._devices.keys()):
                    if protocol != "DALI" or split_address(address)[0] != line:
                        continue  # Groups only apply to DALI devices on this line
                    await asyncio.sleep(0.2)
                    try:
                        groups = await self.query_groups(address, line)
                        self._devices[(protocol, address)].groups = groups
                        if groups:
                            _LOGGER.debug("Device %d belongs to groups: %s", address, groups)
                    except Exception as e:
                        _LOGGER.debug("Error querying groups for address %d: %s", address, e)

            _LOGGER.info("Scan of line %d complete. Found %d devices total", line, len(self._devices))
            
            # Phase 4: Copy device info from DALI2 to DALI for dual-protocol devices
            # Some devices support both DALI and DALI2, creating separate entries (e.g., DALI_0 and DALI2_0)
            # DALI protocol doesn't support Memory Banks, but DALI2 does
            # Copy device info from DALI2 entry to DALI entry so light entities show metadata
            copied_count = 0
            for (protocol, address), device in list(self._devices.items()):
                if protocol == "DALI2" and device.gtin:
                    dali_key = ("DALI", address)
                    if dali_key in self._devices:
                        dali_device = self._devices[dali_key]
                        dali_device.gtin = device.gtin
                        dali_device.firmware_version = device.firmware_version
                        dali_device.hardware_version = device.hardware_version
                        dali_device.identification_number = device.identification_number
                        _LOGGER.debug(
                            "Copied device info from DALI2_%d to DALI_%d: GTIN=%s, FW=%s, HW=%s, Serial=%s",
                            address, address, device.gtin, device.firmware_version,
                            device.hardware_version, device.identification_number
                        )
                        copied_count += 1
            
            if copied_count > 0:
                _LOGGER.info("Copied device info for %d dual-protocol devices", copied_count)
            
            # Check if scan failed (found 0 devices when we had devices before)
            found_on_line = [k for k in self._devices if split_address(k[1])[0] == line]
            had_on_line = [k for k in devices_backup if split_address(k[1])[0] == line]
            if not found_on_line and had_on_line:
                _LOGGER.error("Scan failed: found 0 devices on line %d. Restoring previous device list.", line)
                self._devices = devices_backup
                raise ConnectionError("Scan failed - connection may have been lost during scan")
            
            return self._devices
            
        except ConnectionError:
            # Re-raise connection errors
            raise
        except Exception as e:
            # On any other error, restore devices and raise
            _LOGGER.error("Error during device scan: %s. Restoring previous device list.", e)
            self._devices = devices_backup
            raise

    async def scan_all_lines(self, scan_groups: bool = True, scan_instances: bool = True) -> dict[tuple[str, int], DaliDevice]:
        """Scan every configured DALI line, one after another."""
        for line in self.lines:
            await self.scan_devices(line, scan_groups=scan_groups, scan_instances=scan_instances)
        return self._devices

    async def update_device_states(self, line: int = 0) -> None:
        """Update the current state (brightness, on/off, color temp) for all DALI light devices."""
        _LOGGER.info("Updating device states...")
        
        # Only query DALI devices (lights), not DALI2 devices (sensors/inputs)
        dali_devices = [(protocol, addr) for protocol, addr in self._devices.keys() if protocol == "DALI"]
        
        for protocol, address in dali_devices:
            device = self._devices.get((protocol, address))
            if device is None:
                continue

            try:
                # Query brightness
                await asyncio.sleep(0.15)  # Small delay between queries
                brightness = await self.query_brightness(address, line)
                
                if brightness is not None:
                    device.brightness = brightness
                    device.is_on = brightness > 0
                    _LOGGER.debug("Device %d: brightness=%d%%, is_on=%s", address, brightness, device.is_on)
                else:
                    _LOGGER.debug("Device %d: no brightness response", address)
                
                # Note: Color temperature query is not reliable on these devices
                # Color temp will be tracked when explicitly set via set_color_temp()
                        
            except Exception as e:
                _LOGGER.warning("Error updating state for device %d: %s", address, e)
        
        _LOGGER.info("Device state update complete")

    def _get_dali2_device_name(self, capabilities: int) -> str:
        """Get DALI2 device name from capabilities byte."""
        is_app_controller = (capabilities & 0x01) == 0x01
        is_inst_controller = (capabilities & 0x02) == 0x02
        
        if is_app_controller and is_inst_controller:
            return "Application + Instance Controller"
        elif is_app_controller:
            return "Application Controller"
        elif is_inst_controller:
            return "Instance Controller"
        else:
            return f"DALI2 Device (0x{capabilities:02x})"

    async def query_brightness(self, address: int, line: int = 0) -> int | None:
        """Query current brightness level of a device."""
        line, address = split_address(address, line)
        try:
            # First, store actual level in DTR (command 33)
            dali_data = [(address << 1) | 1, CMD_STORE_ACTUAL_LEVEL_IN_DTR]
            await self._send_dali_frame(line, dali_data, send_twice=True)
            
            await asyncio.sleep(0.25)  # Wait for store command

            # Query DTR content (command 152)
            dali_data = [(address << 1) | 1, CMD_QUERY_CONTENT_DTR]
            result = await self._send_dali_frame(line, dali_data, wait_for_answer=True)

            level = self._extract_response_value(result)
            if level is not None and level <= 254:
                brightness = round((level / 254) * 100)
                
                # Update device state
                if address in self._devices:
                    self._devices[address].brightness = brightness
                    self._devices[address].is_on = level > 0
                
                return brightness

        except Exception as e:
            _LOGGER.error("Error querying brightness for address %d: %s", address, e)

        return None

    async def query_color_temp(self, address: int, line: int = 0) -> int | None:
        """Query current color temperature of a DT8 device (returns Kelvin).
        
        Note: Many DALI DT8 devices do not support querying the current color temperature.
        This method attempts to query but will return None if not supported.
        Color temperature should be tracked when explicitly set via set_color_temp().
        """
        line, address = split_address(address, line)
        try:
            # Enable Device Type 8
            dali_data = [193, 8]
            await self._send_dali_frame(line, dali_data)
            await asyncio.sleep(0.1)
            
            # Try to query color temperature (command 231 = 0xE7)
            dali_data = [(address << 1) | 1, 0xFE, 231]
            result = await self._send_dali_frame(line, dali_data, num_bits=24, wait_for_answer=True)
            
            mireds = self._extract_response_value(result)
            if mireds is not None and mireds > 0:
                # Convert mireds to Kelvin
                kelvin = round(1000000 / mireds)
                return kelvin
                
        except Exception as e:
            _LOGGER.debug("Error querying color temp for address %d: %s", address, e)
        
        return None

    async def set_brightness(
        self, address: int, brightness: int, line: int = 0
    ) -> bool:
        """Set brightness of a device (0-100%)."""
        line, address = split_address(address, line)
        try:
            # Convert percentage to DALI level (0-254)
            level = round((brightness / 100) * 254)
            level = max(0, min(254, level))  # Clamp to valid range

            # Send direct arc power command
            dali_data = [address << 1, level]
            await self._send_dali_frame(line, dali_data)

            # Update device state
            if address in self._devices:
                self._devices[address].brightness = brightness
                self._devices[address].is_on = brightness > 0

            _LOGGER.debug("Set brightness %d%% (level %d) for address %d", brightness, level, address)
            return True

        except Exception as e:
            _LOGGER.error("Error setting brightness for address %d: %s", address, e)
            return False

    async def turn_on(self, address: int, line: int = 0) -> bool:
        """Turn on a device (recall last level)."""
        line, address = split_address(address, line)
        try:
            # Send "go to last level" command (10)
            dali_data = [(address << 1) | 1, 10]
            await self._send_dali_frame(line, dali_data)

            if address in self._devices:
                self._devices[address].is_on = True

            _LOGGER.debug("Turned on device at address %d", address)
            return True

        except Exception as e:
            _LOGGER.error("Error turning on address %d: %s", address, e)
            return False

    async def turn_off(self, address: int, line: int = 0) -> bool:
        """Turn off a device."""
        line, address = split_address(address, line)
        try:
            # Send OFF command
            dali_data = [(address << 1) | 1, CMD_OFF]
            await self._send_dali_frame(line, dali_data)

            if address in self._devices:
                self._devices[address].is_on = False
                self._devices[address].brightness = 0

            _LOGGER.debug("Turned off device at address %d", address)
            return True

        except Exception as e:
            _LOGGER.error("Error turning off address %d: %s", address, e)
            return False

    async def set_color_temp(
        self, address: int, kelvin: int, line: int = 0
    ) -> bool:
        """Set color temperature for DT8 devices (in Kelvin)."""
        line, address = split_address(address, line)
        try:
            # Convert Kelvin to mireds
            mireds = round(1000000 / kelvin)
            msb = mireds // 256
            lsb = mireds % 256

            # DT8 color temperature sequence (6 commands, matching Node-RED exactly)
            # Example: 2700K -> mireds=370 -> MSB=1, LSB=114
            commands = [
                [195, msb],                                      # 1. Set DTR to MSB
                [163, lsb],                                      # 2. Set DTR1 to LSB
                [193, 8],                                        # 3. Enable Device Type 8
                [(address << 1) | 1, CMD_SET_TEMP_COLOR_TEMP],  # 4. Set temporary color temperature (231)
                [193, 8],                                        # 5. Enable Device Type 8 (again!)
                [(address << 1) | 1, CMD_ACTIVATE],             # 6. Activate (226)
            ]

            for dali_data in commands:
                await self._send_dali_frame(line, dali_data)
                await asyncio.sleep(0.05)  # Small delay between commands

            # Update device state
            key = ("DALI", address)
            if key in self._devices:
                self._devices[key].color_temp = kelvin

            _LOGGER.debug("Set color temperature %dK (mireds=%d) for address %d", kelvin, mireds, address)
            return True

        except Exception as e:
            _LOGGER.error("Error setting color temp for address %d: %s", address, e)
            return False

    async def recall_max_level(self, address: int, line: int = 0) -> bool:
        """Turn device on to maximum level (direct arc power)."""
        line, address = split_address(address, line)
        try:
            # Send direct arc power at level 254 (maximum)
            dali_data = [address << 1, 254]
            await self._send_dali_frame(line, dali_data)
            
            if address in self._devices:
                self._devices[address].is_on = True
                self._devices[address].brightness = 100
                
            _LOGGER.debug("Set to max level (254) for address %d", address)
            return True
        except Exception as e:
            _LOGGER.error("Error setting max level for address %d: %s", address, e)
            return False

    async def step_up(self, address: int, line: int = 0) -> bool:
        """Step brightness up."""
        line, address = split_address(address, line)
        try:
            dali_data = [(address << 1) | 1, CMD_STEP_UP]
            await self._send_dali_frame(line, dali_data)
            _LOGGER.debug("Step up address %d", address)
            return True
        except Exception as e:
            _LOGGER.error("Error stepping up address %d: %s", address, e)
            return False

    async def step_down(self, address: int, line: int = 0) -> bool:
        """Step brightness down."""
        line, address = split_address(address, line)
        try:
            dali_data = [(address << 1) | 1, CMD_STEP_DOWN]
            await self._send_dali_frame(line, dali_data)
            _LOGGER.debug("Step down address %d", address)
            return True
        except Exception as e:
            _LOGGER.error("Error stepping down address %d: %s", address, e)
            return False

    async def query_groups(self, address: int, line: int = 0) -> list[int]:
        """Query group membership for a device."""
        line, address = split_address(address, line)
        groups = []
        try:
            # Query groups 0-7
            dali_data = [(address << 1) | 1, CMD_QUERY_GROUPS_0_7]
            result = await self._send_dali_frame(line, dali_data, wait_for_answer=True)
            groups_0_7 = self._extract_response_value(result)
            if groups_0_7 is not None:
                for i in range(8):
                    if groups_0_7 & (1 << i):
                        groups.append(i)
            
            await asyncio.sleep(0.1)
            
            # Query groups 8-15
            dali_data = [(address << 1) | 1, CMD_QUERY_GROUPS_8_15]
            result = await self._send_dali_frame(line, dali_data, wait_for_answer=True)
            groups_8_15 = self._extract_response_value(result)
            if groups_8_15 is not None:
                for i in range(8):
                    if groups_8_15 & (1 << i):
                        groups.append(i + 8)
            
            if address in self._devices:
                self._devices[address].groups = groups
                
            _LOGGER.debug("Device %d belongs to groups: %s", address, groups)
            
        except Exception as e:
            _LOGGER.error("Error querying groups for address %d: %s", address, e)
        
        return groups

    async def query_dali2_capabilities(self, address: int, line: int = 0) -> int | None:
        """Query DALI2 capabilities (24-bit command)."""
        line, address = split_address(address, line)
        try:
            dali_data = [(address << 1) | 1, 0xFE, CMD_QUERY_DEVICE_CAPABILITIES]
            result = await self._send_dali_frame(
                line, dali_data, num_bits=24, wait_for_answer=True
            )
            
            capabilities = self._extract_response_value(result)
            if capabilities is not None:
                if address in self._devices:
                    self._devices[address].capabilities = capabilities
                _LOGGER.debug("Device %d has DALI2 capabilities: 0x%02x", address, capabilities)
                return capabilities
                
        except Exception as e:
            _LOGGER.error("Error querying DALI2 capabilities for address %d: %s", address, e)
        
        return None

    async def query_num_instances(self, address: int, line: int = 0) -> int:
        """Query number of DALI2 instances (24-bit command)."""
        line, address = split_address(address, line)
        try:
            dali_data = [(address << 1) | 1, 0xFE, CMD_QUERY_NUMBER_OF_INSTANCES]
            result = await self._send_dali_frame(
                line, dali_data, num_bits=24, wait_for_answer=True
            )
            
            num_instances = self._extract_response_value(result)
            if num_instances is not None:
                if address in self._devices:
                    self._devices[address].num_instances = num_instances
                _LOGGER.debug("Device %d has %d instances", address, num_instances)
                return num_instances
                
        except Exception as e:
            _LOGGER.error("Error querying number of instances for address %d: %s", address, e)
        
        return 0

    async def query_instance_type(
        self, address: int, instance: int, line: int = 0
    ) -> int | None:
        """Query DALI2 instance type (24-bit command)."""
        line, address = split_address(address, line)
        try:
            dali_data = [(address << 1) | 1, instance, CMD_QUERY_INSTANCE_TYPE]
            result = await self._send_dali_frame(
                line, dali_data, num_bits=24, wait_for_answer=True
            )
            
            instance_type = self._extract_response_value(result)
            if instance_type is not None:
                if address in self._devices:
                    if instance not in self._devices[address].instances:
                        self._devices[address].instances[instance] = {}
                    self._devices[address].instances[instance]["type"] = instance_type
                    
                from .const import INSTANCE_TYPES
                type_name = INSTANCE_TYPES.get(instance_type, f"Unknown ({instance_type})")
                _LOGGER.debug(
                    "Device %d instance %d type: %s", address, instance, type_name
                )
                return instance_type
                
        except Exception as e:
            _LOGGER.error(
                "Error querying instance type for address %d instance %d: %s",
                address, instance, e
            )
        
        return None

    async def query_instance_features(
        self, address: int, instance: int, line: int = 0
    ) -> list[int]:
        """Query instance feature types to detect capabilities (e.g., feedback LEDs).
        
        Returns list of feature type numbers. Feature Type 32 = Light output (feedback LED).
        Uses QUERY FEATURE TYPE (0x8E) and QUERY NEXT FEATURE TYPE (0x8F).
        """
        line, address = split_address(address, line)
        features = []
        
        try:
            # Query first feature type (resets feature index to 0)
            dali_data = [(address << 1) | 1, instance, CMD_QUERY_FEATURE_TYPE]
            result = await self._send_dali_frame(
                line, dali_data, num_bits=24, wait_for_answer=True
            )
            
            feature = self._extract_response_value(result)
            if feature is not None and feature != 0 and feature != 255:
                features.append(feature)
                
                from .const import FEATURE_TYPES
                feature_name = FEATURE_TYPES.get(feature, f"Unknown ({feature})")
                _LOGGER.debug(
                    "Device %d instance %d feature 0: %d (%s)",
                    address, instance, feature, feature_name
                )
                
                # Query additional features (up to 7 more)
                for idx in range(1, 8):
                    dali_data = [(address << 1) | 1, instance, CMD_QUERY_NEXT_FEATURE_TYPE]
                    result = await self._send_dali_frame(
                        line, dali_data, num_bits=24, wait_for_answer=True
                    )
                    
                    next_feature = self._extract_response_value(result)
                    if next_feature is not None and next_feature != 0 and next_feature != 255:
                        features.append(next_feature)
                        feature_name = FEATURE_TYPES.get(next_feature, f"Unknown ({next_feature})")
                        _LOGGER.debug(
                            "Device %d instance %d feature %d: %d (%s)",
                            address, instance, idx, next_feature, feature_name
                        )
                    else:
                        break
                    
                    await asyncio.sleep(0.05)
            
            # Store features in device instance data
            device_key = ("DALI2", address)  # Features only queried for DALI2 devices
            if features and device_key in self._devices:
                if instance not in self._devices[device_key].instances:
                    self._devices[device_key].instances[instance] = {}
                self._devices[device_key].instances[instance]["features"] = features
                self._devices[device_key].instances[instance]["has_feedback_led"] = 32 in features
                
                if 32 in features:
                    _LOGGER.info(
                        "Device %d instance %d supports feedback LED control", address, instance
                    )
                    
        except Exception as e:
            _LOGGER.debug(
                "Error querying instance features for address %d instance %d: %s",
                address, instance, e
            )
        
        return features

    async def read_device_info(
        self, address: int, line: int = 0
    ) -> dict[str, str | None]:
        """Read extended device information from Memory Bank 0.
        
        Returns GTIN, firmware version, hardware version, and identification number.
        """
        line, address = split_address(address, line)
        info = {
            "gtin": None,
            "firmware_version": None,
            "hardware_version": None,
            "identification_number": None,
        }
        
        try:
            # Set DTR1 = 0 (Memory Bank 0)
            dali_data = [0xC1, CMD_SET_DTR1, 0]
            await self._send_dali_frame(line, dali_data, num_bits=24, send_twice=False)
            await asyncio.sleep(0.05)
            
            # Set DTR0 = 0x03 (start address for GTIN)
            dali_data = [0xC1, CMD_SET_DTR0, 0x03]
            await self._send_dali_frame(line, dali_data, num_bits=24, send_twice=False)
            await asyncio.sleep(0.05)
            
            # Read 21 bytes (0x03-0x17): GTIN(6) + FW(2) + ID(8) + HW(2) + extras(3)
            memory_data = []
            for _ in range(21):
                dali_data = [(address << 1) | 1, 0xFE, CMD_READ_MEMORY_LOCATION]
                result = await self._send_dali_frame(
                    line, dali_data, num_bits=24, wait_for_answer=True
                )
                
                value = self._extract_response_value(result)
                if value is not None:
                    memory_data.append(value)
                else:
                    break
                    
                await asyncio.sleep(0.05)
            
            # Parse the data if we got enough bytes
            if len(memory_data) >= 18:
                # GTIN (6 bytes: 0x03-0x08) - store as hex
                gtin_bytes = memory_data[0:6]
                gtin_hex = ''.join(f'{b:02X}' for b in gtin_bytes)
                info["gtin"] = gtin_hex_to_decimal(gtin_hex)  # Convert to decimal
                
                # Firmware version (2 bytes: 0x09-0x0A)
                fw_bytes = memory_data[6:8]
                info["firmware_version"] = format_firmware_version([fw_bytes[0], fw_bytes[1], 0, 0])
                
                # Identification number (8 bytes: 0x0B-0x12) - store as hex
                id_bytes = memory_data[8:16]
                id_hex = ''.join(f'{b:02X}' for b in id_bytes)
                info["identification_number"] = format_serial_number(id_hex)  # Convert to decimal
                
                # Hardware version (2 bytes: 0x13-0x14)
                if len(memory_data) >= 18:
                    hw_bytes = memory_data[16:18]
                    info["hardware_version"] = format_hardware_version(hw_bytes)
                
                # Store in device object (keep hex format internally for conversion)
                device_key = ("DALI2", address)
                if device_key in self._devices:
                    device = self._devices[device_key]
                    device.gtin = gtin_hex  # Store hex internally
                    device.firmware_version = info["firmware_version"]
                    device.hardware_version = info["hardware_version"]
                    device.identification_number = id_hex  # Store hex internally
                    
                    _LOGGER.debug(
                        "DALI2 device %d info: GTIN=%s (%s), FW=%s, HW=%s, Manufacturer=%s",
                        address, info["gtin"], gtin_hex, info["firmware_version"], 
                        info["hardware_version"], device.manufacturer or "Unknown"
                    )
            else:
                _LOGGER.debug(
                    "Device %d: Could not read complete memory bank (got %d bytes)",
                    address, len(memory_data)
                )
                
        except Exception as e:
            _LOGGER.debug("Error reading device info for address %d: %s", address, e)
        
        return info

    async def read_dali1_device_info(
        self, address: int, line: int = 0
    ) -> dict[str, str | None]:
        """Read device information from DALI1 device using Memory Bank 0.
        
        Memory Bank 0 structure is the same for DALI1 and DALI2 (IEC 62386-102):
        0x00: Last accessible memory location
        0x01: Checksum (returns no response, but auto-increment continues)
        0x02: Lock byte
        0x03-0x08: GTIN (6 bytes)
        0x09-0x0C: Firmware version (4 bytes)
        0x0D-0x14: Serial number/Identification (8 bytes)
        
        Uses 16-bit DALI commands with special commands for DTR0/DTR1.
        
        Returns dict with hex values for internal storage (gtin_hex, id_hex)
        and formatted values for display (gtin, firmware_version, etc.)
        """
        line, address = split_address(address, line)
        info = {
            "gtin": None,  # Decimal format for display
            "gtin_hex": None,  # Hex format for internal storage
            "firmware_version": None,
            "hardware_version": None,
            "identification_number": None,  # Decimal format for display
            "id_hex": None,  # Hex format for internal storage
        }
        
        try:
            # Set DTR0 = 0x00 (starting memory address)
            # Special command: [0xA3, 0x00]
            dali_data = [0xA3, 0x00]
            await self._send_dali_frame(line, dali_data, num_bits=16, send_twice=True, wait_for_answer=False)
            await asyncio.sleep(0.1)
            
            # Set DTR1 = 0x00 (Memory Bank 0)
            # Special command: [0xC3, 0x00]
            dali_data = [0xC3, 0x00]
            await self._send_dali_frame(line, dali_data, num_bits=16, send_twice=True, wait_for_answer=False)
            await asyncio.sleep(0.1)
            
            # Read Memory Bank bytes with auto-increment
            # Command 197 (0xC5) = READ MEMORY LOCATION
            memory_data = []
            
            # Read 21 bytes: last_addr(1) + checksum(1) + lock(1) + GTIN(6) + FW(4) + Serial(8)
            for i in range(21):
                dali_data = [(address << 1) | 1, 0xC5]
                result = await self._send_dali_frame(
                    line, dali_data, num_bits=16, wait_for_answer=True
                )
                
                # Extract response - field name can be "answer" or "daliData"
                value = None
                if result and result.get("result") in [1, 8]:
                    value = result.get("answer") or result.get("daliData")
                
                if value is not None:
                    memory_data.append(value)
                elif i == 1:
                    # Address 0x01 (checksum) returns nothing by design
                    # Use placeholder and continue - auto-increment still works
                    memory_data.append(0xFF)
                else:
                    # Other missing bytes might indicate end of data or error
                    memory_data.append(0xFF)
                    
                await asyncio.sleep(0.15)
            
            # Check if we got valid data (at least first byte - last accessible location)
            if len(memory_data) >= 21 and memory_data[0] != 0xFF:
                last_accessible = memory_data[0]
                
                # Parse according to IEC 62386-102 structure
                # Bytes 0x03-0x08: GTIN (6 bytes) → memory_data[3:9]
                gtin_bytes = memory_data[3:9]
                gtin_hex = ''.join(f'{b:02X}' for b in gtin_bytes)
                if any(b != 0xFF for b in gtin_bytes):
                    info["gtin_hex"] = gtin_hex
                    info["gtin"] = gtin_hex_to_decimal(gtin_hex)  # Convert to decimal
                
                # Bytes 0x09-0x0C: Firmware version (4 bytes) → memory_data[9:13]
                fw_bytes = memory_data[9:13]
                if any(b != 0xFF and b != 0 for b in fw_bytes):
                    info["firmware_version"] = format_firmware_version(fw_bytes)
                
                # Bytes 0x0D-0x14: Identification number (8 bytes) → memory_data[13:21]
                id_bytes = memory_data[13:21]
                id_hex = ''.join(f'{b:02X}' for b in id_bytes)
                if any(b != 0xFF and b != 0 for b in id_bytes):
                    info["id_hex"] = id_hex
                    info["identification_number"] = format_serial_number(id_hex)  # Convert to decimal
                
                # Get manufacturer from GTIN
                manufacturer = get_manufacturer_from_gtin(info["gtin"]) if info["gtin"] else None
                
                _LOGGER.debug(
                    "DALI1 device %d info: GTIN=%s (%s), FW=%s, ID=%s, Manufacturer=%s (last_accessible=0x%02X)",
                    address, info["gtin"], gtin_hex, info["firmware_version"], 
                    info["identification_number"], manufacturer or "Unknown", last_accessible
                )
            else:
                _LOGGER.debug(
                    "DALI1 device %d: No response to Memory Bank 0 read (not supported or offline)",
                    address
                )
                
        except Exception as e:
            _LOGGER.debug("Error reading DALI1 device info for address %d: %s", address, e)
        
        return info

    async def set_feedback_led(
        self, address: int, instance: int, state: bool, line: int = 0
    ) -> bool:
        """Set feedback LED state for a DALI2 instance.
        
        Args:
            address: DALI2 device address
            instance: Instance number (0-7 for 8-button controllers)
            state: True for ON, False for OFF
            line: DALI line number (default 0)
            
        Returns:
            True if command was sent successfully
        """
        line, address = split_address(address, line)
        try:
            # 24-bit frame: [address<<1+1, 0x20+instance, 0x10 (ON) or 0x11 (OFF)]
            from .const import FEEDBACK_LED_ON, FEEDBACK_LED_OFF
            led_command = FEEDBACK_LED_ON if state else FEEDBACK_LED_OFF
            
            dali_data = [(address << 1) + 1, 0x20 + instance, led_command]
            await self._send_dali_frame(
                line, dali_data, num_bits=24, send_twice=False, wait_for_answer=False
            )
            
            _LOGGER.debug(
                "Set feedback LED for device %d instance %d to %s",
                address, instance, "ON" if state else "OFF"
            )
            return True
            
        except Exception as e:
            _LOGGER.error(
                "Error setting feedback LED for address %d instance %d: %s",
                address, instance, e
            )
            return False

    async def set_group_brightness(
        self, group_number: int, level: int, line: int = 0
    ) -> bool:
        """Set brightness for a DALI group (0-254)."""
        if not 0 <= group_number <= 15:
            _LOGGER.error("Invalid group number %d (must be 0-15)", group_number)
            return False
        
        try:
            # Group command: address byte = 0x80 | (group << 1)
            address_byte = 0x80 | (group_number << 1)
            dali_data = [address_byte, level]
            await self._send_dali_frame(line, dali_data)
            # Our own frames are not tracked from the bus monitor, so update members here
            self._apply_gear_state(
                self._resolve_command_targets(address_byte, line) or [], level > 0, round((level / 254) * 100)
            )
            
            _LOGGER.debug("Set group %d brightness to level %d", group_number, level)
            return True
        except Exception as e:
            _LOGGER.error("Error setting brightness for group %d: %s", group_number, e)
            return False

    async def set_group_color_temp(
        self, group_number: int, kelvin: int, line: int = 0
    ) -> bool:
        """Set color temperature for a DALI group (in Kelvin)."""
        if not 0 <= group_number <= 15:
            _LOGGER.error("Invalid group number %d (must be 0-15)", group_number)
            return False
        
        try:
            # Convert Kelvin to mireds
            mireds = round(1000000 / kelvin)
            msb = mireds // 256
            lsb = mireds % 256

            # Group address byte
            group_address = 0x80 | (group_number << 1)

            # DT8 color temperature sequence for groups
            commands = [
                [195, msb],                          # 1. Set DTR to MSB
                [163, lsb],                          # 2. Set DTR1 to LSB
                [193, 8],                            # 3. Enable Device Type 8
                [group_address | 1, CMD_SET_TEMP_COLOR_TEMP],  # 4. Set temporary color temperature
                [193, 8],                            # 5. Enable Device Type 8 (again)
                [group_address | 1, CMD_ACTIVATE],   # 6. Activate
            ]

            for dali_data in commands:
                await self._send_dali_frame(line, dali_data)
                await asyncio.sleep(0.05)

            _LOGGER.debug("Set group %d color temperature to %dK (mireds=%d)", group_number, kelvin, mireds)
            return True

        except Exception as e:
            _LOGGER.error("Error setting color temp for group %d: %s", group_number, e)
            return False

    async def set_broadcast_brightness(self, level: int, line: int = 0) -> bool:
        """Set brightness for all devices via broadcast (0-254)."""
        try:
            # Broadcast command: address byte = 0xFE (broadcast unaddressed)
            dali_data = [0xFE, level]
            await self._send_dali_frame(line, dali_data)
            # Our own frames are not tracked from the bus monitor, so update all gear here
            self._apply_gear_state(
                self._resolve_command_targets(0xFE, line) or [], level > 0, round((level / 254) * 100)
            )
            
            _LOGGER.debug("Set broadcast brightness to level %d", level)
            return True
        except Exception as e:
            _LOGGER.error("Error setting broadcast brightness: %s", e)
            return False

    async def set_broadcast_color_temp(self, kelvin: int, line: int = 0) -> bool:
        """Set color temperature for all devices via broadcast (in Kelvin)."""
        try:
            # Convert Kelvin to mireds
            mireds = round(1000000 / kelvin)
            msb = mireds // 256
            lsb = mireds % 256

            # DT8 color temperature sequence for broadcast
            commands = [
                [195, msb],                          # 1. Set DTR to MSB
                [163, lsb],                          # 2. Set DTR1 to LSB
                [193, 8],                            # 3. Enable Device Type 8
                [0xFF, CMD_SET_TEMP_COLOR_TEMP],     # 4. Broadcast command
                [193, 8],                            # 5. Enable Device Type 8 (again)
                [0xFF, CMD_ACTIVATE],                # 6. Broadcast activate
            ]

            for dali_data in commands:
                await self._send_dali_frame(line, dali_data)
                await asyncio.sleep(0.05)

            _LOGGER.debug("Set broadcast color temperature to %dK (mireds=%d)", kelvin, mireds)
            return True

        except Exception as e:
            _LOGGER.error("Error setting broadcast color temp: %s", e)
            return False

