# Changes from upstream

This fork tracks [Pulzki/w100-zha-quirks](https://github.com/Pulzki/w100-zha-quirks).
It is based on upstream `c335919` (merge of PR #6, external temperature/humidity
sensor support). Every quirk change below is in
`custom_zha_quirks/sensor_ht_agl001.py`. The fork also adds an automated test
suite (see [Tests](#tests)).

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
checks the response itself, whether it's a Write Attributes Response or a Default
Response. If the device rejects a frame, the helper raises instead of treating
the write as a success.

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
| `D` | the last value the device reported, echoed back (its meaning is unknown; not saved, so `0` after a restart until the device reports it) |

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

### External sensor mode

Writing the tests turned up three bugs in the external temperature/humidity
support added in upstream PR #6:

- **Switching to external mode didn't send the stored measurements.** After the
  mode-change frames, the quirk sends the last external temperature and
  humidity. But `_send_external_measurement` skips sending unless the `sensor`
  attribute already says "external", and that attribute is only updated
  afterwards. So when switching from internal mode, the usual case, nothing was
  sent until the next value was written. These two sends now bypass that check.
- **The external values didn't survive a restart.** They were held in instance
  fields that went back to 20 °C / 50 % on every restart. They are now read
  from the saved `external_temperature` and `external_humidity` attributes,
  with 20 °C / 50 % as the fallback when nothing has been written yet.
- **The default values were never set.** `__init__` wrote 20 °C / 50 % into the
  attribute cache. For a cluster added with `.replaces()`, zigpy's v2 quirk
  builder then overwrites that cache with the original cluster's, so the
  defaults were lost and reads returned nothing. Reads now return the fallback
  values described above.

Reads of these two attributes are also returned under the key the caller used
(name or ID), as with the thermostat cluster, rather than always by ID.

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
- The "thermostat off" frame is padded with `ljust` instead of a length check
  that was always true.

## Diagnostics

- Each PMTSD frame is logged at debug level when it is sent.
- Each heartbeat is logged at debug level, along with the current thermostat
  feature state.

## Tests

Upstream has no tests. This fork adds a pytest suite in `tests/` that loads the
quirk through ZHA's real device registry, with only the Zigbee radio mocked. It
covers:

- thermostat mode switching, heartbeat replies and control-frame write status
- behaviour after a restart, by mimicking how zigpy clears and restores the
  attribute cache on startup
- `apply_custom_configuration`, including when individual steps fail
- the virtual thermostat and fan clusters, and PMTSD frames reported by the
  device
- buttons, battery parsing, external sensor mode, the Basic cluster and the
  device's cluster lookup

Coverage of `sensor_ht_agl001.py` is enforced at 100% (lines and branches).
`requirements_test.txt` pins the versions the suite runs against: ZHA 2.4.3,
zigpy 2.4.1 and zha-quirks 2.4.1. See the README for how to run it.
