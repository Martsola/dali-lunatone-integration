# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Group and broadcast lights had no state** - `DaliGroupLight` and `DaliBroadcastLight` never reported `is_on`/`brightness`, so Home Assistant showed them as unknown. They now derive state from their member lights (on if any member is on, brightness is the mean of the members that are on, as Home Assistant light groups do), and group/broadcast commands sent from Home Assistant update the member lights' tracked state.

## [0.1.4-beta-3] - 2026-03-13

### Fixed
- **DALI iterator disruption mid-enumeration** - Previous retry logic only triggered when `detected_types` was completely empty. However the bus can be disturbed after the first type (e.g. `6`) is returned but before the rest (`8, 50, 51, 52, 254`) arrive — causing the code to accept a truncated `[DT6]` result as complete. Fixed by tracking the `254` end-of-list sentinel: enumeration is only considered complete when `254` is received. Any `None`/no-response exit now triggers a retry, regardless of how many types were already collected.

## [0.1.4-beta-2] - 2026-03-13

### Fixed
- **DALI device type iterator disrupted by shared bus** - When two Home Assistant instances share the same Lunatone gateway, DALI commands from one instance land between `QUERY DEVICE TYPE` and `QUERY NEXT DEVICE TYPE` on the other, invalidating the device type iterator. Added a retry loop (up to 3 attempts) that re-sends `QUERY DEVICE TYPE` (resetting the iterator in device firmware) and waits 500 ms for competing bus traffic to clear before each retry. This fixes intermittent DT6-only detection for addresses 25 and 27 on a shared bus.
- **Added GTIN prefix `769894`** → `Shredded Foam Of Hawaii, Inc.`

## [0.1.4-beta] - 2026-03-13

### Fixed
- **Critical: DALI device type iterator invalidation** - `QUERY NEXT DEVICE TYPE` (cmd 167) must immediately follow `QUERY DEVICE TYPE` with no intervening DALI commands. The previous code sent a `QUERY STATUS` command between the two, which reset the device type iterator per IEC 62386-102. This caused multi-type devices (e.g. DT6+DT8 CCT luminaires) at addresses 25-27 to be detected as DT6 only, losing CCT colour control.
- **Corrected `CMD_QUERY_NEXT_DEVICE_TYPE` constant** - Was `154` (0x9A), corrected to `167` (0xA7) based on Lunatone DALI Cockpit TCP capture analysis. The wrong command caused devices 40-49 to previously appear as DT85.
- **Reduced iterator loop sleep** from 100 ms to 20 ms to stay within the ~80 ms iterator timeout observed on some device firmware.
- **Dynamic light entity creation** - Light entities are now created dynamically when a manual scan detects new DT6/DT7/DT8 devices that were previously unknown (e.g. devices stored as DT1 at startup that become DT8 after a scan).
- **CCT color mode refresh** - `LunatoneDaliLight` now re-evaluates `supported_color_modes` on every coordinator update, so an entity created as brightness-only (DT6) correctly gains CCT controls after being rescanned as DT8 without requiring a restart.
- **`KeyError` in background state update** - `update_device_states` used an unguarded `self._devices[(protocol, address)]` lookup that could raise `KeyError` if device list changed mid-iteration; replaced with safe `.get()`.
- **`KeyError` in `LunatoneDaliLight.__init__`** - Constructor no longer does an unguarded `coordinator.data[key]` lookup; initial color mode is set via the safe `_update_color_mode()` helper.
- **Full traceback in background update errors** - Changed `_LOGGER.error("...: %s", e)` to `_LOGGER.exception(...)` so stack traces are visible in the log.

### Changed
- Multi-type device scan: removed `QUERY STATUS` presence-check (Step A) that invalidated the iterator; the MASK (0xFF) response itself is sufficient evidence of device presence; if the subsequent `QUERY NEXT DEVICE TYPE` loop returns nothing, the device is silently skipped rather than defaulting to DT6.
- `FeedbackLedLight` constructor uses `coordinator.data.get(key)` instead of direct `[]` access to prevent `KeyError` when the device is temporarily absent from coordinator data.

## [0.1.3-beta] - 2026-02-11

### Fixed
- **Critical: DALI-2 push button event decoding** - Event info byte was incorrectly parsed by masking lower 3 bits (`& 0x07`), but per IEC 62386-301 the full byte value uniquely identifies the event type
- `long_press_stop` (value 12) was incorrectly decoded as `long_press_start` — making it impossible to detect button release after long press
- `double_press` (value 5), `long_press_start` (value 9), `long_press_repeat` (value 11), and `button_free` (value 14) were all mapped to wrong event names
- Removed incorrect `event_counter` field based on wrong assumption of bit splitting
- Button state now correctly reflects only active press states (pressed, long_press_start, long_press_repeat)

### Changed
- `BUTTON_EVENT_TYPES` dictionary updated to use correct IEC 62386-301 event info values (0, 1, 2, 5, 9, 11, 12, 14, 15 instead of 0-7)
- iT1 (Push Button) and iT2 (Absolute Input Device/Switch) event parsing both corrected

## [0.1.1-beta] - 2025-11-13

### Added
- **DALI1 Memory Bank 0 Support**: Extended device information reading now works for both DALI1 and DALI2 devices
  - Implemented correct 16-bit command sequence for DALI1 Memory Bank 0 access
  - DTR0 (0xA3) for memory address, DTR1 (0xC3) for bank selection, READ (0xC5) with auto-increment
- **Feedback LED Detection**: Automatic detection of feedback indicator LED support on DALI2 devices using QUERY FEATURE TYPE command (IEC 62386-303)
- **Extended Device Information**: Read and display device details from Memory Bank 0:
  - GTIN (Global Trade Item Number) - displayed in **decimal format**
  - Firmware version with smart formatting (e.g., "3.12" instead of "3.12.0.0")
  - Hardware version
  - Serial number - correctly decoded from middle 4 bytes (big-endian 32-bit) of 64-bit ID
- **Manufacturer Detection**: Automatic manufacturer identification from GTIN prefix:
  - Osram (4062172, 4052899)
  - Sunricher (6971542)
  - Signify (8718696)
  - LTECH (6971379110529)
  - Lunatone (9010342)
  - Tridonic (9006210)
- **Feedback LED Control**: New service `set_feedback_led` to control indicator LEDs on DALI2 button/sensor instances
- **Device Naming Format**: Devices now named as "[Protocol] Address [N]" (e.g., "DALI Address 0", "DALI2 Address 1")
- **Enhanced Entity Attributes**: Binary sensors now expose:
  - `has_feedback_led`: Indicates if instance supports feedback LED
  - `led_controllable`: Indicates if LED can be controlled
  - `features`: List of detected DALI2 feature types for the instance
- **Device Info Display**: Extended device information now visible in Home Assistant device pages:
  - Manufacturer name (from GTIN)
  - Model includes GTIN in decimal format when available
  - Software version shows firmware version
  - Hardware version displayed in device details
  - Serial number in decimal format

### Changed
- Device discovery process now queries instance features and extended device information for both DALI1 and DALI2
- Binary sensor, light, and sensor entities display extended device information in device registry
- GTIN values hidden when all zeros (000000000000)
- Serial numbers extracted from correct byte positions in 64-bit identification number
- Device naming simplified to protocol and address format

### Fixed
- DALI1 Memory Bank 0 reading now works correctly (handles checksum byte behavior)
- Serial number decoding fixed - now extracts middle 4 bytes as big-endian 32-bit integer
- GTIN displayed in decimal format instead of hex
- Empty device info values (all zeros) no longer shown
- Group range validation: Groups correctly limited to 0-15 per DALI IEC 62386 specification
- Group extraction mask fixed from 0x3F (6 bits) to 0x0F (4 bits)
- Group control methods now validate group number range (0-15)
- Feedback LED entities now show toggle switch interface from the start (not lightning bolt after first use)

### Technical
- Implemented QUERY FEATURE TYPE (0x8E) and QUERY NEXT FEATURE TYPE (0x8F) commands
- Implemented READ MEMORY LOCATION (0x3C) for Memory Bank 0 access (DALI2)
- Implemented DALI1 Memory Bank 0 reading with DTR0/DTR1/READ command sequence
- Added Feature Type 32 detection for "Light output" (feedback LEDs)
- New API methods: `query_instance_features()`, `read_device_info()`, `read_dali1_device_info()`, `set_feedback_led()`
- Helper functions: `gtin_hex_to_decimal()`, `get_manufacturer_from_gtin()`, `format_serial_number()`, `format_firmware_version()`, `format_hardware_version()`
- Device properties: `gtin_decimal`, `manufacturer`, `serial_number`

## [0.1.0-beta] - 2025-11-12

### Added
- Initial beta release for Lunatone DALI-2 IoT Gateway
- Support for DALI devices (DT6 LED, DT7 Switching, DT8 Color Temperature)
- Support for DALI2 instance types (iT0-iT6):
  - iT1: Push Buttons with 8 event types
  - iT2: Absolute Input Devices/Switches
  - iT3: Occupancy Sensors
  - iT4: Light Sensors (illuminance in lux)
  - iT5: Colour Sensors
  - iT6: General Purpose Sensors
- Brightness control (0-100%)
- Color temperature control (2000K-6500K) for DT8 devices
- DALI group control entities (groups 0-15)
- Broadcast control entity (all devices)
- Configuration flow with device discovery
- Device state synchronization with configurable scan interval (60-3600 seconds)
- Persistent device storage
- Device configuration management via UI
- Platform services: step_up, step_down, recall_max
- Integration services: rescan_devices
- Binary sensors for buttons, switches, and occupancy
- Light level sensors with lux measurement
- Real-time DALI bus monitoring
- Automatic reconnection on connection loss
- WebSocket communication with Lunatone DALI-2 IoT Gateway
- Async/await architecture with DataUpdateCoordinator
- Comprehensive documentation (README, INSTALL, QUICK_REFERENCE)
- GitHub issue templates for bug reports and feature requests
- GitHub workflows for validation and HACS compatibility
- CONTRIBUTING.md with development guidelines
- Comprehensive FAQ section in README
- HACS integration support with hacs.json
- .editorconfig for consistent code formatting

### Fixed
- Push button (iT1) state now correctly returns to "off" after short press events
- Improved binary sensor state handling for momentary vs maintained buttons

[Unreleased]: https://github.com/martsola/dali-lunatone-integration/compare/v0.1.3-beta...HEAD
[0.1.3-beta]: https://github.com/martsola/dali-lunatone-integration/compare/v0.1.2-beta...v0.1.3-beta
[0.1.0-beta]: https://github.com/martsola/dali-lunatone-integration/releases/tag/v0.1.0-beta
