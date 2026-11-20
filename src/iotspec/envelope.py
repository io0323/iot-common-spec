"""共通エンベロープの組み立てと解析（第3章「共通エンベロープ」）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from iotspec.clock import Clock, format_ts, parse_ts
from iotspec.constants import KINDS, SCHEMA_NAMES, UINT32_MAX
from iotspec.ids import new_boot_id, new_ulid
from iotspec.schema import validate

__all__ = [
    "Envelope",
    "EnvelopeBuilder",
    "InvalidMessageError",
    "SeqCounter",
    "TsSource",
    "kind_of_schema",
]

TsSource = Literal["device", "gateway"]

_STANDARD_FIELDS = frozenset(
    {"schema", "msg_id", "device_id", "seq", "boot_id", "ts", "ts_source", "payload", "uptime_ms"}
)
_KIND_BY_SCHEMA = {schema: kind for kind, schema in SCHEMA_NAMES.items()}


class InvalidMessageError(ValueError):
    """Schema に適合しない電文。errors に "JSON Pointer: 理由" のリストを持つ。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def kind_of_schema(schema: str) -> str:
    """'iot.v1.cmd_ack' → 'cmd/ack'（トピックの kind）。未知なら KeyError。"""
    return _KIND_BY_SCHEMA[schema]


@dataclass(frozen=True)
class Envelope:
    """共通エンベロープ。extra には未知の項目（前方互換）をそのまま保持する。"""

    schema: str
    msg_id: str
    device_id: str
    seq: int
    boot_id: str
    ts: datetime
    ts_source: TsSource
    payload: dict[str, Any]
    uptime_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """トピックの kind（例：'cmd/ack'）。"""
        return kind_of_schema(self.schema)

    def to_dict(self) -> dict[str, Any]:
        """電文の JSON オブジェクトにする。"""
        message: dict[str, Any] = {
            "schema": self.schema,
            "msg_id": self.msg_id,
            "device_id": self.device_id,
            "seq": self.seq,
            "boot_id": self.boot_id,
            "ts": format_ts(self.ts),
            "ts_source": self.ts_source,
        }
        if self.uptime_ms is not None:
            message["uptime_ms"] = self.uptime_ms
        message.update(self.extra)
        message["payload"] = self.payload
        return message

    @classmethod
    def from_dict(cls, message: Mapping[str, Any]) -> Envelope:
        """電文を Schema で検証してから解析する。不適合なら InvalidMessageError。"""
        errors = validate(message)
        if errors:
            raise InvalidMessageError(errors)
        return cls(
            schema=message["schema"],
            msg_id=message["msg_id"],
            device_id=message["device_id"],
            seq=message["seq"],
            boot_id=message["boot_id"],
            ts=parse_ts(message["ts"]),
            ts_source=message["ts_source"],
            payload=dict(message["payload"]),
            uptime_ms=message.get("uptime_ms"),
            extra={k: v for k, v in message.items() if k not in _STANDARD_FIELDS},
        )


class SeqCounter:
    """1つの発信元の、kind ごとの連番（uint32。一周したら0に戻る）と boot_id を持つ。

    起動ごとに作り直す想定で、連番は0から始まる（第3章「seq」）。
    """

    def __init__(self, boot_id: str | None = None) -> None:
        self.boot_id: str = boot_id if boot_id is not None else new_boot_id()
        self._next: dict[str, int] = {}

    def next(self, kind: str) -> int:
        """kind の次の連番を返す。kind はトピックの kind（constants.KINDS）。"""
        if kind not in KINDS:
            raise ValueError(f"unknown kind: {kind!r}")
        value = self._next.get(kind, 0)
        self._next[kind] = 0 if value == UINT32_MAX else value + 1
        return value

    def peek(self, kind: str) -> int:
        """次に返す連番（進めない）。"""
        return self._next.get(kind, 0)


class EnvelopeBuilder:
    """1つの発信元の電文を組み立てる。時刻は Clock から取り、連番は SeqCounter で振る。"""

    def __init__(self, device_id: str, clock: Clock, counter: SeqCounter | None = None) -> None:
        self.device_id = device_id
        self.clock = clock
        self.counter = counter if counter is not None else SeqCounter()

    @property
    def boot_id(self) -> str:
        return self.counter.boot_id

    def build(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        ts: datetime | None = None,
        ts_source: TsSource = "device",
        uptime_ms: int | None = None,
    ) -> Envelope:
        """kind の電文を組み立てる。ts 省略時は clock.now()。"""
        if kind not in KINDS:
            raise ValueError(f"unknown kind: {kind!r}")
        return Envelope(
            schema=SCHEMA_NAMES[kind],
            msg_id=new_ulid(self.clock),
            device_id=self.device_id,
            seq=self.counter.next(kind),
            boot_id=self.counter.boot_id,
            ts=ts if ts is not None else self.clock.now(),
            ts_source=ts_source,
            payload=dict(payload),
            uptime_ms=uptime_ms,
        )
