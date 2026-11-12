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
| `T` | the heating setpoint, or the cooling setpoint in `Cool` mode, in whole degrees (see [PMTSD protocol fixes](#pmtsd-protocol-fixes)) |
| `S` | the fan cluster's `fan_mode` |
| `D` | the last value the device reported, echoed back (its meaning is unknown; not saved, so `0` after a restart until the device reports it) |

The device has a single target temperature, so writing either setpoint now
updates both.

### PMTSD protocol fixes

Hardware testing on 2026-11-12, together with the protocol analysis in
[zigbee2mqtt #33469](https://github.com/Koenkk/zigbee2mqtt/issues/33469), turned
up several problems with how upstream talks to the W100 in thermostat mode. Z2M
has the same problems.

- **The device's frames were never acknowledged.** The W100 expects an
  acknowledgement for every frame it sends (byte 3 = `0x84`): heartbeats and
  state changes made on the device. Without one it holds back further local
  changes and shows **E02**. We saw this on hardware: E02 appeared after a
  local change, with a 74-second gap in heartbeats. Every such frame is now
  answered with
  `aa 71 0d 24 <counter> <checksum> 05 41 0a 00 00 00 54 ef 44 80 71 1a 00`,
  echoing the device's counter. A heartbeat is acknowledged first and then
  answered as before. The device's own `0x24` acknowledgements are not
  answered.
- **The PMTSD frame had hard-coded length bytes.** Bytes 2 and 8 were fixed
  at `0x1F`/`0x1C`, which is only correct for a 15-character PMTSD string. They
  are now worked out from the frame length (total − 6 and total − 9). Byte 16
  (`0x1A`) belongs to a fixed sequence and stays as it is.
- **The header checksum was wrong.** Byte 5 is now the Lumi header checksum,
  `(256 − sum(bytes 0–4)) & 0xFF`, which every frame from the device satisfies.
  Upstream summed the whole frame instead.
- **The frame counter was random.** Byte 4 now increments for each frame, as
  the device's own counter does.
- **Setpoints were not whole degrees.** The protocol only carries whole
  degrees: 23.5 set on the device is reported as `T23`, and a decimal `T` from
  the hub has been seen to cause **E01**. `T` is now always sent as a whole
  number. A setpoint written from HA is rounded (half up), and the rounded
  value is stored, so the climate entity shows what the device actually has.
- **Changes made on the device were never confirmed.** An acknowledgement
  isn't enough on its own. After a change on the device, the W100 waits for
  the hub to send the state back as a PMTSD frame, and shows **E02** until it
  does, which in practice meant until the next heartbeat (60–90 s later). Each
  change that carries state is now acknowledged straight away and confirmed
  with the full state once the changes stop: 1 s after the last one, so a
  burst of button presses gets one confirmation. Frames with an empty payload
  are only acknowledged: they carry no state we can read, and sending ours back
  could undo the change. Nothing is sent back while the Thermostat Mode Switch
  is off.

  As a result, a half degree set on the device (for example 23.5) becomes the
  whole degree the device reported (23) once it's confirmed. The protocol has
  no way to carry it. Leaving `T` out of the reply instead was tried on
  hardware: it kept the half degree but left the device showing E02.

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

### Fan entity

- **Off showed as off in HA but AUTO on the device.** The device has no fan
  "off" (or the On/Smart presets ZHA also offers), so these are sent as Auto
  (`S0`). Upstream stored the value HA wrote, so the entity said off while the
  display showed AUTO. The value actually sent (Auto) is now stored, so the
  entity shows the Auto preset.
- **The fan came up off with no preset after a restart.** ZHA reads
  `fan_mode` from the device at startup, but the fan cluster is virtual (on an
  endpoint the quirk adds), so the read failed. Reads are now answered from
  the attribute cache, with Auto as the default, like the thermostat cluster.

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
- the PMTSD protocol (acknowledgements, frame layout, checksums, counters,
  whole-degree setpoints), checked against frames captured from a real W100
  and the reference frames in zigbee2mqtt #33469
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
