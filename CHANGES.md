# Changes from upstream

This fork tracks [Pulzki/w100-zha-quirks](https://github.com/Pulzki/w100-zha-quirks).
It is based on upstream `c335919` (merge of PR #6, external temperature/humidity
sensor support). Every change below is in `custom_zha_quirks/sensor_ht_agl001.py`.

## Fixes

### Thermostat mode frames too large to send

Upstream sends W100 control frames to attribute `0xFFF2` with the high-level
`write_attributes`. A size guard in that method rejects any single attribute
record larger than the link's per-request limit (about 50 bytes). The frame that
turns thermostat mode on is about 60 bytes, so it was never sent.

All control frames now go through one helper, `_write_w100_control_frame`. It
builds the attribute record by hand (ZCL octet string, type `0x41`) and calls
`write_attributes_raw` with manufacturer code `0x115F`. The PMTSD frame and both
thermostat mode frames use it.

`write_attributes_raw` does no status checking of its own, so the helper now
checks the response itself. If the device rejects a frame, the helper raises
instead of treating the write as a success.

### Thermostat Mode Switch not staying off

When the Thermostat Mode Switch was off, the device kept going back into
thermostat mode, with the **T** symbol showing on the display. Testing on the
device showed that:

- the device only sends PMTSD heartbeat requests (command `0x84`) while it is in
  thermostat mode, and
- after a while it goes back into thermostat mode on its own.

Upstream replied to every heartbeat with the cached PMTSD frame. Any PMTSD
reply, even one with `P=1`, keeps the device in thermostat mode.

The fix:

- A new method, `_reply_to_heartbeat`, answers heartbeats based on the switch:
  - **Switch off:** sends the "thermostat off" frame again, which puts the
    device back into buttons-only mode.
  - **Switch on:** sends the current HVAC state as a PMTSD frame, as before.
- The switch state is read from the `0xFFF3` attribute each time, not from a
  separate in-memory flag. zigpy saves the attribute cache and restores it when
  HA starts, so the heartbeat reply stays correct after a restart. A separate
  flag would reset to "off" on every restart and could switch off a thermostat
  the user had turned on.
- The heartbeat reply runs as a tracked background task. A failed reply is
  logged as a warning rather than showing up as an unretrieved task exception.
- The "thermostat on" and "thermostat off" frames are now built by their own
  methods, `_build_thermostat_on_frame` and `_build_thermostat_off_frame`.

### Thermostat mode reset on every reconfigure

Upstream's `apply_custom_configuration` always sent the "thermostat off" frame,
then the "thermostat on" frame. The device was therefore put into thermostat mode
on every pair or reconfigure, whatever the user had chosen.

It now reads the last stored value of the Thermostat Mode Switch (`0xFFF3`) and
sends the frame that matches it. If no value is stored it sends "off", so the
device stays in buttons-only mode. Binding the manufacturer cluster is now a
separate step with its own failure log.

ZHA only runs `apply_custom_configuration` when the device is paired or rejoins,
or when you press Reconfigure. It doesn't run on every HA startup. After a normal
restart, the heartbeat reply described above is what puts the device back into
the mode the user chose.

### Turning thermostat mode on or off

- `set_thermostat_mode` only updates the switch state (`0xFFF3`) and the
  climate entity once the device has accepted the frame. If the write fails it
  raises and nothing changes, so HA keeps showing the device's real state.
- The climate entity's `system_mode` now follows the switch. Turning the switch
  off sets it to `Off`. Turning it on restores the last HVAC mode the device
  reported or the user set.
- After the "thermostat on" frame, the quirk waits 0.5 s and then sends a PMTSD
  frame with `P=0`, which sets the device's running state to active. This
  matches what the reference quirk does; the delay gives the device time to
  process the "on" frame first.

### HVAC state lost on restart

Upstream kept the PMTSD values (`P`, `M`, `T`, `S`) in separate in-memory fields
that went back to hard-coded defaults on every restart. The first heartbeat reply
after a restart could then switch HVAC off and reset the setpoint to 20°C.

These values are now worked out from the attributes zigpy saves:

| PMTSD | Source |
| --- | --- |
| `P` | `system_mode` (`Off` → `1`, otherwise `0`) |
| `M` | `system_mode` (`Cool`/`Heat`/`Auto` → `0`/`1`/`2`). While off, the last active mode is used. |
| `T` | the heating setpoint, or the cooling setpoint in `Cool` mode |
| `S` | the fan cluster's `fan_mode` |
| `D` | echoed back unchanged (its meaning is unknown) |

The device has a single target temperature, so writing either setpoint now
updates both.

### Climate entity shown as "unknown" after a restart

`W100ThermostatCluster` is a virtual cluster: the physical device has no standard
Thermostat cluster. When ZHA read its attributes from the device, nothing came
back and the climate entity stayed `unknown`. On startup, zigpy also clears the
default values the cluster sets up and restores only the values it has saved.

`read_attributes` now first fills in any missing defaults, then calls zigpy's own
cache-only read. Results come back under the same keys the caller used (name or
ID).

### Thermostat defaults to off

Until the device reports its real state, the virtual thermostat assumes
`system_mode=Off` (`P=1`). That way the quirk won't send PMTSD frames that turn
on a device the user has switched off.

### Reporting configuration

The `configure_reporting` calls for the custom temperature (`0x0163`) and humidity
(`0x016A`) attributes no longer pass `manufacturer=0x115F`. zigpy takes the
manufacturer code from the attribute definitions, which are marked
`is_manufacturer_specific=True`.

## Internal cleanups

- A single helper, `_find_w100_thermostat`, now looks up the thermostat cluster
  on endpoint 1. It replaces several copies that each checked slightly different
  things.
- Frame builders use `_device_ieee_bytes()` instead of serialising the device's
  IEEE address inline.
- The thermostat and fan attribute IDs and the PMTSD `M`/`S` mappings are now
  defined once as module-level constants.

## Diagnostics

- Each PMTSD frame is logged at debug level when it is sent.
- Each heartbeat is logged at debug level, along with the current thermostat
  feature state.
