# Aqara W100 (lumi.sensor_ht.agl001) ZHA Quirk

This custom quirk enables functionality for the Aqara W100 Climate Sensor in Home Assistant using ZHA (Zigbee Home Automation). It is mainly used for debugging and testing. By enabling Thermostat Mode Switch it is possible to control the middle display.

## Features

- **Thermostat Control:** Climate entity with Heat, Cool, Auto (shown in Home Assistant as Heat/Cool) and Off modes, and a target temperature. Requires the Thermostat Mode Switch to be on.
- **Fan Control:** Dedicated Fan entity (Endpoint 21) supporting Low, Medium, High, and Auto speeds.
- **Sensors:** Temperature, Humidity, and Battery reporting.
- **External sensor display:** Show readings from another source on the device (for example outdoor weather):
  - **Sensor display mode:** `internal` shows the W100's own readings; `external` shows the values below.
  - **External temperature / External humidity:** Number entities to write the values to display, e.g. from an automation.
- **Button Actions:** Exposes the 3 buttons (Plus, Center, Minus) as device automation triggers (Single, Double, Hold, Release). **NOTE**, if the Thermostat Mode Switch is enabled, then it is not possible for button action as the button control the display.
- **Configuration:**
  - **Thermostat Mode Switch:** Turns the device's thermostat mode on or off. On shows the thermostat (the **T** symbol) and the buttons control it; off restores the buttons as automation triggers.
  - **Auto Hide Middle Line:** Control the display behavior.

## Installation

1. **Prepare the Directory:**
   - Access your Home Assistant configuration directory (where `configuration.yaml` is located).
   - Create a folder named `custom_zha_quirks` (if it doesn't exist).

2. **Copy the Quirk:**
   - Download the `sensor_ht_agl001.py` file.
   - Place it inside the `custom_zha_quirks` folder.

3. **Update Configuration:**
   - Edit your `configuration.yaml` file to include the custom quirks path:
     ```yaml
     zha:
       enable_quirks: true
       custom_quirks_path: /config/custom_zha_quirks/
     ```

4. **Restart Home Assistant:**
   - Restart Home Assistant to load the new configuration.

5. **Pair/Re-pair Device:**
   - If the device is already paired, you may need to remove it and pair it again for the quirk to apply correctly and create all entities (especially the new Fan entity).

## Usage

### Thermostat Mode Switch
Turn this on to use the device as a thermostat. While it's on, the buttons change the mode and target temperature on the device, and those changes are sent to Home Assistant. While it's off, the quirk keeps the device in buttons-only mode: the device drifts back into thermostat mode on its own, and the quirk switches it back off the next time the device checks in.

### Thermostat
The climate entity controls the device's system mode and target temperature (the Thermostat Mode Switch must be on).
- **Heat / Cool / Heat/Cool (Auto):** Sets the device's mode.
- **Off:** Turns the device's HVAC state off. It doesn't leave thermostat mode; use the Thermostat Mode Switch for that.
- **Target temperature:** Whole degrees only; see [Known Issues](#known-issues).

### Fan Control
The quirk creates a separate Fan entity.
- **Low / Medium / High:** Maps directly to the device's fan speeds.
- **Auto:** The device's automatic fan mode (internally handled as Speed 0).
- **Off, On and Smart:** The device has no equivalent, so these are sent as Auto and the entity shows the Auto preset.

### External Sensor Display
Set **Sensor display mode** to `external`, then write values to **External temperature** and **External humidity**, for example from an automation that copies a weather entity. The last values written are kept across restarts and sent to the device when you switch to external mode.

### Buttons
You can use the buttons in Home Assistant Automations.
- Triggers: `Plus Button Short Press`, `Center Button Double Press`, etc.

## Known Issues

- **Whole degrees only.** The Zigbee protocol the W100 uses for thermostat mode (PMTSD) only carries whole degrees, even though the device's own display lets you set half degrees. A setpoint from Home Assistant is rounded to the nearest whole degree (25.5 becomes 26). A half degree set on the device (23.5) is reported as 23 and becomes 23 once the quirk confirms the change. Half degrees may work when the device runs Matter over Thread, but that's unverified.
- **Thermostat Mode Switch may turn itself off after a ZHA reload.** An "on" switch state has been seen to come back as off after ZHA reloaded (for example after enabling entities), because the stored value was missing when the device was reconfigured. Turn the switch back on if this happens. Being investigated.
- **The T may briefly reappear after a Home Assistant restart.** With the switch off, the device can drift back into thermostat mode while Home Assistant is down. The quirk switches it back off when the device next checks in, usually within a minute or two.
- **Some changes on the device aren't reflected in Home Assistant.** Some local changes (seen when cycling modes) arrive with an empty payload the quirk can't decode. Home Assistant catches up when the device next reports its state.
- **The fan can't be turned off.** The device has no fan "off", only Auto and three speeds. ZHA's fan entity always offers Off (and the On and Smart presets); the quirk sends these as Auto, and the entity switches to the Auto preset to match the display (AUTO with three bars).
- **E02 on the display.** E02 means the device sent a change and is waiting for the hub to confirm it. The quirk acknowledges and confirms every change, so it's expected to appear only while Home Assistant or ZHA is unavailable, clearing once they're back. (Confirmation is new; if you still see E02 in normal use, please report it with debug logs.)

## Troubleshooting

- **Entities missing?** Try removing the device from ZHA and pairing it again.
- **Logs:** Enable debug logging for the quirk to see what it sends and receives:
  ```yaml
  logger:
    logs:
      sensor_ht_agl001: debug
  ```

## Development

The quirk has an automated test suite that runs it against the real zigpy/ZHA
stack with the Zigbee radio mocked out. Tests live in `tests/` and coverage of
`sensor_ht_agl001.py` is enforced at 100% (lines and branches).

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

`requirements_test.txt` pins the ZHA, zigpy and zha-quirks versions the quirk is
tested against; bump them to match the Home Assistant release you're targeting.
