"""Thermostat Mode Switch, heartbeat replies and control-frame writes."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from zigpy.zcl import foundation
from zigpy.zcl.clusters.hvac import Fan, Thermostat

import sensor_ht_agl001 as quirk
from conftest import HEARTBEAT, build_w100, pmtsd_frame, write_response

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
    w100.thermostat.set_setpoint(2150)
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_T21.5_S0_D0"]


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

    assert w100.frames == ["ON", "P0_M1_T20.0_S0_D0"]
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


async def test_pmtsd_frame_checksum_and_payload(w100):
    await w100.manu.send_pmtsd(0, 1, 21.5, 2, 0)
    (frame,) = w100.writes
    assert frame.endswith(b"P0_M1_T21.5_S2_D0")
    assert frame[21] == len(b"P0_M1_T21.5_S2_D0")
    assert frame[5] == (sum(frame) - frame[5]) & 0xFF


# --- restart behaviour -----------------------------------------------------------------


async def test_restart_with_switch_on_replies_with_persisted_state(w100):
    w100.restart({
        (1, 0xFCC0, 0xFFF3): True,
        (1, 0x0201, quirk.SYSTEM_MODE_ATTR): SystemMode.Heat,
        (1, 0x0201, quirk.HEATING_SETPOINT_ATTR): 2200,
        (1, 0x0201, quirk.COOLING_SETPOINT_ATTR): 2200,
        (21, 0x0202, quirk.FAN_MODE_ATTR): Fan.FanMode.Medium,
    })
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M1_T22.0_S2_D0"]


async def test_restart_with_switch_off_reasserts_off(w100):
    w100.restart({(1, 0xFCC0, 0xFFF3): False})
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["OFF"]


async def test_restart_uses_cooling_setpoint_in_cool_mode(w100):
    w100.restart({
        (1, 0xFCC0, 0xFFF3): True,
        (1, 0x0201, quirk.SYSTEM_MODE_ATTR): SystemMode.Cool,
        (1, 0x0201, quirk.COOLING_SETPOINT_ATTR): 2400,
    })
    w100.receive(HEARTBEAT)
    await w100.drain()
    assert w100.frames == ["P0_M0_T24.0_S0_D0"]


async def test_restart_with_nothing_persisted_uses_defaults(w100):
    w100.restart({(1, 0xFCC0, 0xFFF3): True})
    assert w100.thermostat.pmtsd_state() == (1, 0, 20.0, 0, 0)


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
