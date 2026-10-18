# Testing on Production HassOS Device

This guide is for testing the integration on your HassOS production Home Assistant before making it public.

## Prerequisites

- Home Assistant OS (HassOS) device accessible via SSH or terminal
- Lunatone DALI-2 IoT Gateway on the same network
- SSH access enabled (Settings → Add-ons → Terminal & SSH)

## Installation Methods

### Method 1: Using File Editor Add-on (Easiest for HassOS)

1. **Install File Editor Add-on** (if not already installed):
   - Settings → Add-ons → Add-on Store
   - Search for "File editor"
   - Install and Start

2. **Create directory structure:**
   - Open File Editor
   - Navigate to `/config/`
   - Create folder: `custom_components`
   - Inside that, create folder: `dali_lunatone`

3. **Upload files:**
   - Upload all files from `custom_components/dali_lunatone/` to `/config/custom_components/dali_lunatone/`
   - Required files:
     - `__init__.py`
     - `manifest.json`
     - `strings.json`
     - `config_flow.py`
     - `const.py`
     - `coordinator.py`
     - `light.py`
     - `binary_sensor.py`
     - `sensor.py`
     - `lunatone_api.py`
     - `storage.py`
     - `repairs.py`
     - `services.yaml`

4. **Restart Home Assistant:**
   - Settings → System → Restart

### Method 2: Using Terminal/SSH

1. **Connect to Home Assistant via SSH:**
   ```bash
   ssh root@homeassistant.local
   # or
   ssh root@<IP_ADDRESS>
   ```

2. **Create and upload files:**
   ```bash
   # From your development machine, create a zip
   cd ~/development/dali-lunatone-integration
   cd custom_components
   zip -r dali_lunatone.zip dali_lunatone/
   
   # Copy to HassOS (from development machine)
   scp dali_lunatone.zip root@homeassistant.local:/config/
   
   # On HassOS, extract
   cd /config
   mkdir -p custom_components
   unzip dali_lunatone.zip -d custom_components/
   rm dali_lunatone.zip
   ```

3. **Restart Home Assistant:**
   ```bash
   ha core restart
   ```

### Method 3: Using Samba/SMB Share

1. **Enable Samba add-on** (if not already):
   - Settings → Add-ons → Samba share
   - Start the add-on

2. **Access via file browser:**
   - Windows: `\\homeassistant.local\config`
   - macOS: `smb://homeassistant.local/config`
   - Linux: `smb://homeassistant.local/config`

3. **Copy files:**
   - Create folder: `custom_components\dali_lunatone`
   - Copy all integration files into this folder

4. **Restart via UI:**
   - Settings → System → Restart

## Configuration

1. **Add Integration:**
   - Settings → Devices & Services
   - Click "+ Add Integration"
   - Search for "Lunatone DALI-2 IoT"
   - Enter your gateway details:
     - **Host**: Gateway IP address (e.g., 192.168.1.100)
     - **Port**: 80
     - **Update Interval**: 900 (default)
     - **Scan on Setup**: Yes (recommended)

2. **Verify Installation:**
   - Check that devices appear in Settings → Devices & Services → Lunatone DALI-2 IoT
   - Test turning lights on/off
   - Test brightness control
   - Check binary sensors (buttons, occupancy)
   - Check light sensors

## Testing Checklist

### Basic Functionality
- [ ] Integration loads without errors
- [ ] All expected DALI devices discovered
- [ ] Light entities respond to commands
- [ ] Brightness control works (0-100%)
- [ ] Color temperature works (DT8 devices)

### DALI2 Features
- [ ] Push buttons create binary sensor entities
- [ ] Button press events trigger correctly
- [ ] Button state returns to "off" after short press
- [ ] Occupancy sensors work
- [ ] Light sensors show lux values

### Advanced Features
- [ ] External wall switch commands update HA
- [ ] Group commands update multiple devices
- [ ] Services work (rescan_devices, step_up, step_down, recall_max)
- [ ] Integration reconnects after gateway restart
- [ ] Device states persist across HA restart

### Performance
- [ ] WebSocket connection stable
- [ ] No excessive CPU usage
- [ ] No memory leaks over 24 hours
- [ ] Bus monitoring doesn't cause lag

## Debug Logging

If you encounter issues, enable debug logging:

1. **Edit configuration.yaml:**
   ```yaml
   logger:
     default: info
     logs:
       custom_components.dali_lunatone: debug
   ```

2. **Restart Home Assistant**

3. **Check logs:**
   - Settings → System → Logs
   - Look for `dali_lunatone` entries

## Common Issues

### Integration Not Found
- Verify files are in `/config/custom_components/dali_lunatone/`
- Check `manifest.json` is valid JSON
- Restart Home Assistant again

### Cannot Connect to Gateway
- Verify gateway IP address
- Check network connectivity: `ping <gateway_ip>`
- Ensure port 80 is accessible
- Check firewall settings

### Devices Not Discovered
- Enable "Scan on Setup" during configuration
- Run `dali_lunatone.rescan_devices` service
- Check DALI bus is powered and devices are addressed
- Enable debug logging to see scan results

### Button Stays "On"
- This should be fixed in v0.1.0
- If still occurs, check logs and report issue

## Removing for Testing

If you need to remove and reinstall:

1. **Remove integration:**
   - Settings → Devices & Services
   - Find Lunatone DALI-2 IoT
   - Click three dots → Delete

2. **Remove files:**
   ```bash
   rm -rf /config/custom_components/dali_lunatone
   # or via File Editor / Samba
   ```

3. **Restart Home Assistant**

## Reporting Issues

If you find bugs during testing:

1. **Gather information:**
   - Integration version: 0.1.0-beta
   - Home Assistant version
   - Gateway firmware version
   - Debug logs showing the issue

2. **Document:**
   - Steps to reproduce
   - Expected behavior
   - Actual behavior
   - Screenshots if relevant

3. **Report:**
   - Create issue on GitHub (after making repository public)
   - Or note for local tracking before release

## Success Criteria

Before making the integration public, verify:

- ✅ All basic features work reliably
- ✅ No critical bugs
- ✅ Performance is acceptable
- ✅ Documentation is accurate
- ✅ All tested device types work
- ✅ No data loss or corruption
- ✅ Safe to use in production environment

## Next Steps After Testing

Once testing is complete and successful:

1. Note any issues found and fix them
2. Update CHANGELOG.md with any additional fixes
3. Create GitHub repository
4. Push code to GitHub
5. Create v0.1.0-beta release
6. Share with community for wider testing
7. Plan for v1.0.0 stable release after beta period

## Unit tests

The DALI client (`lunatone_api.py`) has unit tests that run without Home Assistant
or a gateway. They need Python 3.12+ (the same as Home Assistant).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements_test.txt
venv/bin/python -m pytest tests
```

- `tests/test_external_commands.py` - following commands from wall buttons and other bus masters
- `tests/test_multi_line.py` - multi-line (multi-bus) support

