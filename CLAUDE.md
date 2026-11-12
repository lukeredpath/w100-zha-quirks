# W100 ZHA quirk

A custom ZHA quirk for the Aqara W100 climate sensor (`lumi.sensor_ht.agl001`,
Z2M model TH-S04D). Everything lives in one file,
`custom_zha_quirks/sensor_ht_agl001.py`, which users copy into Home Assistant's
`custom_zha_quirks` folder.

- **This is the maintained fork** of `Pulzki/w100-zha-quirks`; upstream appears
  unmaintained. `main` is the release branch: do work on a feature branch and
  merge it into `main`.
- **Releases** are git tags (`v1.0.0-beta.1`, ...). Add each release's notes to
  `CHANGES.md` under a new version heading.
- **Docs to keep in sync:** `README.md` (user-facing, including Known Issues) and
  `CHANGES.md` (the changelog). Update both when behaviour changes.

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

- The suite loads the quirk through ZHA's real `DEVICE_REGISTRY` with only the
  radio mocked. `pytest.ini` enforces **100% line and branch coverage**.
- Versions are pinned in `requirements_test.txt` (ZHA 2.4.3, zigpy 2.4.1,
  zha-quirks 2.4.1, which matches HA 2026.11).
- **Helpers in `tests/conftest.py`:**
  - `w100` fixture
  - `w100.receive(frame)`: the device reports on `0xFFF2`
  - `w100.frames`: PMTSD strings, `"ON"` or `"OFF"` written, acks excluded
  - `w100.acks`: counters echoed by acknowledgements
  - `w100.drain()`: wait for background tasks
  - `w100.restart({(ep, cluster, attr): value})`: mimics zigpy's startup: clear the
    cache, then restore persisted values
  - `pmtsd_frame()` and `HEARTBEAT`: built from frames captured from a real device
- **Delays:** `REGISTRATION_SETTLE_DELAY` and `STATE_CONFIRMATION_DELAY` are
  patched to 0 by autouse fixtures. Tests opt back in with `monkeypatch`.
- **When asserting a frame**, check what the real device would accept (see the
  protocol notes below), not just the string.

## W100 thermostat protocol (PMTSD)

Thermostat mode talks over the manufacturer attribute `0xFFF2` on cluster
`0xFCC0` (manufacturer code `0x115F`). The payload is ASCII
`P{power}_M{mode}_T{setpoint}_S{fan}_D{?}`.

- **Values:**
  - `P`: 0 = on, 1 = off
  - `M`: 0 = cool, 1 = heat, 2 = auto
  - `S`: 0 = auto, 1–3 = low, medium, high. There is no fan "off".
  - `D`: unknown, echoed back
- **Whole degrees only.** The device reports a 23.5 set on its own display as
  `T23`, and a decimal `T` from the hub has been seen to cause E01. `T` is
  always sent as an integer.
- **Device-originated frames** have byte 3 = `0x84`:
  - no payload: a heartbeat/request
  - with a payload: a local change; it can be partial (`M1_T24`) or empty
- **Every `0x84` frame must be acknowledged** with
  `aa 71 0d 24 <its counter> <checksum> 05 41 0a 00 00 00 54 ef 44 80 71 1a 00`.
  - Without an acknowledgement, the device holds back further local changes.
  - An acknowledgement alone isn't enough: until the hub sends the full state
    back as a PMTSD frame, it also shows **E02**.
  - The quirk sends that state back 1 s after the last local change. Empty
    payloads only get the acknowledgement.
- **Frame layout:**
  - Byte 5 is the Lumi header checksum: `(256 - sum(bytes 0..4)) & 0xFF`.
  - Bytes 2 and 8 are length fields: total − 6 and total − 9.
  - Byte 16 (`0x1A`) belongs to a fixed sequence; never change it.
  - The counter (byte 4) increments for each frame.
- **Device-sent `0x24` frames** are its acknowledgements of our frames; never
  answer them.
- **Thermostat mode switch** (`0xFFF3`):
  - It's toggled with dedicated ON/OFF registration frames, not with PMTSD.
  - With the switch off, the device drifts back into thermostat mode on its own
    and starts heartbeating; the quirk answers those heartbeats with the OFF
    frame.
  - While thermostat mode is off, the device acknowledges PMTSD frames but
    ignores them. Climate and fan changes are applied when the switch is turned
    on.
- **Reference:** zigbee2mqtt issues #33469 (protocol analysis) and #33624, and
  the Z2M converter in zigbee-herdsman-converters (`src/lib/lumi.ts`).

## ZHA / zigpy behaviour that matters here

- **Startup cache handling.** zigpy restores the attribute cache **after**
  constructing the quirk's clusters, and clears what `__init__` seeded first.
  Defaults therefore have to be re-seeded lazily (`_seed_defaults`). Derive
  state from attributes, not instance fields, so it survives restarts.
- **`.replaces()` clusters.** zigpy's v2 builder overwrites a replaced cluster's
  cache with the original's, so seeding in `__init__` has no effect on them.
- **Virtual clusters** (the thermostat, and the fan on endpoint 21) can't be
  read from the device. Their `read_attributes` answers from the cache through
  `super().read_attributes(..., only_cache=True)`, which keys results the way
  the caller asked (name or ID).
- **`apply_custom_configuration`** runs on pair, rejoin and Reconfigure. On the
  user's system it also ran on every ZHA start.
- **Command schemas are tuples.** zigpy returns responses such as
  `DefaultResponse` as tuple subclasses, so check `status_records` or `status`
  before treating a response as a list.

## Testing on real hardware

Machine-specific details (the Home Assistant instance, entity IDs, how builds
are deployed) are kept in `CLAUDE.local.md`, which isn't committed. Enable
debug logging with the logger name `sensor_ht_agl001`.

## Open issues

- **State is sometimes lost after a restart or ZHA reload.** Defaults get sent
  to the device and can turn the HVAC off. Evidence and the planned
  investigation are in the README's Known Issues and in local notes.
  The next step is a round-trip test through zigpy's real appdb (sqlite).
