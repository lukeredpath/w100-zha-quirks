"""Virtual thermostat and fan clusters, and device-reported PMTSD frames."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from zigpy.zcl import foundation
from zigpy.zcl.clusters.hvac import Fan, Thermostat

import sensor_ht_agl001 as quirk
from conftest import pmtsd_frame

SystemMode = Thermostat.SystemMode


# --- thermostat reads ------------------------------------------------------------------


async def test_read_by_name_is_keyed_by_name(w100):
    success, failure = await w100.thermostat.read_attributes(["system_mode", "occupied_heating_setpoint"])
    assert success == {"system_mode": SystemMode.Off, "occupied_heating_setpoint": 2000}
    assert failure == {}


async def test_read_by_id_is_keyed_by_id(w100):
    success, _ = await w100.thermostat.read_attributes([quirk.SYSTEM_MODE_ATTR])
    assert success == {quirk.SYSTEM_MODE_ATTR: SystemMode.Off}


async def test_read_never_hits_the_device(w100, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("device read attempted")

    monkeypatch.setattr(w100.thermostat, "read_attributes_raw", boom)
    success, failure = await w100.thermostat.read_attributes(["local_temperature"])
    assert success == {}
    assert failure == {}


async def test_read_after_restart_reseeds_defaults(w100):
    w100.restart({(1, 0x0201, quirk.SYSTEM_MODE_ATTR): SystemMode.Heat})
    success, _ = await w100.thermostat.read_attributes(
        ["system_mode", "occupied_heating_setpoint", "occupied_cooling_setpoint", "running_state"]
    )
    assert success == {
        "system_mode": SystemMode.Heat,  # persisted value wins over the seed
        "occupied_heating_setpoint": 2000,
        "occupied_cooling_setpoint": 2000,
        "running_state": 0,
    }


# --- thermostat writes -----------------------------------------------------------------


async def test_system_mode_write_sends_pmtsd(w100):
    result = await w100.thermostat.write_attributes({"system_mode": SystemMode.Heat})
    assert w100.frames == ["P0_M1_T20_S0_D0"]
    assert w100.thermostat._last_m == 1
    assert result == [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS, attrid=quirk.SYSTEM_MODE_ATTR)]]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(SystemMode.Cool, "P0_M0"), (SystemMode.Heat, "P0_M1"), (SystemMode.Auto, "P0_M2"), (SystemMode.Off, "P1_M0")],
)
async def test_system_mode_mapping(w100, mode, expected):
    await w100.thermostat.write_attributes({quirk.SYSTEM_MODE_ATTR: mode})
    assert w100.frames[-1].startswith(expected)


async def test_off_keeps_last_active_mode(w100):
    await w100.thermostat.write_attributes({"system_mode": SystemMode.Auto})
    await w100.thermostat.write_attributes({"system_mode": SystemMode.Off})
    assert w100.frames[-1].startswith("P1_M2")


@pytest.mark.parametrize("attr", ["occupied_heating_setpoint", quirk.COOLING_SETPOINT_ATTR])
async def test_setpoint_write_mirrors_both_setpoints(w100, attr):
    await w100.thermostat.write_attributes({attr: 2350})
    # Whole degrees only (the protocol can't carry 23.5), stored as sent.
    assert w100.thermostat.get(quirk.HEATING_SETPOINT_ATTR) == 2400
    assert w100.thermostat.get(quirk.COOLING_SETPOINT_ATTR) == 2400
    assert w100.frames == ["P1_M0_T24_S0_D0"]


async def test_write_other_attribute_updates_cache(w100):
    await w100.thermostat.write_attributes({"local_temperature": 1900})
    assert w100.thermostat.get(quirk.LOCAL_TEMPERATURE_ATTR) == 1900


async def test_write_without_manufacturer_cluster_does_not_send(w100):
    del w100.device.endpoints[1].in_clusters[0xFCC0]
    await w100.thermostat.write_attributes({"system_mode": SystemMode.Heat})
    assert w100.writes == []
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat


# --- running state ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "local", "setpoint", "expected"),
    [
        (SystemMode.Heat, 1800, 2000, 0x0001),
        (SystemMode.Heat, 2100, 2000, 0),
        (SystemMode.Cool, 2500, 2000, 0x0002),
        (SystemMode.Cool, 1900, 2000, 0),
        (SystemMode.Auto, 1800, 2000, 0),
        (SystemMode.Off, 1800, 2000, 0),
    ],
)
def test_running_state(w100, mode, local, setpoint, expected):
    t = w100.thermostat
    t.update_attribute(quirk.SYSTEM_MODE_ATTR, mode)
    t.set_setpoint(setpoint)
    t.update_attribute(quirk.LOCAL_TEMPERATURE_ATTR, local)
    t.recalculate_running_state()
    assert t.get(quirk.RUNNING_STATE_ATTR) == expected


def test_running_state_pulls_local_temperature_from_sensor(w100):
    w100.restart({})
    w100.thermostat._seed_defaults()
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Heat)
    temp = w100.device.endpoints[1].in_clusters[0x0402]
    temp._attr_cache.set_value(temp.find_attribute(0x0000), 1700)

    w100.thermostat.recalculate_running_state()

    assert w100.thermostat.get(quirk.LOCAL_TEMPERATURE_ATTR) == 1700
    assert w100.thermostat.get(quirk.RUNNING_STATE_ATTR) == 0x0001


def test_running_state_unchanged_without_any_temperature(w100):
    w100.restart({})
    w100.thermostat._seed_defaults()
    w100.thermostat.update_attribute(quirk.RUNNING_STATE_ATTR, 0x0001)
    w100.thermostat.recalculate_running_state()
    assert w100.thermostat.get(quirk.RUNNING_STATE_ATTR) == 0x0001


def test_running_state_without_temperature_cluster(w100):
    w100.restart({})
    w100.thermostat._seed_defaults()
    del w100.device.endpoints[1].in_clusters[0x0402]
    w100.thermostat.recalculate_running_state()
    assert w100.thermostat.get(quirk.LOCAL_TEMPERATURE_ATTR) is None


def test_temperature_report_updates_thermostat(w100):
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Heat)
    temp = w100.device.endpoints[1].in_clusters[0x0402]
    temp.update_attribute(0x0000, 1850)
    assert w100.thermostat.get(quirk.LOCAL_TEMPERATURE_ATTR) == 1850
    assert w100.thermostat.get(quirk.RUNNING_STATE_ATTR) == 0x0001


def test_temperature_report_other_attribute_ignored(w100):
    temp = w100.device.endpoints[1].in_clusters[0x0402]
    temp.update_attribute(0x0001, -4000)  # min_measured_value
    assert w100.thermostat.get(quirk.LOCAL_TEMPERATURE_ATTR) is None


def test_temperature_report_to_foreign_thermostat(w100):
    foreign = MagicMock(spec=["update_attribute"])
    w100.device.endpoints[1].in_clusters[Thermostat.cluster_id] = foreign
    w100.device.endpoints[1].in_clusters[0x0402].update_attribute(0x0000, 1850)
    foreign.update_attribute.assert_called_once_with(0x0000, 1850)


def test_temperature_report_without_thermostat(w100):
    del w100.device.endpoints[1].in_clusters[Thermostat.cluster_id]
    temp = w100.device.endpoints[1].in_clusters[0x0402]
    temp.update_attribute(0x0000, 1850)
    assert temp.get(0x0000) == 1850


# --- pmtsd_state / helpers -------------------------------------------------------------


def test_pmtsd_state_without_fan_endpoint(w100):
    del w100.device.endpoints[21]
    w100.fan.update_attribute(quirk.FAN_MODE_ATTR, Fan.FanMode.High)
    assert w100.thermostat.pmtsd_state()[3] == 0


def test_find_thermostat_without_endpoint_1():
    assert quirk._find_w100_thermostat(SimpleNamespace(endpoints={})) is None


def test_find_thermostat_ignores_foreign_cluster():
    ep1 = SimpleNamespace(in_clusters={Thermostat.cluster_id: object()})
    assert quirk._find_w100_thermostat(SimpleNamespace(endpoints={1: ep1})) is None


# --- fan -------------------------------------------------------------------------------


def test_fan_defaults(w100):
    assert w100.fan.get(quirk.FAN_MODE_ATTR) == Fan.FanMode.Auto
    assert w100.fan.get(0x0001) == Fan.FanModeSequence.Low_Med_High_Auto


async def test_fan_read_after_restart_serves_defaults_without_device_read(w100, monkeypatch):
    """ZHA reads fan_mode on startup; the virtual fan cluster can't be read from the device."""
    async def boom(*args, **kwargs):
        raise AssertionError("device read attempted")

    monkeypatch.setattr(w100.fan, "read_attributes_raw", boom)
    w100.restart({})
    success, failure = await w100.fan.read_attributes(["fan_mode", "fan_mode_sequence"])
    assert success == {"fan_mode": Fan.FanMode.Auto, "fan_mode_sequence": Fan.FanModeSequence.Low_Med_High_Auto}
    assert failure == {}


async def test_fan_read_after_restart_keeps_persisted_mode(w100):
    w100.restart({(21, 0x0202, quirk.FAN_MODE_ATTR): Fan.FanMode.High})
    success, _ = await w100.fan.read_attributes([quirk.FAN_MODE_ATTR])
    assert success == {quirk.FAN_MODE_ATTR: Fan.FanMode.High}


@pytest.mark.parametrize(
    ("mode", "s", "stored"),
    [
        (Fan.FanMode.Low, 1, Fan.FanMode.Low),
        (Fan.FanMode.Medium, 2, Fan.FanMode.Medium),
        (Fan.FanMode.High, 3, Fan.FanMode.High),
        (Fan.FanMode.Auto, 0, Fan.FanMode.Auto),
        # The device has no off/on/smart: these are sent, and stored, as Auto
        # so HA shows what the device displays.
        (Fan.FanMode.Off, 0, Fan.FanMode.Auto),
        (Fan.FanMode.On, 0, Fan.FanMode.Auto),
        (Fan.FanMode.Smart, 0, Fan.FanMode.Auto),
    ],
)
async def test_fan_write_sends_s(w100, mode, s, stored):
    result = await w100.fan.write_attributes({"fan_mode": mode})
    assert w100.fan.get(quirk.FAN_MODE_ATTR) == stored
    assert w100.frames == [f"P1_M0_T20_S{s}_D0"]
    assert result == [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS, attrid=0x0000)]]


async def test_fan_write_by_id(w100):
    await w100.fan.write_attributes({0x0000: Fan.FanMode.High})
    assert w100.frames == ["P1_M0_T20_S3_D0"]


async def test_fan_write_without_manufacturer_cluster(w100):
    del w100.device.endpoints[1].in_clusters[0xFCC0]
    await w100.fan.write_attributes({"fan_mode": Fan.FanMode.Low})
    assert w100.writes == []
    assert w100.fan.get(quirk.FAN_MODE_ATTR) == Fan.FanMode.Low


async def test_fan_write_without_endpoint_1(w100):
    del w100.device.endpoints[1]
    await w100.fan.write_attributes({"fan_mode": Fan.FanMode.Low})
    assert w100.writes == []


async def test_fan_other_attribute_passes_through(w100, monkeypatch):
    calls = []

    async def parent_write(self, attributes, manufacturer=None):
        calls.append(attributes)
        return "parent"

    monkeypatch.setattr(quirk.W100FanCluster.__mro__[1], "write_attributes", parent_write)
    assert await w100.fan.write_attributes({"fan_mode_sequence": 2}) == "parent"
    assert calls == [{"fan_mode_sequence": 2}]


# --- device-reported PMTSD -------------------------------------------------------------


async def test_device_report_updates_thermostat_and_fan(w100):
    w100.receive(pmtsd_frame("P0_M1_T22.5_S3_D1"))
    t = w100.thermostat
    assert t.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat
    assert t.get(quirk.HEATING_SETPOINT_ATTR) == 2250
    assert t.get(quirk.COOLING_SETPOINT_ATTR) == 2250
    assert w100.fan.get(quirk.FAN_MODE_ATTR) == Fan.FanMode.High
    assert t.pmtsd_state() == (0, 1, 23, 3, 1.0)  # 22.5 -> whole degrees


async def test_device_report_off_remembers_mode(w100):
    w100.receive(pmtsd_frame("P1_M2_T19.5_S1_D0"))
    t = w100.thermostat
    assert t.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Off
    assert t._last_m == 2
    t.set_active(True)
    assert t.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Auto


async def test_device_report_partial_payload_keeps_current_mode(w100):
    w100.thermostat.update_attribute(quirk.SYSTEM_MODE_ATTR, SystemMode.Cool)
    w100.receive(pmtsd_frame("T18"))
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Cool
    assert w100.thermostat.get(quirk.COOLING_SETPOINT_ATTR) == 1800


async def test_device_report_unknown_mode_is_off(w100):
    w100.receive(pmtsd_frame("P0_M7"))
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Off


async def test_device_report_ignores_malformed_pairs(w100):
    w100.receive(pmtsd_frame("P0_M1_X_Tabc_"))
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat
    assert w100.thermostat.get(quirk.HEATING_SETPOINT_ATTR) == 2000


async def test_device_report_without_thermostat(w100):
    del w100.device.endpoints[1].in_clusters[Thermostat.cluster_id]
    w100.receive(pmtsd_frame("P0_M1_S3"))
    assert w100.fan.get(quirk.FAN_MODE_ATTR) == Fan.FanMode.Auto


async def test_device_report_without_fan_endpoint(w100):
    del w100.device.endpoints[21]
    w100.receive(pmtsd_frame("P0_M1_S3"))
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat


async def test_device_report_fan_endpoint_without_fan_cluster(w100):
    del w100.device.endpoints[21].in_clusters[Fan.cluster_id]
    w100.receive(pmtsd_frame("P0_M1_S3"))
    assert w100.thermostat.get(quirk.SYSTEM_MODE_ATTR) == SystemMode.Heat


@pytest.mark.parametrize(
    "data",
    [
        b"",
        "not bytes",
        b"\xaa\x71\x10\x44\x00",  # no end marker
        bytes([0xAA, 0x71, 0x10, 0x44, 0, 0, 0x08, 0x44]),  # marker at end, not a heartbeat
        bytes([0x08, 0x44]),  # marker at end, too short to be a heartbeat
        bytes([0xAA, 0x71, 0x10, 0x44, 0x08, 0x44, 0x10]) + b"P0",  # declared length overruns
        bytes([0xAA, 0x71, 0x10, 0x44, 0x08, 0x44, 0x02, 0xFF, 0xFE]),  # non-ASCII payload
    ],
    ids=["empty", "str", "no-marker", "marker-at-end", "short", "overrun", "non-ascii"],
)
async def test_unparseable_control_data_is_ignored(w100, data):
    before = w100.thermostat.pmtsd_state()
    w100.manu._parse_w100_data(data)
    await w100.drain()
    assert w100.writes == []
    assert w100.thermostat.pmtsd_state() == before


# --- setpoint change tracking ----------------------------------------------------------

ZCL_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
SOURCE = Thermostat.SetpointChangeSource


def _change_time(t):
    return ZCL_EPOCH + timedelta(seconds=t.get(quirk.SETPOINT_CHANGE_TIMESTAMP_ATTR))


def test_setpoint_change_untracked_until_first_change(w100):
    assert w100.thermostat.get(quirk.SETPOINT_CHANGE_SOURCE_ATTR) is None
    assert w100.thermostat.get(quirk.SETPOINT_CHANGE_TIMESTAMP_ATTR) is None


async def test_setpoint_change_on_device_is_manual(w100):
    before = datetime.now(UTC).replace(microsecond=0)
    w100.receive(pmtsd_frame("P0_M1_T23"))
    await w100.drain()
    t = w100.thermostat
    assert t.get(quirk.SETPOINT_CHANGE_SOURCE_ATTR) == SOURCE.Manual
    assert before <= _change_time(t) <= datetime.now(UTC)


async def test_setpoint_change_from_ha_is_external(w100):
    before = datetime.now(UTC).replace(microsecond=0)
    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2400})
    t = w100.thermostat
    assert t.get(quirk.SETPOINT_CHANGE_SOURCE_ATTR) == SOURCE.External
    assert before <= _change_time(t) <= datetime.now(UTC)


async def test_unchanged_setpoint_does_not_update_tracking(w100):
    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2400})
    w100.thermostat._attr_cache.set_value(
        w100.thermostat.find_attribute(quirk.SETPOINT_CHANGE_TIMESTAMP_ATTR), 1
    )
    # The device reports the same setpoint back: not a new change.
    w100.receive(pmtsd_frame("P0_M1_T24"))
    await w100.drain()
    t = w100.thermostat
    assert t.get(quirk.SETPOINT_CHANGE_SOURCE_ATTR) == SOURCE.External
    assert t.get(quirk.SETPOINT_CHANGE_TIMESTAMP_ATTR) == 1


async def test_zha_formats_tracking_timestamp(w100):
    """ZHA's sensor shows ZCL_EPOCH + seconds; check it lands at 'now'."""
    from zha.application.platforms.sensor import SetpointChangeSourceTimestamp

    await w100.thermostat.write_attributes({"occupied_heating_setpoint": 2400})
    raw = w100.thermostat.get(quirk.SETPOINT_CHANGE_TIMESTAMP_ATTR)
    shown = SetpointChangeSourceTimestamp.formatter(None, raw)
    assert abs((datetime.now(UTC) - shown).total_seconds()) < 5
