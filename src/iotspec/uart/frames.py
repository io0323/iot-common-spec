"""UART フレームのデータクラスと pack/unpack（第7章「フレーム構造」「種別と対応」）。

    [種別 u8][seq u16][長さ u8][ペイロード 0–200B][CRC-16/CCITT-FALSE u16]

をリトルエンディアンで並べ、COBS で符号化して末尾に 0x00 を付ける。
CRC は種別からペイロード末尾まで。

受信側の decode_frame は例外を投げず、壊れたフレームを Discarded（理由つき）で返す。
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import ClassVar, Final, Literal

from iotspec.constants import (
    UART_CRC_SIZE,
    UART_DIAG_PAYLOAD_SIZE,
    UART_FRAME_DELIMITER,
    UART_HEADER_SIZE,
    UART_OP_CLEAR_OVERRIDE,
    UART_OP_GET_DIAG,
    UART_OP_GET_STATUS,
    UART_OP_SET_FAN_OVERRIDE,
    UART_OP_SET_THRESHOLD,
    UART_PAYLOAD_MAX,
    UART_TYPE_ACK,
    UART_TYPE_CMD,
    UART_TYPE_DIAG,
    UART_TYPE_EVENT,
    UART_TYPE_HEARTBEAT,
    UART_TYPE_STATE,
    UART_TYPE_TELEMETRY,
)
from iotspec.uart import cobs
from iotspec.uart.crc import crc16_ccitt_false

__all__ = [
    "CMD_ARGS_FORMAT",
    "CMD_ARG_NAMES",
    "Ack",
    "Cmd",
    "Diag",
    "DiscardReason",
    "Discarded",
    "Event",
    "Frame",
    "FrameReader",
    "Heartbeat",
    "State",
    "Telemetry",
    "decode_frame",
    "encode_frame",
]

_HEADER = struct.Struct("<BHB")
_CRC = struct.Struct("<H")

DiscardReason = Literal[
    "cobs_invalid",
    "too_short",
    "crc_mismatch",
    "length_mismatch",
    "unknown_type",
    "payload_size",
    "args_size",
]

CMD_ARGS_FORMAT: Final[dict[int, str]] = {
    UART_OP_SET_FAN_OVERRIDE: "<H",  # duty_pm:u16
    UART_OP_CLEAR_OVERRIDE: "",
    UART_OP_SET_THRESHOLD: "<hhh",  # warn_c100:i16, crit_c100:i16, hyst_c100:i16
    UART_OP_GET_STATUS: "",
    UART_OP_GET_DIAG: "",
}
"""CMD の args の形（v0.2.1、#20）。表にない op の args は解釈せずバイト列のまま持つ。"""

CMD_ARG_NAMES: Final[dict[int, tuple[str, ...]]] = {
    UART_OP_SET_FAN_OVERRIDE: ("duty_pm",),
    UART_OP_CLEAR_OVERRIDE: (),
    UART_OP_SET_THRESHOLD: ("warn_c100", "crit_c100", "hyst_c100"),
    UART_OP_GET_STATUS: (),
    UART_OP_GET_DIAG: (),
}
"""CMD の args の名前（CMD_ARGS_FORMAT と同じ順）。"""


@dataclass(frozen=True)
class Telemetry:
    TYPE: ClassVar[int] = UART_TYPE_TELEMETRY
    FORMAT: ClassVar[str] = "<IhBBH"
    seq: int
    uptime_ms: int
    temp_c100: int
    quality: int
    state: int
    fan_pm: int

    def _fields(self) -> tuple[int, ...]:
        return (self.uptime_ms, self.temp_c100, self.quality, self.state, self.fan_pm)


@dataclass(frozen=True)
class Event:
    TYPE: ClassVar[int] = UART_TYPE_EVENT
    FORMAT: ClassVar[str] = "<IBBBBh"
    seq: int
    uptime_ms: int
    code: int
    severity: int
    from_state: int
    to_state: int
    temp_c100: int

    def _fields(self) -> tuple[int, ...]:
        return (
            self.uptime_ms,
            self.code,
            self.severity,
            self.from_state,
            self.to_state,
            self.temp_c100,
        )


@dataclass(frozen=True)
class Heartbeat:
    TYPE: ClassVar[int] = UART_TYPE_HEARTBEAT
    FORMAT: ClassVar[str] = "<I"
    seq: int
    uptime_ms: int

    def _fields(self) -> tuple[int, ...]:
        return (self.uptime_ms,)


@dataclass(frozen=True)
class State:
    TYPE: ClassVar[int] = UART_TYPE_STATE
    FORMAT: ClassVar[str] = "<HIH"
    seq: int
    fw_version: int
    boot_id: int
    period_ms: int

    def _fields(self) -> tuple[int, ...]:
        return (self.fw_version, self.boot_id, self.period_ms)


@dataclass(frozen=True)
class Diag:
    """DIAG（01→03、v0.2.2 #37）。GET_DIAG に DIAG → ACK(done) の順で返す。

    rx_frame_errors は COBS・長さ・種別・受信あふれの合計。u16 の項目はすべて 0xFFFF で飽和する。
    """

    TYPE: ClassVar[int] = UART_TYPE_DIAG
    FORMAT: ClassVar[str] = "<IBHHHHH"
    seq: int
    uptime_ms: int
    reset_reason: int
    reset_count: int
    rx_crc_errors: int
    rx_frame_errors: int
    max_loop_us: int
    overruns: int

    def _fields(self) -> tuple[int, ...]:
        return (
            self.uptime_ms,
            self.reset_reason,
            self.reset_count,
            self.rx_crc_errors,
            self.rx_frame_errors,
            self.max_loop_us,
            self.overruns,
        )


assert struct.calcsize(Diag.FORMAT) == UART_DIAG_PAYLOAD_SIZE


@dataclass(frozen=True)
class Ack:
    TYPE: ClassVar[int] = UART_TYPE_ACK
    FORMAT: ClassVar[str] = "<HBBBH"
    seq: int
    cmd_ref: int
    status: int
    reason: int
    limited: int
    applied: int

    def _fields(self) -> tuple[int, ...]:
        return (self.cmd_ref, self.status, self.reason, self.limited, self.applied)


@dataclass(frozen=True)
class Cmd:
    """CMD。args は op ごとの値のタプル（CMD_ARGS_FORMAT）。表にない op は raw_args に生のまま。"""

    TYPE: ClassVar[int] = UART_TYPE_CMD
    HEAD_FORMAT: ClassVar[str] = "<HBH"  # cmd_ref:u16, op:u8, ttl_s:u16
    seq: int
    cmd_ref: int
    op: int
    ttl_s: int
    args: tuple[int, ...] = ()
    raw_args: bytes = field(default=b"")

    def args_bytes(self) -> bytes:
        fmt = CMD_ARGS_FORMAT.get(self.op)
        if fmt is None:
            if self.args:
                raise ValueError(f"op 0x{self.op:02x} の args の形は未定義（raw_args を使う）")
            return self.raw_args
        if self.raw_args:
            raise ValueError("args の形が決まっている op に raw_args は使えない")
        return struct.pack(fmt, *self.args) if fmt else b""


Frame = Telemetry | Event | Heartbeat | State | Diag | Ack | Cmd

_FIXED: Final[dict[int, type[Telemetry | Event | Heartbeat | State | Diag | Ack]]] = {
    cls.TYPE: cls for cls in (Telemetry, Event, Heartbeat, State, Diag, Ack)
}


@dataclass(frozen=True)
class Discarded:
    """破棄したフレーム。reason は理由、detail は補足、raw は受け取ったバイト列（区切りなし）。"""

    reason: DiscardReason
    detail: str
    raw: bytes


def _payload(frame: Frame) -> bytes:
    if isinstance(frame, Cmd):
        head = struct.pack(Cmd.HEAD_FORMAT, frame.cmd_ref, frame.op, frame.ttl_s)
        return head + frame.args_bytes()
    return struct.pack(frame.FORMAT, *frame._fields())


def encode_frame(frame: Frame) -> bytes:
    """フレームを COBS で符号化し、末尾に 0x00 を付けたバイト列にする。

    ペイロードが UART_PAYLOAD_MAX を超える・値が型の範囲外なら ValueError（struct.error を含む）。
    """
    try:
        payload = _payload(frame)
        if len(payload) > UART_PAYLOAD_MAX:
            raise ValueError(f"ペイロード長 {len(payload)} が上限 {UART_PAYLOAD_MAX} を超える")
        body = _HEADER.pack(frame.TYPE, frame.seq, len(payload)) + payload
    except struct.error as exc:
        raise ValueError(f"フィールドの値が範囲外: {exc}") from exc
    raw = body + _CRC.pack(crc16_ccitt_false(body))
    return cobs.encode(raw) + bytes([UART_FRAME_DELIMITER])


def decode_frame(data: bytes) -> Frame | Discarded:
    """符号化済みのフレーム（末尾の 0x00 はあってもなくてもよい）を復号する。例外は投げない。"""
    encoded = data[:-1] if data and data[-1] == UART_FRAME_DELIMITER else data
    try:
        raw = cobs.decode(encoded)
    except cobs.CobsDecodeError as exc:
        return Discarded("cobs_invalid", str(exc), encoded)
    if len(raw) < UART_HEADER_SIZE + UART_CRC_SIZE:
        return Discarded("too_short", f"{len(raw)} バイト", raw)
    body, crc_bytes = raw[:-UART_CRC_SIZE], raw[-UART_CRC_SIZE:]
    (received_crc,) = _CRC.unpack(crc_bytes)
    computed = crc16_ccitt_false(body)
    if received_crc != computed:
        return Discarded("crc_mismatch", f"受信 0x{received_crc:04x} 計算 0x{computed:04x}", raw)
    frame_type, seq, length = _HEADER.unpack(body[:UART_HEADER_SIZE])
    payload = body[UART_HEADER_SIZE:]
    if length != len(payload) or length > UART_PAYLOAD_MAX:
        return Discarded("length_mismatch", f"長さ {length}、実際 {len(payload)}", raw)
    if frame_type == UART_TYPE_CMD:
        return _decode_cmd(seq, payload, raw)
    cls = _FIXED.get(frame_type)
    if cls is None:
        return Discarded("unknown_type", f"種別 0x{frame_type:02x}", raw)
    if len(payload) != struct.calcsize(cls.FORMAT):
        return Discarded("payload_size", f"{cls.__name__} に {len(payload)} バイト", raw)
    return cls(seq, *struct.unpack(cls.FORMAT, payload))


def _decode_cmd(seq: int, payload: bytes, raw: bytes) -> Cmd | Discarded:
    head_size = struct.calcsize(Cmd.HEAD_FORMAT)
    if len(payload) < head_size:
        return Discarded("payload_size", f"CMD に {len(payload)} バイト", raw)
    cmd_ref, op, ttl_s = struct.unpack(Cmd.HEAD_FORMAT, payload[:head_size])
    rest = payload[head_size:]
    fmt = CMD_ARGS_FORMAT.get(op)
    if fmt is None:
        return Cmd(seq, cmd_ref, op, ttl_s, raw_args=bytes(rest))
    if len(rest) != struct.calcsize(fmt):
        return Discarded("args_size", f"op 0x{op:02x} の args に {len(rest)} バイト", raw)
    args = struct.unpack(fmt, rest) if fmt else ()
    return Cmd(seq, cmd_ref, op, ttl_s, tuple(args))


class FrameReader:
    """バイト列を少しずつ受け取り、0x00 で区切ってフレームを取り出す（シリアルの受信用）。"""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Frame | Discarded]:
        """data を足し、そろったフレームを順に返す。空のフレーム（0x00 の連続）は読み飛ばす。"""
        self._buffer += data
        while True:
            index = self._buffer.find(UART_FRAME_DELIMITER)
            if index < 0:
                return
            chunk = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            if chunk:
                yield decode_frame(chunk)
