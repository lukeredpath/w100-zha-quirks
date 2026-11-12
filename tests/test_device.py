"""Buttons, battery, external sensor mode, the manufacturer cluster and the device."""
import logging
from unittest.mock import MagicMock

import pytest
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import OnOff, PowerConfiguration

import sensor_ht_agl001 as quirk
from conftest import write_response

FAIL = write_response(foundation.Status.FAILURE)


def _parent(cls):
    """The class super() resolves to from inside cls's own methods."""
    return cls.__mro__[1]


# --- quirk wiring ----------------------------------------------------------------------


def test_quirk_replaces_and_adds_clusters(w100):
    ep1 = w100.device.endpoints[1]
    assert isinstance(w100.device, quirk.XiaomiCustomDeviceV2)
    assert isinstance(ep1.in_clusters[0x0000], quirk.W100BasicCluster)
    assert isinstance(ep1.in_clusters[0x0001], quirk.XiaomiPowerConfigurationPercent)
    assert isinstance(ep1.in_clusters[0x0402], quirk.W100TemperatureMeasurement)
    assert isinstance(w100.manu, quirk.W100ManuSpecificCluster)
    assert isinstance(w100.thermostat, quirk.W100ThermostatCluster)
    assert isinstance(w100.fan, quirk.W100FanCluster)
    for ep_id in (1, 2, 3):
        assert isinstance(w100.device.endpoints[ep_id].in_clusters[0x0012], quirk.MultistateInputCluster)


# --- buttons ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ep_id", "button"), [(1, quirk.PLUS_BUTTON), (2, quirk.CENTER_BUTTON), (3, quirk.MINUS_BUTTON)]
)
@pytest.mark.parametrize(
    ("value", "command"),
    [
        (0, quirk.COMMAND_HOLD),
        (1, quirk.COMMAND_SINGLE),
        (2, quirk.COMMAND_DOUBLE),
        (255, quirk.COMMAND_RELEASE),
        (7, quirk.COMMAND_SINGLE),  # unknown press types fall back to single
    ],
)
def test_button_press_emits_zha_event(w100, ep_id, button, value, command):
    cluster = w100.device.endpoints[ep_id].in_clusters[0x0012]
    listener = MagicMock()
    cluster.add_listener(listener)

    cluster.update_attribute(quirk.STATUS_TYPE_ATTR, value)

    listener.zha_send_event.assert_called_once_with(
        command,
        {
            quirk.COMMAND: command,
            quirk.VALUE: value,
            quirk.ENDPOINT_ID: ep_id,
            "args": button,
        },
    )


def test_button_other_attribute_emits_nothing(w100):
    cluster = w100.device.endpoints[1].in_clusters[0x0012]
    listener = MagicMock()
    cluster.add_listener(listener)
    cluster.update_attribute(0x0051, False)  # out_of_service
    listener.zha_send_event.assert_not_called()


def test_button_on_unnamed_endpoint(w100):
    cluster = quirk.MultistateInputCluster(w100.device.endpoints[21], is_server=True)
    listener = MagicMock()
    cluster.add_listener(listener)
    cluster.update_attribute(quirk.STATUS_TYPE_ATTR, 1)
    assert listener.zha_send_event.call_args.args[1]["args"] == "unknown"


# --- battery (0x00F7 Xiaomi struct) ----------------------------------------------------


def _struct(*items):
    """Encode (tag, zcl_type, value) triples as a Xiaomi 0xF7 struct."""
    out = b""
    for tag, zcl_type, value in items:
        out += bytes([tag]) + foundation.TypeValue(type=zcl_type, value=value).serialize()
    return out


UINT8, UINT16 = 0x20, 0x21


def _power(w100):
    return w100.device.endpoints[1].in_clusters[PowerConfiguration.cluster_id]


def test_battery_prefers_w100_percent_key(w100):
    w100.manu._update_attribute(0x00F7, _struct((0x01, UINT16, t.uint16_t(3000)), (0x05, UINT16, t.uint16_t(40)), (0x66, UINT8, t.uint8_t(87))))
    power = _power(w100)
    assert power.get(PowerConfiguration.AttributeDefs.battery_voltage.id) == 30
    assert power.get(PowerConfiguration.AttributeDefs.battery_percentage_remaining.id) == 87 * 2


@pytest.mark.parametrize(("raw", "percent"), [(60, 60), (150, 75), (250, 100)])
def test_battery_legacy_percent_key(w100, raw, percent):
    w100.manu._update_attribute(0x00F7, _struct((0x05, UINT16, t.uint16_t(raw))))
    assert _power(w100).get(PowerConfiguration.AttributeDefs.battery_percentage_remaining.id) == percent * 2


def test_battery_voltage_only(w100):
    w100.manu._update_attribute(0x00F7, _struct((0x01, UINT16, t.uint16_t(2900))))
    power = _power(w100)
    assert power.get(PowerConfiguration.AttributeDefs.battery_voltage.id) == 29
    assert power.get(PowerConfiguration.AttributeDefs.battery_percentage_remaining.id) is None


def test_battery_ignores_non_integer_and_unknown_tags(w100):
    data = _struct((0x66, 0x42, t.CharacterString("x")), (0x03, UINT8, t.uint8_t(25)))
    w100.manu._update_battery_from_xiaomi_struct(data)
    assert _power(w100).get(PowerConfiguration.AttributeDefs.battery_percentage_remaining.id) is None


def test_battery_ignores_non_bytes(w100):
    w100.manu._update_battery_from_xiaomi_struct(42)
    assert _power(w100).get(PowerConfiguration.AttributeDefs.battery_percentage_remaining.id) is None


def test_battery_without_endpoint_1(w100):
    del w100.device.endpoints[1]
    w100.manu._update_battery_from_xiaomi_struct(_struct((0x66, UINT8, t.uint8_t(50))))


def test_battery_without_power_cluster(w100):
    del w100.device.endpoints[1].in_clusters[PowerConfiguration.cluster_id]
    w100.manu._update_battery_from_xiaomi_struct(_struct((0x66, UINT8, t.uint8_t(50))))


def test_battery_malformed_struct_is_logged(w100, caplog):
    with caplog.at_level(logging.WARNING):
        w100.manu._update_battery_from_xiaomi_struct(b"\x66\xff\x01")
    assert "Error parsing 0xF7" in caplog.text


# --- auto hide middle line -------------------------------------------------------------


@pytest.mark.parametrize(("value", "switch"), [(True, False), (False, True)])
def test_auto_hide_drives_onoff(w100, value, switch):
    on_off = MagicMock()
    w100.device.endpoints[1].in_clusters[OnOff.cluster_id] = on_off
    w100.manu._update_attribute(0x0173, value)
    on_off.update_attribute.assert_called_once_with(0x0000, switch)


def test_auto_hide_without_onoff_cluster(w100):
    w100.manu._update_attribute(0x0173, True)
    assert w100.manu.get(0x0173) is True


# --- external sensor mode --------------------------------------------------------------


@pytest.fixture
def parent_read(monkeypatch):
    calls = []

    async def read(self, attributes, allow_cache=False, only_cache=False, manufacturer=None, **kwargs):
        calls.append((attributes, manufacturer))
        if read.exc:
            raise read.exc
        return {"parent": True}, {}

    read.exc = None
    read.calls = calls
    monkeypatch.setattr(_parent(quirk.W100ManuSpecificCluster), "read_attributes", read)
    return read


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(quirk.asyncio, "sleep", no_sleep)


@pytest.mark.parametrize("value", [None, "abc", 0, 1, 2, 3, 4])
def test_normalize_sensor_mode(w100, value):
    expected = 2 if value in (2, 3) else 0
    assert w100.manu._normalize_sensor_mode(value) == expected


def test_lumi_header_integrity():
    header = quirk.W100ManuSpecificCluster._lumi_header(0x12, 10, 0x02)
    assert header[:5] == bytes([0xAA, 0x71, 13, 0x44, 0x12])
    assert (sum(header[:5]) + header[5]) & 0xFF == 0
    assert header[6:] == bytes([0x02, 0x41, 10])


async def test_switch_to_external_sensor(w100, parent_read):
    defs = quirk.W100ManuSpecificCluster.AttributeDefs
    w100.manu._update_attribute(defs.external_temperature.id, 21.5)
    w100.manu._update_attribute(defs.external_humidity.id, 45.0)
    result = await w100.manu.write_attributes({"sensor": quirk.W100ExternalSensorMode.external})

    assert len(w100.writes) == 4
    config1, config2, temp, humi = w100.writes
    assert config1[6] == 0x02 and config2[6] == 0x02  # action: register external sensor
    assert temp.endswith(t.Single(2150.0).serialize()[::-1])  # big-endian float of value*100
    assert humi.endswith(t.Single(4500.0).serialize()[::-1])
    assert w100.manu.get(quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id) == 2
    assert parent_read.calls[0][1] == 0x115F
    assert result == [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS, attrid=0x0172)]]


async def test_switch_to_internal_sensor(w100, parent_read):
    await w100.manu.write_attributes({quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id: 0})
    assert len(w100.writes) == 2
    assert all(frame[6] == 0x04 for frame in w100.writes)  # action: unregister
    assert w100.manu.get(quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id) == 0


async def test_sensor_mode_read_back_failure_is_ignored(w100, parent_read):
    parent_read.exc = RuntimeError("timeout")
    await w100.manu.write_attributes({"sensor": 0})
    assert w100.manu.get(quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id) == 0


@pytest.mark.parametrize(
    ("key", "kind", "tag"),
    [("external_temperature", "temperature", 0x01), ("external_humidity", "humidity", 0x02)],
)
async def test_external_measurement_sent_in_external_mode(w100, key, kind, tag):
    w100.manu._update_attribute(quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id, 2)
    attr_id = w100.manu.attributes_by_name[key].id
    result = await w100.manu.write_attributes({key: 23.4})

    (frame,) = w100.writes
    assert frame[9:17] == quirk.W100ManuSpecificCluster._FICTIVE_SENSOR_IEEE
    assert frame[17:21] == bytes([0x00, tag, 0x00, 0x55])
    assert w100.manu.get(attr_id) == pytest.approx(23.4)
    assert result == [[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS, attrid=attr_id)]]


@pytest.mark.parametrize("key", ["external_temperature", "external_humidity"])
async def test_external_measurement_cached_but_not_sent_in_internal_mode(w100, key):
    w100.manu._update_attribute(quirk.W100ManuSpecificCluster.AttributeDefs.sensor.id, 0)
    await w100.manu.write_attributes({w100.manu.attributes_by_name[key].id: 30})
    assert w100.writes == []
    cached = w100.manu._cached_external_temperature if key == "external_temperature" else w100.manu._cached_external_humidity
    assert cached == 30.0


async def test_external_measurement_cache_updated_even_if_send_fails(w100):
    w100.response = FAIL
    with pytest.raises(RuntimeError):
        await w100.manu.write_attributes({"external_temperature": 19})
    assert w100.manu.get(quirk.W100ManuSpecificCluster.AttributeDefs.external_temperature.id) == 19.0


async def test_external_measurement_unknown_kind(w100):
    with pytest.raises(ValueError):
        await w100.manu._send_external_measurement("pressure", 1)


def test_external_values_default_until_set(w100):
    assert w100.manu._cached_external_temperature == 20.0
    assert w100.manu._cached_external_humidity == 50.0


async def test_external_values_survive_restart(w100, parent_read):
    defs = quirk.W100ManuSpecificCluster.AttributeDefs
    w100.restart({
        (1, 0xFCC0, defs.sensor.id): 0,
        (1, 0xFCC0, defs.external_temperature.id): 22.5,
        (1, 0xFCC0, defs.external_humidity.id): 41.0,
    })
    await w100.manu.write_attributes({"sensor": 2})
    temp, humi = w100.writes[2:]
    assert temp.endswith(t.Single(2250.0).serialize()[::-1])
    assert humi.endswith(t.Single(4100.0).serialize()[::-1])


# --- manufacturer cluster read/write passthrough ---------------------------------------


async def test_virtual_attributes_read_locally_keyed_as_requested(w100, parent_read):
    w100.manu._update_attribute(0x0175, 61.0)
    success, failure = await w100.manu.read_attributes(["external_temperature", 0x0175])
    assert success == {"external_temperature": 20.0, 0x0175: 61.0}
    assert failure == {}
    assert parent_read.calls == []


async def test_other_attributes_pass_through_with_manufacturer_code(w100, parent_read):
    success, _ = await w100.manu.read_attributes(["temp_period", 0xFFF2, "external_humidity"])
    assert parent_read.calls == [(["temp_period", 0xFFF2], 0x115F)]
    assert success == {"parent": True, "external_humidity": 50.0}


async def test_unknown_attribute_name_passes_through(w100, parent_read):
    await w100.manu.read_attributes(["no_such_attribute"], manufacturer=0x1234)
    assert parent_read.calls == [(["no_such_attribute"], 0x1234)]


async def test_other_writes_pass_through(w100, monkeypatch):
    calls = []

    async def parent_write(self, attributes, manufacturer=None):
        calls.append((attributes, manufacturer))
        return "parent"

    monkeypatch.setattr(_parent(quirk.W100ManuSpecificCluster), "write_attributes", parent_write)
    assert await w100.manu.write_attributes({"temp_period": 30}, manufacturer=0x115F) == "parent"
    assert calls == [({"temp_period": 30}, 0x115F)]


def test_update_attribute_logs_bytes_as_hex(w100, caplog):
    with caplog.at_level(logging.DEBUG, logger=quirk.__name__):
        w100.manu._update_attribute(0x00DF, b"\xde\xad")
    assert "hex=dead" in caplog.text


# --- Basic cluster ---------------------------------------------------------------------


@pytest.fixture
def basic(w100):
    return w100.device.endpoints[1].in_clusters[0x0000]


async def test_basic_read_handles_report_style_records(basic):
    async def raw(attributes, manufacturer=None):
        return [
            foundation.Attribute(attrid=0x0004, value=foundation.TypeValue(type=0x42, value=t.CharacterString("Aqara"))),
            foundation.ReadAttributeRecord(
                attrid=0x0005,
                status=foundation.Status.SUCCESS,
                value=foundation.TypeValue(type=0x42, value=t.CharacterString("lumi.sensor_ht.agl001")),
            ),
            foundation.ReadAttributeRecord(attrid=0x0006, status=foundation.Status.UNSUPPORTED_ATTRIBUTE),
        ]

    basic.read_attributes_raw = raw
    success, failure = await basic.read_attributes([0x0004, 0x0005, 0x0006])
    assert success == {0x0004: "Aqara", 0x0005: "lumi.sensor_ht.agl001"}
    assert failure == {0x0006: foundation.Status.UNSUPPORTED_ATTRIBUTE}


@pytest.fixture
def basic_parent_read(monkeypatch):
    calls = []

    async def read(self, attributes, allow_cache=False, only_cache=False, manufacturer=None, **kwargs):
        calls.append((attributes, only_cache))
        return "parent", {}

    monkeypatch.setattr(_parent(quirk.W100BasicCluster), "read_attributes", read)
    return calls


async def test_basic_read_only_cache_defers_to_parent(basic, basic_parent_read):
    assert await basic.read_attributes([0x0004], only_cache=True) == ("parent", {})
    assert basic_parent_read == [([0x0004], True)]


async def test_basic_read_falls_back_on_raw_failure(basic, basic_parent_read):
    async def raw(attributes, manufacturer=None):
        raise TimeoutError

    basic.read_attributes_raw = raw
    assert await basic.read_attributes([0x0004]) == ("parent", {})


# --- device ----------------------------------------------------------------------------


def _packet(cluster_id, src_ep=1):
    return t.ZigbeePacket(
        src=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x1234),
        src_ep=src_ep,
        dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x0000),
        dst_ep=1,
        profile_id=260,
        cluster_id=cluster_id,
        data=t.SerializableBytes(b""),
    )


@pytest.mark.parametrize("direction", [foundation.Direction.Client_to_Server, foundation.Direction.Server_to_Client])
def test_find_zcl_cluster_resolves_in_either_direction(w100, direction):
    hdr = foundation.ZCLHeader.general(1, foundation.GeneralCommand.Report_Attributes, direction=direction)
    assert w100.device._find_zcl_cluster(hdr, _packet(0xFCC0)) is w100.manu


@pytest.mark.parametrize("direction", [foundation.Direction.Client_to_Server, foundation.Direction.Server_to_Client])
def test_find_zcl_cluster_unknown_cluster_raises(w100, direction):
    hdr = foundation.ZCLHeader.general(1, foundation.GeneralCommand.Report_Attributes, direction=direction)
    with pytest.raises(KeyError):
        w100.device._find_zcl_cluster(hdr, _packet(0x0B04))
