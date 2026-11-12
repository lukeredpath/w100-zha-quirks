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

- The cluster now remembers whether the thermostat feature is on, in
  `_thermostat_feature_on`. `set_thermostat_mode` sets it.
- A new method, `_reply_to_heartbeat`, answers heartbeats based on the switch:
  - **Switch off:** sends the "thermostat off" frame again, which puts the
    device back into buttons-only mode.
  - **Switch on:** sends the cached PMTSD frame, as before.
- The "thermostat off" frame builder is now its own method,
  `_build_thermostat_off_frame`, so the heartbeat reply can reuse it.

### Thermostat mode reset on every restart

Upstream's `apply_custom_configuration` always sent the "thermostat off" frame,
then the "thermostat on" frame. The device was therefore put into thermostat mode
on every reconfigure or restart, whatever the user had chosen.

On startup the quirk now reads the last stored value of the Thermostat Mode
Switch (`0xFFF3`) and sends the frame that matches it. If no value is stored it
sends "off", so the device stays in buttons-only mode. This is needed because the
device goes back to its firmware default (thermostat on) whenever it reconnects,
for example during an HA/ZHA restart while its heartbeats go unanswered. Binding
the manufacturer cluster is now a separate step with its own failure log.

### Turning thermostat mode on

- After the "thermostat on" frame, the quirk now also sends a PMTSD frame with
  `P=0`, which sets the device's running state to active. This matches what the
  reference quirk does.
- `set_thermostat_mode` keeps the thermostat cluster's PMTSD power flag,
  `_cached_p`, in step with the switch: `0` when on, `1` when off.

### Climate entity shown as "unknown" after a restart

`W100ThermostatCluster` is a virtual cluster: the physical device has no standard
Thermostat cluster. When ZHA read its attributes from the device, nothing came
back and the climate entity stayed `unknown`.

`read_attributes` is now overridden to answer from the local attribute cache.
Attributes that aren't in the cache are returned as `UNSUPPORTED_ATTRIBUTE`.

### Thermostat power flag default

`_cached_p` now starts at `1` (off) instead of `0`. This matches the
`system_mode=Off` starting value. Until the device reports its real state, the
quirk won't send PMTSD frames that turn on a device the user has switched off.

### Reporting configuration

The `configure_reporting` calls for the custom temperature (`0x0163`) and humidity
(`0x016A`) attributes no longer pass `manufacturer=0x115F`. zigpy takes the
manufacturer code from the attribute definitions, which are marked
`is_manufacturer_specific=True`.

## Diagnostics

- Each PMTSD frame is logged at debug level when it is sent.
- Each heartbeat is logged at debug level, along with the current thermostat
  feature state.
