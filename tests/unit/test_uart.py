"""Phase 3：UART コーデック・プロファイル・変換・ゴールデンのテスト。

独立した値での裏取り：
- CRC-16/CCITT-FALSE の検査値 "123456789" → 0x29B1、および標準ライブラリ binascii.crc_hqx との一致
- COBS は Wikipedia 掲載の既知ベクトル（0x00 を含む例、254 バイト境界の例）
"""

from __future__ import annotations

import binascii
import json
import struct
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from iotspec import constants as c
from iotspec.clock import VirtualClock, parse_ts
from iotspec.profile import ProfileError, load_profile, parse_profile
from iotspec.schema import validate
from iotspec.uart import cobs
from iotspec.uart.bridge import (
    CommandRejected,
    SeqExtender,
    UartBridge,
    format_boot_id,
    format_fw_version,
)
from iotspec.uart.crc import crc16_ccitt_false
from iotspec.uart.frames import (
    Ack,
    Cmd,
    Diag,
    Discarded,
    Event,
    Frame,
    FrameReader,
    Heartbeat,
    State,
    Telemetry,
    decode_frame,
    encode_frame,
)
from iotspec.uart.mapping import EVENT_CODE, QUALITY, REASON, SEVERITY, STATUS, UnknownValueError

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "uart" / "golden"
T0 = datetime(2026, 11, 20, 6, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------- CRC


def test_crc_check_value() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    assert crc16_ccitt_false(b"") == c.CRC16_INIT


@given(st.binary(max_size=300))
def test_crc_matches_stdlib_crc_hqx(data: bytes) -> None:
    """binascii.crc_hqx（多項式 0x1021、反転なし）に初期値 0xFFFF を与えたものと一致する。"""
    assert crc16_ccitt_false(data) == binascii.crc_hqx(data, 0xFFFF)


# ---------------------------------------------------------------- COBS

_R254 = bytes(range(1, 255))  # 01..FE
_R255 = bytes(range(1, 256))  # 01..FF
_S254 = bytes(range(2, 256))  # 02..FF
_S253 = bytes(range(3, 256))  # 03..FF

# Wikipedia「Consistent Overhead Byte Stuffing」の Encoding examples（区切りの 0x00 は除く）
COBS_VECTORS: list[tuple[str, bytes, bytes]] = [
    ("single_zero", b"\x00", b"\x01\x01"),
    ("two_zeros", b"\x00\x00", b"\x01\x01\x01"),
    ("zero_11_zero", b"\x00\x11\x00", b"\x01\x02\x11\x01"),
    ("11_22_00_33", b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
    ("no_zero", b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
    ("trailing_zeros", b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"),
    ("254_nonzero", _R254, b"\xff" + _R254),
    ("zero_then_254", b"\x00" + _R254, b"\x01\xff" + _R254),
    ("255_nonzero", _R255, b"\xff" + _R254 + b"\x02\xff"),
    ("254_then_zero", _S254 + b"\x00", b"\xff" + _S254 + b"\x01\x01"),
    ("253_zero_01", _S253 + b"\x00\x01", b"\xfe" + _S253 + b"\x02\x01"),
]


@pytest.mark.parametrize(
    ("name", "plain", "encoded"), COBS_VECTORS, ids=[v[0] for v in COBS_VECTORS]
)
def test_cobs_known_vectors(name: str, plain: bytes, encoded: bytes) -> None:
    assert cobs.encode(plain) == encoded
    assert cobs.decode(encoded) == plain


@given(st.binary(max_size=700))
def test_cobs_round_trip_without_zero(data: bytes) -> None:
    encoded = cobs.encode(data)
    assert 0 not in encoded
    assert cobs.decode(encoded) == data


@pytest.mark.parametrize("bad", [b"", b"\x00", b"\x05\x11\x22", b"\x03\x11\x00"])
def test_cobs_rejects_invalid(bad: bytes) -> None:
    with pytest.raises(cobs.CobsDecodeError):
        cobs.decode(bad)


# ---------------------------------------------------------------- frames

u8 = st.integers(0, 0xFF)
u16 = st.integers(0, 0xFFFF)
u32 = st.integers(0, c.UINT32_MAX)
i16 = st.integers(-0x8000, 0x7FFF)

KNOWN_OPS = [
    c.UART_OP_SET_FAN_OVERRIDE,
    c.UART_OP_CLEAR_OVERRIDE,
    c.UART_OP_SET_THRESHOLD,
    c.UART_OP_GET_STATUS,
    c.UART_OP_GET_DIAG,
]


@st.composite
def cmd_frames(draw: st.DrawFn) -> Cmd:
    op = draw(st.sampled_from([*KNOWN_OPS, 0x7F]))
    seq, ref, ttl = draw(u16), draw(u16), draw(u16)
    if op == c.UART_OP_SET_FAN_OVERRIDE:
        return Cmd(seq, ref, op, ttl, (draw(u16),))
    if op == c.UART_OP_SET_THRESHOLD:
        return Cmd(seq, ref, op, ttl, (draw(i16), draw(i16), draw(i16)))
    if op in KNOWN_OPS:
        return Cmd(seq, ref, op, ttl)
    head = struct.calcsize(Cmd.HEAD_FORMAT)
    return Cmd(seq, ref, op, ttl, raw_args=draw(st.binary(max_size=c.UART_PAYLOAD_MAX - head)))


frames = st.one_of(
    st.builds(Telemetry, u16, u32, i16, u8, u8, u16),
    st.builds(Event, u16, u32, u8, u8, u8, u8, i16),
    st.builds(Heartbeat, u16, u32),
    st.builds(State, u16, u16, u32, u16),
    st.builds(Diag, u16, u32, u8, u16, u16, u16, u16, u16),
    st.builds(Ack, u16, u16, u8, u8, u8, u16),
    cmd_frames(),
)


@given(frames)
def test_frame_round_trip(frame: Frame) -> None:
    data = encode_frame(frame)
    assert data[-1] == c.UART_FRAME_DELIMITER
    assert c.UART_FRAME_DELIMITER not in data[:-1]
    assert decode_frame(data) == frame


def test_payload_limit() -> None:
    head = struct.calcsize(Cmd.HEAD_FORMAT)
    room = c.UART_PAYLOAD_MAX - head
    assert isinstance(decode_frame(encode_frame(Cmd(0, 0, 0x7F, 0, raw_args=b"\x01" * room))), Cmd)
    with pytest.raises(ValueError):
        encode_frame(Cmd(0, 0, 0x7F, 0, raw_args=b"\x01" * (room + 1)))


def test_encode_rejects_out_of_range_field() -> None:
    with pytest.raises(ValueError):
        encode_frame(Heartbeat(c.UART_SEQ_MODULO, 0))
    with pytest.raises(ValueError):
        encode_frame(Cmd(0, 0, c.UART_OP_SET_FAN_OVERRIDE, 0, (1, 2)))


def _raw_frame(frame_type: int, seq: int, payload: bytes, length: int | None = None) -> bytes:
    body = struct.pack("<BHB", frame_type, seq, len(payload) if length is None else length)
    body += payload
    return cobs.encode(body + struct.pack("<H", crc16_ccitt_false(body))) + b"\x00"


def _corrupt_crc(frame: Frame) -> bytes:
    raw = bytearray(cobs.decode(encode_frame(frame)[:-1]))
    raw[-1] ^= 0x01
    return cobs.encode(bytes(raw)) + b"\x00"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (_corrupt_crc(Heartbeat(1, 2)), "crc_mismatch"),
        (b"\x05\x11\x22\x00", "cobs_invalid"),
        (b"\x00", "cobs_invalid"),
        (_raw_frame(c.UART_TYPE_HEARTBEAT, 1, b"\x01\x02\x03\x04", length=5), "length_mismatch"),
        (_raw_frame(0x7E, 1, b""), "unknown_type"),
        (_raw_frame(c.UART_TYPE_HEARTBEAT, 1, b"\x01\x02"), "payload_size"),
        (_raw_frame(c.UART_TYPE_DIAG, 1, b"\x00" * 14), "payload_size"),
        (_raw_frame(c.UART_TYPE_DIAG, 1, b"\x00" * 16), "payload_size"),
        (_raw_frame(c.UART_TYPE_DIAG, 1, b"\x00" * 15, length=16), "length_mismatch"),
        (_raw_frame(c.UART_TYPE_CMD, 1, b"\x01\x00\x01\x3c\x00"), "args_size"),
        (cobs.encode(b"\x03\x01") + b"\x00", "too_short"),
    ],
)
def test_corrupt_frames_are_discarded_with_reason(data: bytes, reason: str) -> None:
    result = decode_frame(data)
    assert isinstance(result, Discarded)
    assert result.reason == reason


def test_frame_reader_splits_stream() -> None:
    a, b = encode_frame(Heartbeat(1, 10)), encode_frame(State(2, 0x0102, 1, 1000))
    reader = FrameReader()
    stream = b"\x00" + a + b"\x00" + b[:3]
    first = list(reader.feed(stream))
    assert first == [Heartbeat(1, 10)]
    rest = list(reader.feed(b[3:] + b"\x05\x00"))
    assert rest[0] == State(2, 0x0102, 1, 1000)
    assert isinstance(rest[1], Discarded)


# ---------------------------------------------------------------- mapping


@pytest.mark.parametrize(
    "table", [QUALITY, STATUS, REASON, EVENT_CODE, SEVERITY], ids=lambda t: t.name
)
def test_mapping_round_trip(table: Any) -> None:
    for value in table.uart_values():
        assert table.to_uart(table.to_common(value)) == value
    with pytest.raises(UnknownValueError):
        table.to_common(250)
    with pytest.raises(UnknownValueError):
        table.to_uart("NOT_A_VALUE")


def test_mapping_values_match_spec() -> None:
    assert QUALITY.common_values() == ["good", "stale", "fault"]
    assert STATUS.common_values() == ["accepted", "done", "rejected", "failed"]
    assert REASON.common_values() == [
        None,
        "unsafe",
        "invalid_arg",
        "unknown_op",
        "expired",
        "internal",
    ]
    assert EVENT_CODE.to_common(7) == "UPSTREAM_LOST"
    assert SEVERITY.common_values() == ["info", "warning", "critical"]


def test_formatters() -> None:
    assert format_fw_version(0x0102) == "1.2"
    assert format_fw_version(0xFFFF) == "255.255"
    assert format_boot_id(0x7F3A9C01) == "7f3a9c01"
    assert format_boot_id(0) == "00000000"


# ---------------------------------------------------------------- profile


def test_bundled_profile_loads() -> None:
    profile = load_profile(c.DEVICE_MCU_C_01)
    assert profile.transport == "uart"
    assert profile.states[0] == c.UART_INITIAL_DEVICE_STATE
    assert profile.state_name(4) == "SENSOR_FAULT"
    spec = profile.find_command("set_actuator", "fan_main")
    assert spec is not None
    assert spec.uart_op == c.UART_OP_SET_FAN_OVERRIDE
    assert spec.ttl_from_lease
    assert profile.find_command("get_status", None) is not None
    assert profile.find_command("set_actuator", "pump") is None


def _good_profile() -> dict[str, Any]:
    return {
        "device_id": "mcu-c-02",
        "transport": "uart",
        "channels": [{"channel": "temp_board", "unit": "degC", "period_ms": 1000}],
        "actuators": [{"id": "fan_main", "min": 0, "max": 1000}],
        "states": {0: "INIT"},
        "commands": [
            {
                "op": "set_actuator",
                "target": "fan_main",
                "uart_op": "SET_FAN_OVERRIDE",
                "args": {"duty_pm": "level"},
                "ttl_s": "lease_s",
            }
        ],
    }


def _mutated(path: list[Any], value: Any) -> dict[str, Any]:
    data = _good_profile()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


_DELETE = object()


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        (_mutated(["states"], _DELETE), ""),
        (_mutated(["channels", 0, "unit"], "celsius"), "/channels/0/unit"),
        (_mutated(["transport"], "can"), "/transport"),
        (_mutated(["states", 0], "init"), "/states/0"),
        (_mutated(["states", 300], "BIG"), "/states"),
        (_mutated(["commands", 0, "uart_op"], "REBOOT"), "/commands/0/uart_op"),
        (_mutated(["commands", 0, "target"], "pump"), "/commands/0/target"),
        (_mutated(["commands", 0, "args"], {"level": "level"}), "/commands/0/args"),
        (_mutated(["extra"], 1), ""),
    ],
)
def test_invalid_profiles_are_rejected_with_reason(data: dict[str, Any], pointer: str) -> None:
    parse_profile(_good_profile())  # 元のプロファイルは適合する
    with pytest.raises(ProfileError) as info:
        parse_profile(data)
    assert any(e.startswith(f"{pointer}: ") for e in info.value.errors), info.value.errors


def test_profile_file_name_must_match_device_id(tmp_path: Path) -> None:
    path = tmp_path / "mcu-c-01.yaml"
    path.write_text("device_id: mcu-c-01\ntransport: mqtt\nchannels: []\nactuators: []\n")
    assert load_profile(path).device_id == "mcu-c-01"
    bad = tmp_path / "broken.yaml"
    bad.write_text("device_id: mcu-c-01\ntransport: uart\nchannels: []\nactuators: []\n")
    with pytest.raises(ProfileError):
        load_profile(bad)


# ---------------------------------------------------------------- SeqExtender


def test_seq_extender_wraps() -> None:
    ext = SeqExtender()
    assert ext.extend(c.UART_SEQ_MODULO - 1) == c.UART_SEQ_MODULO - 1
    assert ext.extend(0) == c.UART_SEQ_MODULO
    assert ext.extend(5) == c.UART_SEQ_MODULO + 5
    ext.reset()
    assert ext.extend(3) == 3


@given(st.integers(0, c.UART_SEQ_MODULO - 1), st.lists(st.integers(1, 1000), max_size=400))
def test_seq_extender_is_monotonic_across_wraps(start: int, steps: list[int]) -> None:
    """性質：UART の seq が（欠番を含めて）進む限り、広げた seq は単調に増える。

    増え方は UART の seq の進み（欠番を含む）と一致する。
    """
    ext = SeqExtender()
    seq16, previous = start, ext.extend(start)
    for step in steps:
        seq16 = (seq16 + step) % c.UART_SEQ_MODULO
        value = ext.extend(seq16)
        assert value - previous == step
        previous = value


# ---------------------------------------------------------------- bridge


def _bridge() -> tuple[UartBridge, VirtualClock]:
    clock = VirtualClock(T0)
    return UartBridge(load_profile(c.DEVICE_MCU_C_01), clock), clock


def _booted() -> tuple[UartBridge, VirtualClock]:
    bridge, clock = _bridge()
    result = bridge.receive(State(0, 0x0102, 0xA1B2C3D4, 1000))
    assert result.envelopes
    return bridge, clock


def test_bridge_state_is_completed_from_profile_and_valid() -> None:
    bridge, _ = _bridge()
    result = bridge.receive(State(0, 0x0203, 0xA1B2C3D4, 250))
    (env,) = result.envelopes
    message = env.to_dict()
    assert validate(message) == []
    assert message["boot_id"] == "a1b2c3d4"
    assert message["ts_source"] == "gateway"
    payload = message["payload"]
    assert payload["mode"] == c.UART_INITIAL_DEVICE_STATE
    assert payload["fw_version"] == "2.3"
    assert payload["channels"] == [{"channel": "temp_board", "unit": "degC", "period_ms": 250}]
    assert payload["actuators"] == [{"id": "fan_main", "min": 0, "max": 1000, "max_on_s": None}]


def test_bridge_ts_is_receive_time_and_uptime_is_kept() -> None:
    bridge, clock = _booted()
    clock.advance(1.5)
    (env,) = bridge.receive(Telemetry(1, 777, 2050, 0, 2, 300)).envelopes
    assert env.ts == clock.now()
    assert env.uptime_ms == 777
    assert env.payload["device_state"] == "WARNING"
    assert bridge.mode == "WARNING"


def test_bridge_reboot_detection_drops_telemetry_until_state() -> None:
    bridge, _ = _booted()
    assert bridge.receive(Telemetry(1, 5000, 2000, 0, 1, 0)).envelopes
    detected = bridge.receive(Telemetry(2, 100, 2000, 0, 1, 0))
    assert detected.discarded == "awaiting_state"
    assert detected.request_state is True
    again = bridge.receive(Telemetry(3, 1100, 2000, 0, 1, 0))
    assert again.discarded == "awaiting_state"
    assert again.request_state is False  # STATE を求めるのは1回
    assert bridge.receive(Heartbeat(4, 1200)).envelopes == ()
    assert bridge.dropped_telemetry == 2
    (state,) = bridge.receive(State(5, 0x0102, 0x00000002, 1000)).envelopes
    assert state.boot_id == "00000002"
    assert state.seq == 5  # 新しい起動として連番を振り直す
    assert state.payload["mode"] == c.UART_INITIAL_DEVICE_STATE
    (tel,) = bridge.receive(Telemetry(6, 1300, 2000, 0, 1, 0)).envelopes
    assert tel.boot_id == "00000002"
    assert bridge.dropped_telemetry == 2


def test_bridge_discards_event_and_ack_while_awaiting_state() -> None:
    bridge, _ = _bridge()
    assert bridge.receive(Event(0, 10, 1, 0, 0, 0, 2000)).discarded == "awaiting_state"
    assert bridge.receive(Ack(1, 7, 1, 0, 0, 0)).discarded == "awaiting_state"
    assert bridge.dropped_telemetry == 0
    assert bridge.discards["awaiting_state"] == 2


def test_bridge_unknown_values_are_discarded() -> None:
    bridge, _ = _booted()
    assert bridge.receive(Telemetry(1, 10, 2000, 9, 1, 0)).discarded == "unknown_value"
    assert bridge.receive(Telemetry(2, 20, 2000, 0, 99, 0)).discarded == "unknown_value"
    assert bridge.receive(Event(3, 30, 99, 0, 1, 1, 2000)).discarded == "unknown_value"
    assert bridge.receive(Ack(4, 0x4242, 1, 0, 0, 0)).discarded == "unknown_cmd_ref"
    assert bridge.receive(Telemetry(5, 40, 2000, 0, 1, 1001)).discarded == "schema_invalid"
    assert bridge.receive_bytes(b"\x05\x11\x00").discarded == "cobs_invalid"


def test_bridge_ack_applied_only_for_output_op() -> None:
    bridge, _ = _booted()
    set_cmd = {
        "cmd_id": "01JD3R0A5K7M2N4P6R8T0V2X4Y",
        "op": "set_actuator",
        "args": {"id": "fan_main", "level": 900},
    }
    rel_cmd = {"cmd_id": "01JD3R0B5K7M2N4P6R8T0V2X4Y", "op": "release", "args": {"id": "fan_main"}}
    assert isinstance(bridge.command_to_frame(set_cmd, 10), Cmd)
    assert isinstance(bridge.command_to_frame(rel_cmd, 11), Cmd)
    (a,) = bridge.receive(Ack(1, 10, 1, 0, 1, 700)).envelopes
    assert a.payload == {
        "cmd_id": set_cmd["cmd_id"],
        "status": "done",
        "limited": True,
        "reason": None,
        "applied": {"level": 700},
    }
    (b,) = bridge.receive(Ack(2, 11, 2, 1, 0, 0)).envelopes
    assert "applied" not in b.payload
    assert b.payload["reason"] == "unsafe"
    assert validate(a.to_dict()) == validate(b.to_dict()) == []


@given(st.builds(Diag, u16, u32, u8, u16, u16, u16, u16, u16))
def test_diag_round_trip(frame: Diag) -> None:
    """v0.2.2 #37：DIAG はペイロード15バイト（u32, u8, u16 ×5）。"""
    data = encode_frame(frame)
    raw = cobs.decode(data[:-1])
    assert raw[0] == c.UART_TYPE_DIAG == 0x05
    assert raw[3] == c.UART_DIAG_PAYLOAD_SIZE == 15
    assert decode_frame(data) == frame


def test_bridge_diag_is_not_converted_and_kept() -> None:
    bridge, clock = _booted()
    clock.advance(2)
    result = bridge.receive(Diag(0, 4321, 3, 2, 1, 0xFFFF, 900, 7))
    assert result.envelopes == ()
    assert result.discarded == "not_converted"
    assert result.diag == {
        "received_at": "2026-11-20T06:00:02.000Z",
        "boot_id": "a1b2c3d4",
        "uptime_ms": 4321,
        "reset_reason": 3,
        "reset_count": 2,
        "rx_crc_errors": 1,
        "rx_frame_errors": 0xFFFF,
        "max_loop_us": 900,
        "overruns": 7,
    }
    assert bridge.discards["not_converted"] == 1


def test_bridge_diag_while_awaiting_state_and_reboot_detection() -> None:
    bridge, _ = _bridge()
    first = bridge.receive(Diag(0, 100, 1, 0, 0, 0, 0, 0))
    assert (first.discarded, first.diag, first.request_state) == ("awaiting_state", None, True)
    bridge.receive(State(0, 0x0102, 0x1, 1000))
    assert bridge.receive(Diag(1, 5000, 1, 0, 0, 0, 0, 0)).discarded == "not_converted"
    # DIAG の uptime_ms の減少も再起動の検知に使う
    detected = bridge.receive(Diag(2, 10, 1, 0, 0, 0, 0, 0))
    assert (detected.discarded, detected.request_state) == ("awaiting_state", True)


def test_bridge_boot_detail_has_only_reset_reason() -> None:
    bridge, _ = _booted()
    for code_value, name in c.UART_RESET_REASONS.items():
        (env,) = bridge.receive(
            Event(code_value, 10 + code_value, 1, 0, code_value, 0, 2500)
        ).envelopes
        assert env.payload["code"] == "BOOT"
        assert env.payload["detail"] == {"reset_reason": name}
        assert validate(env.to_dict()) == []
    assert bridge.receive(Event(7, 100, 1, 0, 7, 0, 2500)).discarded == "unknown_value"
    assert bridge.receive(Event(8, 110, 1, 0, 1, 1, 2500)).discarded == "unknown_value"  # to ≠ 0


def test_bridge_interlock_events_keep_from_and_to_state() -> None:
    bridge, _ = _booted()
    (active,) = bridge.receive(Event(0, 10, 9, 1, 3, 3, 6100)).envelopes
    (released,) = bridge.receive(Event(1, 20, 10, 0, 1, 1, 3000)).envelopes
    assert (active.payload["code"], active.payload["severity"]) == ("INTERLOCK_ACTIVE", "warning")
    assert active.payload["detail"] == {"from_state": "CRITICAL", "to_state": "CRITICAL"}
    assert (released.payload["code"], released.payload["severity"]) == (
        "INTERLOCK_RELEASED",
        "info",
    )
    assert released.payload["detail"] == {"from_state": "NORMAL", "to_state": "NORMAL"}


def test_bridge_internal_get_diag_ack_is_not_converted() -> None:
    bridge, _ = _booted()
    frame = bridge.internal_frame(c.UART_OP_GET_DIAG, 0x0042)
    assert (frame.op, frame.ttl_s, frame.args) == (c.UART_OP_GET_DIAG, 0, ())
    result = bridge.receive(Ack(0, 0x0042, 1, 0, 0, 0))
    assert (result.envelopes, result.discarded) == ((), "not_converted")
    assert result.internal_ack == {
        "cmd_ref": 0x0042,
        "op": "GET_DIAG",
        "status": "done",
        "reason": None,
        "limited": 0,
        "applied": 0,
    }
    # 同じ cmd_ref を汎用指令に使い直したら、その ACK は cmd/ack にする
    cmd = {"cmd_id": "01JD3R0A5K7M2N4P6R8T0V2X4Y", "op": "get_status"}
    assert isinstance(bridge.command_to_frame(cmd, 0x0042), Cmd)
    (env,) = bridge.receive(Ack(1, 0x0042, 1, 0, 0, 0)).envelopes
    assert env.kind == c.KIND_CMD_ACK
    with pytest.raises(ValueError):
        bridge.internal_frame(c.UART_OP_SET_FAN_OVERRIDE, 1)
    with pytest.raises(ValueError):
        bridge.internal_frame(c.UART_OP_SET_THRESHOLD, 1, (1, 2))
    assert bridge.internal_frame(c.UART_OP_SET_THRESHOLD, 2, (4500, 6000, 300)).args == (
        4500,
        6000,
        300,
    )


def test_command_to_frame_maps_and_rejects() -> None:
    bridge, _ = _booted()
    base = {"cmd_id": "01JD3R0A5K7M2N4P6R8T0V2X4Y", "priority": "auto"}
    frame = bridge.command_to_frame(
        {**base, "op": "set_actuator", "args": {"id": "fan_main", "level": 1000}}, 1
    )
    assert isinstance(frame, Cmd)
    assert (frame.op, frame.ttl_s, frame.args) == (
        c.UART_OP_SET_FAN_OVERRIDE,
        c.LEASE_DEFAULT_S,
        (1000,),
    )
    status = bridge.command_to_frame({**base, "op": "get_status"}, 2)
    assert isinstance(status, Cmd)
    assert (status.op, status.ttl_s, status.args) == (c.UART_OP_GET_STATUS, 0, ())
    assert status.seq == frame.seq + 1
    assert bridge.heartbeat_frame(1).seq == 0  # HEARTBEAT は CMD と別の連番（#25）

    def reason(cmd: dict[str, Any]) -> str:
        result = bridge.command_to_frame({**base, **cmd}, 3)
        assert isinstance(result, CommandRejected)
        return result.reason

    assert reason({"op": "set_mode", "args": {"mode": "auto"}}) == "unknown_op"
    assert reason({"op": "set_actuator", "args": {"id": "pump", "level": 1}}) == "unknown_target"
    assert reason({"op": "set_actuator", "args": {"id": "fan_main"}}) == "invalid_arg"
    over = {
        "op": "set_actuator",
        "args": {"id": "fan_main", "level": 1},
        "lease_s": c.LEASE_MAX_S + 1,
    }
    assert reason(over) == "invalid_arg"
    with pytest.raises(ValueError):
        bridge.command_to_frame({**base, "op": "get_status"}, c.UART_SEQ_MODULO)


@given(
    st.lists(
        st.one_of(
            st.builds(
                Telemetry, u16, u32, i16, st.integers(0, 2), st.integers(0, 4), st.integers(0, 1000)
            ),
            st.builds(
                Event,
                u16,
                u32,
                st.integers(1, 7),
                st.integers(0, 2),
                st.integers(0, 4),
                st.integers(0, 4),
                i16,
            ),
            st.builds(Heartbeat, u16, u32),
            st.builds(State, u16, u16, u32, st.integers(1, 0xFFFF)),
        ),
        max_size=40,
    )
)
def test_bridge_output_always_conforms(frames_in: list[Frame]) -> None:
    """性質：どんな順でフレームが来ても、変換して出す電文はすべて Schema に適合する。"""
    bridge, clock = _bridge()
    for frame in frames_in:
        clock.advance(0.1)
        for env in bridge.receive(frame).envelopes:
            assert validate(env.to_dict()) == []


# ---------------------------------------------------------------- golden


def _goldens() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLDEN_DIR.glob("*.json"))]


def test_golden_regeneration_has_no_diff() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_golden.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_golden_counts_per_type() -> None:
    goldens = _goldens()
    by_type: dict[str, int] = {}
    for g in goldens:
        by_type[g["frame"]["type"]] = by_type.get(g["frame"]["type"], 0) + 1
    for name in ("TELEMETRY", "EVENT", "HEARTBEAT", "STATE", "CMD", "ACK"):
        assert by_type.get(name, 0) >= 2, name
    assert by_type.get("DIAG", 0) >= 1
    acks = [g["frame"]["limited"] for g in goldens if g["frame"]["type"] == "ACK"]
    assert {0, 1} <= set(acks)
    ops = {g["frame"]["op"] for g in goldens if g["frame"]["type"] == "CMD"}
    assert set(KNOWN_OPS) <= ops


@pytest.mark.parametrize("golden", _goldens(), ids=lambda g: g["name"])
def test_golden_hex_is_independently_consistent(golden: dict[str, Any]) -> None:
    """hex を COBS で戻し、CRC を binascii で、ヘッダと長さを struct で独立に確かめる。"""
    data = bytes.fromhex(golden["hex"])
    assert (
        GOLDEN_DIR / f"{golden['order']:02d}_{golden['name']}.hex"
    ).read_text().strip() == golden["hex"]
    assert data[-1] == 0
    raw = cobs.decode(data[:-1])
    body, crc = raw[:-2], int.from_bytes(raw[-2:], "little")
    frame_type, seq, length = struct.unpack("<BHB", body[:4])
    assert frame_type == golden["frame"]["type_code"]
    assert seq == golden["frame"]["seq"]
    assert length == len(body) - 4
    corrupt = golden.get("expected", {}).get("discarded") == "crc_mismatch"
    assert (crc == binascii.crc_hqx(body, 0xFFFF)) is not corrupt


def test_golden_session_replays_through_a_fresh_bridge() -> None:
    """作品03 と同じ手順：ゴールデンを順に新しいブリッジに通すと、期待どおりの結果になる。"""
    goldens = _goldens()
    clock = VirtualClock(T0)
    pending: list[str] = []
    bridge = UartBridge(
        load_profile(c.DEVICE_MCU_C_01),
        clock,
        new_id=lambda: pending.pop(),
        bridged_by=c.DEVICE_GW_01,
    )
    for g in goldens:
        frame = g["frame"]
        if g["direction"] == "03->01":
            if "input_cmd_payload" in g:
                built = bridge.command_to_frame(g["input_cmd_payload"], g["cmd_ref"])
                assert isinstance(built, Cmd)
                assert encode_frame(built).hex(" ") == g["hex"], g["name"]
            elif "internal_op" in g:
                op = {v: k for k, v in c.UART_OP_NAMES.items()}[g["internal_op"]]
                built = bridge.internal_frame(op, g["cmd_ref"])
                assert encode_frame(built).hex(" ") == g["hex"], g["name"]
            elif frame["type"] == "HEARTBEAT":
                assert encode_frame(bridge.heartbeat_frame(frame["uptime_ms"])).hex(" ") == g["hex"]
            else:
                assert bridge.next_tx_seq(Cmd.TYPE) == frame["seq"]
                decoded = decode_frame(bytes.fromhex(g["hex"]))
                assert isinstance(decoded, Cmd)
                assert list(decoded.args) == frame["args"]
            continue
        expected = g["expected"]
        clock.set(parse_ts(g["received_at"]))
        if expected["envelope"] is not None:
            pending.append(expected["envelope"]["msg_id"])
        result = bridge.receive_bytes(bytes.fromhex(g["hex"]))
        got = result.envelopes[0].to_dict() if result.envelopes else None
        assert got == expected["envelope"], g["name"]
        assert result.discarded == expected["discarded"], g["name"]
        assert result.request_state == expected["request_state"], g["name"]
        assert bridge.dropped_telemetry == expected["dropped_telemetry_total"], g["name"]
        assert result.diag == expected.get("diag"), g["name"]
        if got is not None:
            assert validate(got) == []


def test_per_type_seq_has_no_false_gaps_across_interleaved_frames() -> None:
    """#25：UART の seq は種別ごと。種別をまたいでフレームを挟んでも kind ごとの seq は飛ばない。"""
    bridge, _ = _booted()
    set_cmd = {"cmd_id": "01JD3R0A5K7M2N4P6R8T0V2X4Y", "op": "get_status"}
    assert isinstance(bridge.command_to_frame(set_cmd, 1), Cmd)
    telemetry_seqs: list[int] = []
    event_seqs: list[int] = []
    for i in range(1, 6):
        (t,) = bridge.receive(Telemetry(i - 1, i * 1000, 2000, 0, 1, 0)).envelopes
        telemetry_seqs.append(t.seq)
        assert bridge.receive(Heartbeat(i - 1, i * 1000 + 1)).envelopes == ()
        (e,) = bridge.receive(Event(i - 1, i * 1000 + 2, 2, 0, 1, 2, 2000)).envelopes
        event_seqs.append(e.seq)
        (a,) = bridge.receive(Ack(i - 1, 1, 1, 0, 0, 0)).envelopes
        assert a.seq == i - 1
    assert telemetry_seqs == event_seqs == [0, 1, 2, 3, 4]
    assert sum(bridge.uart_gaps.values()) == 0
    # 本当の欠番は種別ごとに数える
    bridge.receive(Telemetry(8, 9000, 2000, 0, 1, 0))
    assert bridge.uart_gaps == {"TELEMETRY": 3}
