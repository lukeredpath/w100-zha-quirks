"""Thermostat Mode Switch, heartbeat replies and control-frame writes."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from zigpy.zcl import foundation
from zigpy.zcl.clusters.hvac import Fan, Thermostat

import sensor_ht_agl001 as quirk
from conftest import HEARTBEAT, HEARTBEAT_COUNTER, build_w100, header_checksum_ok, pmtsd_frame, write_response

FAIL = write_response(foundation.Status.FAILURE)
SystemMode = Thermostat.SystemMode


# --- heartbeat replies ---------------------------------------------------------------


async def test_heartbeat_with_switch_unset_reasserts_off_frame(w100):
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["OFF"]


async def test_heartbeat_with_switch_off_reasserts_off_frame(w100):
    w100.manu._update_attribute(0xFFF3, False)
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["OFF"]


async def test_heartbeat_with_switch_on_replies_with_current_pmtsd(w100):
    w100.manu._update_attribute(0xFFF3, True)
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Heat)
    w100.thermostat.set_setpoint(2150, from_ha=True)
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_T22_S0_D0"]


async def test_failed_heartbeat_reply_is_logged_not_raised(w100, caplog):
    w100.response = FAIL
    with caplog.at_level(logging.WARNING):
        w100.receive(HEARTBEAT)
        await w100.drain()
    assert "heartbeat reply failed" in caplog.text
    assert not w100.manu._background_tasks


async def test_background_task_reference_released_on_cancel(w100):
    w100.manu._spawn(asyncio.sleep(10), "sleeper")
    (task,) = w100.manu._background_tasks
    task.cancel()
    await w100.drain()
    assert not w100.manu._background_tasks


# --- switch on / off -------------------------------------------------------------------


async def test_switch_on_sends_registration_then_active_pmtsd(w100):
    w100.thermostat._last_m = 1
    result = await w100.manu.write_attributes({"thermostat_mode_switch": True})

    assert w100.frames == ["ON", "P0_M1_S0_D0"]
    assert w100.manu.get(0xFFF3) is True
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat
    assert result == [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS, attrid=0xFFF3)]]


async def test_switch_on_by_attribute_id(w100):
    await w100.manu.write_attributes({0xFFF3: True})
    assert w100.frames[0] == "ON"
    assert w100.manu.get(0xFFF3) is True


async def test_switch_on_waits_before_pmtsd(w100, monkeypatch):
    monkeypatch.setattr(quirk, "REGISTRATION_SETTLE_DELAY", 0.05)
    await w100.manu.set_thermostat_mode("ON")
    gap = w100.write_times[1] - w100.write_times[0]
    assert gap >= 0.05


async def test_switch_off_sends_unregister_and_turns_hvac_off(w100):
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Heat)
    await w100.manu.write_attributes({"thermostat_mode_switch": False})

    assert w100.frames == ["OFF"]
    assert w100.manu.get(0xFFF3) is False
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Off


async def test_switch_on_restores_last_active_mode(w100):
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Auto)
    await w100.manu.set_thermostat_mode("OFF")
    await w100.manu.set_thermostat_mode("ON")
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Auto
    assert w100.frames[-1].startswith("P0_M2")


async def test_failed_switch_write_raises_and_leaves_state_unchanged(w100):
    await w100.manu.set_thermostat_mode("OFF")
    w100.response = FAIL

    with pytest.raises(RuntimeError, match="rejected"):
        await w100.manu.write_attributes({"thermostat_mode_switch": True})

    assert w100.manu.get(0xFFF3) is False
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Off


async def test_switch_without_thermostat_cluster_still_sends_frames(w100):
    del w100.device.endpoints[1].in_clusters[Thermostat.cluster_id]
    await w100.manu.set_thermostat_mode("ON")
    # No thermostat -> no PMTSD state to send after registration.
    assert w100.frames == ["ON"]
    assert w100.manu.get(0xFFF3) is True


def test_frames_embed_device_ieee(w100):
    ieee = w100.manu._device_ieee_bytes()
    assert ieee in w100.manu._build_thermostat_on_frame()
    off = w100.manu._build_thermostat_off_frame()
    assert ieee in off
    assert len(off) == 34


# --- write status handling -------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        write_response(foundation.Status.SUCCESS),
        foundation.DefaultResponse(command_id=0x02, status=foundation.Status.SUCCESS),
        [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]],
        None,
    ],
    ids=["write-response", "default-response", "legacy-list", "no-response"],
)
async def test_control_frame_write_accepts_success(w100, response):
    w100.response = response
    await w100.manu._write_w100_control_frame(b"\x01\x02")
    assert w100.writes == [b"\x01\x02"]


@pytest.mark.parametrize(
    "response",
    [
        write_response(foundation.Status.FAILURE),
        foundation.DefaultResponse(command_id=0x02, status=foundation.Status.UNSUP_MANUF_GENERAL_COMMAND),
        [[foundation.WriteAttributesStatusRecord(foundation.Status.INVALID_VALUE, attrid=0xFFF2)]],
    ],
    ids=["write-response", "default-response", "legacy-list"],
)
async def test_control_frame_write_raises_on_rejection(w100, response):
    w100.response = response
    with pytest.raises(RuntimeError, match="rejected"):
        await w100.manu._write_w100_control_frame(b"\x01\x02")


@pytest.mark.parametrize(
    ("args", "payload"),
    [
        ((0, 1, 21.0, 2, 0), b"P0_M1_T21_S2_D0"),
        ((0, 1, 21.5, 2, 0), b"P0_M1_T22_S2_D0"),  # whole degrees, half up
        ((0, 1, 7, 2, 0), b"P0_M1_T7_S2_D0"),  # 14 chars: broke the old hardcoded lengths
        ((0, 1, None, 2, 0), b"P0_M1_S2_D0"),
    ],
    ids=["whole", "half-rounds-up", "single-digit", "without-T"],
)
async def test_pmtsd_frame_layout(w100, args, payload):
    await w100.manu.send_pmtsd(*args)
    (frame,) = w100.writes
    assert frame.endswith(payload)
    assert frame[21] == len(payload)
    # Lumi length fields: bytes after the 9-byte header, and that + 3.
    assert frame[8] == len(frame) - 9
    assert frame[2] == len(frame) - 6
    assert frame[16] == 0x1A  # fixed sequence byte, must not change
    assert header_checksum_ok(frame)


def test_pmtsd_length_fields_match_previous_hardcoded_values_for_15_chars():
    """The old constants (0x1F, 0x1C) are what the formula gives for a 15-char payload."""
    body_len = 13 + 15
    assert (body_len + 3, body_len) == (0x1F, 0x1C)


# --- setpoint echo (device reports whole degrees) --------------------------------------


async def test_device_reported_setpoint_is_not_echoed(w100):
    """User sets 24.5 on the device; it reports T24. The heartbeat must not send T24."""
    w100.manu._update_attribute(0xFFF3, True)
    # The sequence seen on hardware: a full report, then a partial one.
    w100.receive(pmtsd_frame("P0_M1_T23_S0"))
    w100.receive(pmtsd_frame("M1_T24"))
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_S0_D0"]
    # The reported value is still stored for HA to display.
    assert w100.thermostat.get(quirk.HEATING_SETPOINT_ATTR) == 2400


async def test_ha_set_setpoint_is_asserted(w100):
    w100.manu._update_attribute(0xFFF3, True)
    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2150})
    w100.writes.clear()
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P1_M0_T22_S0_D0"]


async def test_device_report_after_ha_setpoint_stops_asserting_it(w100):
    w100.manu._update_attribute(0xFFF3, True)
    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2150})
    w100.receive(pmtsd_frame("P0_M1_T23"))
    w100.writes.clear()
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_S0_D0"]


async def test_report_without_setpoint_keeps_ha_setpoint(w100):
    w100.manu._update_attribute(0xFFF3, True)
    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2150})
    w100.receive(pmtsd_frame("P0_M1"))
    w100.writes.clear()
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_T22_S0_D0"]


async def test_ha_setpoint_uses_cooling_setpoint_in_cool_mode(w100):
    w100.manu._update_attribute(0xFFF3, True)
    await w100.thermostat.write_attributes({"system_mode": SystemMode.Cool, "occupied_cooling_setpoint": 2400})
    w100.writes.clear()
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M0_T24_S0_D0"]


# --- restart behaviour -----------------------------------------------------------------


async def test_restart_with_switch_on_replies_with_persisted_state_without_setpoint(w100):
    """After a restart the setpoint's source is unknown, so T is left out."""
    w100.restart({
        (1, 0xFCC0, 0xFFF3): True,
        (1, 0x0201, quirk.SYSTEM_MODE_ATTR): SystemMode.Heat,
        (1, 0x0201, quirk.HEATING_SETPOINT_ATTR): 2200,
        (1, 0x0201, quirk.COOLING_SETPOINT_ATTR): 2200,
        (21, 0x0202, quirk.FAN_MODE_ATTR): Fan.FanMode.Medium,
    })
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_S2_D0"]


async def test_restart_with_switch_off_reasserts_off(w100):
    w100.restart({(1, 0xFCC0, 0xFFF3): False})
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["OFF"]


async def test_restart_in_cool_mode(w100):
    w100.restart({
        (1, 0xFCC0, 0xFFF3): True,
        (1, 0x0201, quirk.SYSTEM_MODE_ATTR): SystemMode.Cool,
        (1, 0x0201, quirk.COOLING_SETPOINT_ATTR): 2400,
    })
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M0_S0_D0"]


async def test_restart_with_nothing_persisted_uses_defaults(w100):
    w100.restart({(1, 0xFCC0, 0xFFF3): True})
    assert w100.thermostat.pmtsd_state() == (1, 0, None, 0, 0)


# --- apply_custom_configuration --------------------------------------------------------


class _Recorder:
    """Async stand-in that records calls and can be made to fail."""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc


def _stub_configuration(w100, **failures):
    ep1 = w100.device.endpoints[1]
    temp = ep1.in_clusters[0x0402]
    power = ep1.in_clusters[0x0001]
    stubs = SimpleNamespace(
        temp_bind=_Recorder(failures.get("temp_bind")),
        temp_reporting=_Recorder(),
        temp_read=_Recorder(failures.get("sync")),
        manu_reporting=_Recorder(),
        manu_bind=_Recorder(failures.get("manu_bind")),
        manu_read=_Recorder(),
        power_bind=_Recorder(),
        power_reporting=_Recorder(),
    )
    temp.bind = stubs.temp_bind
    temp.configure_reporting = stubs.temp_reporting
    temp.read_attributes = stubs.temp_read
    power.bind = stubs.power_bind
    power.configure_reporting = stubs.power_reporting
    w100.manu.configure_reporting = stubs.manu_reporting
    w100.manu.bind = stubs.manu_bind
    w100.manu.read_attributes = stubs.manu_read
    return stubs


async def test_configuration_binds_reports_and_defaults_switch_off(w100):
    stubs = _stub_configuration(w100)
    await w100.manu.apply_custom_configuration()

    assert stubs.temp_bind.calls and stubs.power_bind.calls and stubs.manu_bind.calls
    assert [c[0][0] for c in stubs.manu_reporting.calls] == [0x0163, 0x016A]
    assert stubs.temp_reporting.calls[0][0] == (0x0000, 10, 3600, 50)
    assert stubs.power_reporting.calls[0][0] == (0x0020, 3600, 21600, 1)
    assert w100.frames == ["OFF"]
    assert stubs.manu_read.calls[0][0] == ([quirk.W100_ATTR],)
    assert stubs.temp_read.calls


async def test_configuration_reasserts_persisted_switch_on(w100):
    _stub_configuration(w100)
    w100.manu._update_attribute(0xFFF3, True)
    await w100.manu.apply_custom_configuration()
    assert w100.frames[0] == "ON"


async def test_configuration_survives_every_step_failing(w100, caplog):
    _stub_configuration(
        w100,
        temp_bind=RuntimeError("no route"),
        manu_bind=RuntimeError("no bind"),
        sync=RuntimeError("no sync"),
    )
    w100.response = FAIL
    with caplog.at_level(logging.WARNING):
        await w100.manu.apply_custom_configuration()

    assert "Failed to configure reporting" in caplog.text
    assert "Failed to bind manufacturer cluster" in caplog.text
    assert "Failed to re-assert thermostat mode" in caplog.text
    assert "Failed to sync state" in caplog.text


async def test_configuration_without_temperature_or_power_clusters(w100):
    stubs = _stub_configuration(w100)
    ep1 = w100.device.endpoints[1]
    del ep1.in_clusters[0x0402]
    del ep1.in_clusters[0x0001]
    await w100.manu.apply_custom_configuration()
    assert not stubs.temp_bind.calls and not stubs.power_bind.calls
    assert stubs.manu_reporting.calls


async def test_device_configuration_dispatches_to_cluster(w100):
    """ZHA calls apply_custom_configuration on the device; it must reach our cluster."""
    _stub_configuration(w100)
    await w100.device.apply_custom_configuration()
    assert w100.frames == ["OFF"]


def test_build_w100_fixture_is_isolated():
    a, b = build_w100(), build_w100()
    a.manu._update_attribute(0xFFF3, True)
    assert b.manu.get(0xFFF3) is None


# --- acknowledgements and frame counters (zigbee2mqtt issue #33469) --------------------


ACK_FROM_ISSUE = bytes.fromhex("aa710d2499" + "1b" + "05410a00000054ef4480711a00")


async def test_heartbeat_is_acknowledged_before_reply(w100):
    w100.manu._update_attribute(0xFFF3, True)
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.acks == [HEARTBEAT_COUNTER]
    assert w100.writes[0][3] == 0x24  # the ack goes first
    assert w100.frames == ["P1_M0_S0_D0"]


async def test_heartbeat_with_switch_off_is_acknowledged(w100):
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.acks == [HEARTBEAT_COUNTER]
    assert w100.frames == ["OFF"]


async def test_device_state_change_is_acknowledged_without_reply(w100):
    w100.receive(pmtsd_frame("P0_M1_T23_S0", counter=0x06))
    await w100.drain()
    assert w100.acks == [0x06]
    assert w100.frames == []


async def test_real_device_frames_are_acknowledged(w100):
    """Frames captured from the W100: a state change with partial payload and an empty one."""
    w100.receive(bytes.fromhex("aa7217840a3f014117000054ef4480711a08000844064d315f54323400"))
    w100.receive(bytes.fromhex("aa7211840b44014111000054ef4480711a080008440000"))
    await w100.drain()
    assert w100.acks == [0x0A, 0x0B]
    assert w100.thermostat.get(quirk.HEATING_SETPOINT_ATTR) == 2400


async def test_device_acks_of_our_frames_are_not_acknowledged(w100):
    """The device's own 0x24 acks (e.g. aa710d24...) must not be answered."""
    w100.receive(bytes.fromhex("aa710d24bbf905410a00000054ef4480711a00"))
    await w100.drain()
    assert w100.writes == []


async def test_ack_layout_matches_reference(w100):
    await w100.manu._send_ack(0x99)
    (ack,) = w100.writes
    assert ack == ACK_FROM_ISSUE
    assert header_checksum_ok(ack)


async def test_failed_ack_is_logged_not_raised(w100, caplog):
    import logging

    w100.response = FAIL
    with caplog.at_level(logging.WARNING):
        w100.receive(pmtsd_frame("P0_M1", counter=0x10))
        await w100.drain()
    assert "acknowledgement failed" in caplog.text


async def test_frame_counter_increments_and_wraps(w100):
    w100.manu._frame_counter = 0xFE
    for _ in range(3):
        await w100.manu.send_pmtsd(0, 1, None, 0, 0)
    assert [frame[4] for frame in w100.writes] == [0xFF, 0x00, 0x01]
    assert all(header_checksum_ok(frame) for frame in w100.writes)


def test_header_checksum_matches_device_frames():
    for hex_frame in (
        "aa710f84074b06410f000054ef4480711a08000844",  # heartbeat
        "aa7217840a3f014117000054ef4480711a08000844064d315f54323400",  # state change
        "aa710d24bbf905410a00000054ef4480711a00",  # device ack
    ):
        frame = bytes.fromhex(hex_frame)
        assert quirk.W100ManuSpecificCluster._header_checksum(frame) == frame[5]


@pytest.mark.parametrize(("value", "expected"), [(20.0, 20), (20.4, 20), (20.5, 21), (7, 7), (-0.5, 0)])
def test_whole_degrees(value, expected):
    assert quirk.whole_degrees(value) == expected
