"""Fixtures for exercising the W100 quirk against real zigpy/ZHA, with the radio mocked."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
import zigpy.device
import zigpy.types as t
from zha.quirks import DEVICE_REGISTRY
from zigpy.zcl import foundation

import sensor_ht_agl001 as quirk

IEEE = t.EUI64.convert("54:ef:44:10:00:aa:bb:cc")

# Frames captured from a real W100 on 2026-11-12.
# A heartbeat/request: command byte 0x84 at index 3, counter 0x07, end marker at the end.
HEARTBEAT = bytes.fromhex("aa710f84074b06410f000054ef4480711a08000844")
HEARTBEAT_COUNTER = 0x07

ON_FRAME_PREFIX = bytes.fromhex("aa713244")
OFF_FRAME_PREFIX = bytes.fromhex("aa711c44")
ACK_COMMAND = 0x24


def header_checksum_ok(frame: bytes) -> bool:
    """Lumi header checksum: bytes 0-5 sum to 0 mod 256."""
    return sum(frame[:6]) & 0xFF == 0


def write_response(status: foundation.Status) -> foundation.WriteAttributesResponseSchema:
    record = foundation.WriteAttributesStatusRecord(status=status)
    if status != foundation.Status.SUCCESS:
        record.attrid = quirk.W100_ATTR
    return foundation.WriteAttributesResponseSchema(
        status_records=foundation.WriteAttributesResponse([record])
    )


def pmtsd_frame(payload: str, counter: int = 0x0A) -> bytes:
    """A device-originated PMTSD data frame, in the layout the real W100 uses.

    e.g. aa 72 17 84 0a 3f 01 41 17 00 00 54 ef 44 80 71 1a 08 00 08 44 06 "M1_T24" 00
    """
    data = payload.encode("ascii")
    return (
        bytes([0xAA, 0x72, 0x00, 0x84, counter, 0x00, 0x01, 0x41, 0x00, 0x00, 0x00])
        + bytes.fromhex("54ef4480711a08000844")
        + bytes([len(data)])
        + data
        + b"\x00"
    )


def describe(frame: bytes) -> str:
    """Summarise a written control frame as 'ACK', 'ON', 'OFF' or its PMTSD string."""
    if frame[3] == ACK_COMMAND:
        return "ACK"
    if frame.startswith(ON_FRAME_PREFIX):
        return "ON"
    if frame.startswith(OFF_FRAME_PREFIX):
        return "OFF"
    marker = frame.find(b"\x08\x44")
    if marker != -1:
        return frame[marker + 3 :].decode("ascii")
    return frame.hex()


@dataclass
class W100:
    """A quirked W100 with handles to its clusters and the frames written to it."""

    device: zigpy.device.Device
    manu: quirk.W100ManuSpecificCluster
    thermostat: quirk.W100ThermostatCluster
    fan: quirk.W100FanCluster
    writes: list[bytes] = field(default_factory=list)
    write_times: list[float] = field(default_factory=list)
    response: object = field(default_factory=lambda: write_response(foundation.Status.SUCCESS))

    @property
    def frames(self) -> list[str]:
        """Frames written, excluding acknowledgements (see `acks`)."""
        return [d for d in (describe(w) for w in self.writes) if d != "ACK"]

    @property
    def acks(self) -> list[int]:
        """Counters echoed by the acknowledgement frames written, in order."""
        return [w[4] for w in self.writes if w[3] == ACK_COMMAND]

    def receive(self, data: bytes) -> None:
        """Simulate the device reporting a value on the 0xFFF2 control attribute."""
        self.manu._update_attribute(quirk.W100_ATTR, data)

    async def drain(self) -> None:
        """Wait for any background tasks (heartbeat replies) to finish."""
        while self.manu._background_tasks:
            await asyncio.gather(*self.manu._background_tasks, return_exceptions=True)

    def restart(self, persisted: dict[tuple[int, int, int], object]) -> None:
        """Mimic zigpy's appdb startup: clear quirked caches, then restore persisted values.

        Keys are (endpoint_id, cluster_id, attribute_id).
        """
        for ep in self.device.non_zdo_endpoints:
            for cluster in [*ep.in_clusters.values(), *ep.out_clusters.values()]:
                cluster._attr_cache.clear()
        for (ep_id, cluster_id, attr_id), value in persisted.items():
            cluster = self.device.endpoints[ep_id].in_clusters[cluster_id]
            cluster._attr_cache.set_value(cluster.find_attribute(attr_id), value)


def build_w100() -> W100:
    raw = zigpy.device.Device(MagicMock(), IEEE, 0x1234)
    raw.manufacturer = "Aqara"
    raw.model = "lumi.sensor_ht.agl001"
    for ep_id in (1, 2, 3):
        ep = raw.add_endpoint(ep_id)
        ep.profile_id = 260
        ep.device_type = 0x0302
        ep.add_input_cluster(0x0012)  # MultistateInput (buttons)
    for cluster_id in (0x0000, 0x0001, 0x0402, 0x0405, 0xFCC0):
        raw.endpoints[1].add_input_cluster(cluster_id)

    device = DEVICE_REGISTRY.resolve(raw)
    assert device is not raw, "W100 quirk was not applied"

    w100 = W100(
        device=device,
        manu=device.endpoints[1].in_clusters[0xFCC0],
        thermostat=device.endpoints[1].in_clusters[0x0201],
        fan=device.endpoints[21].in_clusters[0x0202],
    )

    async def fake_write_attributes_raw(attributes, manufacturer_code=None, **kwargs):
        w100.writes.append(bytes(attributes[0].value.value))
        w100.write_times.append(asyncio.get_running_loop().time())
        return w100.response

    w100.manu.write_attributes_raw = fake_write_attributes_raw
    return w100


@pytest.fixture(autouse=True)
def no_settle_delay(monkeypatch):
    """Skip the registration settle delay unless a test opts back in."""
    monkeypatch.setattr(quirk, "REGISTRATION_SETTLE_DELAY", 0)


@pytest.fixture
def w100() -> W100:
    return build_w100()
