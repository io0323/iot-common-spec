"""作品03（gw-01）の簡易版。2つの役割を持つ。

- MQTT 側：app-01 の request を受け、シナリオで指定した結果（accepted → done、rejected＋理由、
  無応答など）を request/ack で返す。同じ request_id の再送には現在の状態を返す（第4章、1時間記憶）
- UART 上位側：作品01（またはその代役）に1秒ごとの HEARTBEAT と CMD を送り、ACK を検証する。
  指令は毎秒1件・未完了4件まで、ACK が2秒来なければ同じ cmd_ref で最大3回再送する（第4章）。
  受けたフレームは UartBridge で mcu-c-01 名義の電文に変換して発行する

通信は持たない。MQTT へ送る電文は outbox、UART へ送るバイト列は uart_tx に積む。
時刻は Clock から取る。
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from iotspec.clock import Clock
from iotspec.constants import (
    CMD_REF_MODULO,
    DEVICE_APP_01,
    KIND_CMD_ACK,
    KIND_EVENT,
    KIND_REQUEST,
    KIND_REQUEST_ACK,
    KIND_STATE,
    QOS_BY_KIND,
    REQUEST_DEDUP_TTL_S,
    RETAIN_KINDS,
    SCHEMA_NAMES,
    TOPIC_FORMAT,
    UART_ACK_TIMEOUT_S,
    UART_CMD_MAX_IN_FLIGHT,
    UART_CMD_MAX_RETRIES,
    UART_CMD_RATE_PER_S,
    UART_FRAME_DELIMITER,
    UART_HEARTBEAT_LOSS_S,
)
from iotspec.envelope import Envelope, EnvelopeBuilder, SeqCounter
from iotspec.ids import new_ulid
from iotspec.profile import Profile
from iotspec.schema import validate
from iotspec.sim.devices import Outgoing
from iotspec.sim.scenario import Fault
from iotspec.uart.bridge import CommandRejected, UartBridge
from iotspec.uart.frames import encode_frame

__all__ = ["CmdRecord", "Gw01Lite", "RequestRecord"]

DEFAULT_DONE_AFTER_S: Final = 1.0
FW_VERSION: Final = "sim-gw-0.2"


@dataclass
class CmdRecord:
    """作品01への指令1件の経過。"""

    cmd_id: str
    payload: dict[str, Any]
    cmd_ref: int | None = None
    attempts: int = 0
    sent_at: datetime | None = None
    ack: dict[str, Any] | None = None
    timed_out: bool = False
    repeat: int = 0


@dataclass
class RequestRecord:
    """受けた request 1件の状態。"""

    request_id: str
    result: str
    reason: str | None
    status: str | None = None
    cmd_id: str | None = None
    done_at: datetime | None = None
    received_at: datetime | None = None
    responses: list[dict[str, Any]] = field(default_factory=list)


class Gw01Lite:
    def __init__(
        self,
        profile: Profile,
        clock: Clock,
        *,
        site: str,
        uart_profile: Profile | None = None,
        request_rules: Iterable[Mapping[str, Any]] = (),
        faults: Iterable[Fault] = (),
        boot_id: str | None = None,
    ) -> None:
        self.profile = profile
        self.device_id = profile.device_id
        self.clock = clock
        self.site = site
        self.builder = EnvelopeBuilder(self.device_id, clock, SeqCounter(boot_id))
        self.request_rules = [dict(r) for r in request_rules]
        self.faults = tuple(faults)
        self.outbox: list[Outgoing] = []
        self.uart_tx = bytearray()
        self.stats: Counter[str] = Counter()
        self.requests: dict[str, RequestRecord] = {}
        self.cmds: dict[str, CmdRecord] = {}
        self.bridge = UartBridge(uart_profile, clock) if uart_profile is not None else None
        self._uart_rx = bytearray()
        self._queue: deque[CmdRecord] = deque()
        self._in_flight: dict[int, CmdRecord] = {}
        self._next_cmd_ref = 0
        self._last_cmd_sent: datetime | None = None
        self._last_uart_rx: datetime | None = None
        self._uart_online: bool | None = None
        self._started = clock.now()

    # ------------------------------------------------------------ 共通

    @property
    def boot_id(self) -> str:
        return self.builder.boot_id

    def topic(self, kind: str, device_id: str | None = None) -> str:
        return TOPIC_FORMAT.format(site=self.site, device_id=device_id or self.device_id, kind=kind)

    def subscriptions(self) -> list[str]:
        """購読するトピック（app-01 の request）。"""
        return [self.topic(KIND_REQUEST, DEVICE_APP_01)]

    def will(self) -> Outgoing:
        return self._offline_state()

    def shutdown(self) -> Outgoing:
        return self._offline_state()

    def _offline_state(self) -> Outgoing:
        envelope = Envelope(
            schema=SCHEMA_NAMES[KIND_STATE],
            msg_id=new_ulid(self.clock),
            device_id=self.device_id,
            seq=self.builder.counter.peek(KIND_STATE),
            boot_id=self.boot_id,
            ts=self.clock.now(),
            ts_source="device",
            payload=self._state_payload("offline"),
        )
        return Outgoing(self.topic(KIND_STATE), envelope.to_dict(), QOS_BY_KIND[KIND_STATE], True)

    def _state_payload(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "mode": "normal",
            "channels": [],
            "actuators": [],
            "fw_version": FW_VERSION,
        }

    def _publish(
        self, kind: str, payload: Mapping[str, Any], topic_device: str | None = None
    ) -> None:
        envelope = self.builder.build(kind, payload)
        self.outbox.append(
            Outgoing(
                self.topic(kind, topic_device),
                envelope.to_dict(),
                QOS_BY_KIND[kind],
                kind in RETAIN_KINDS,
            )
        )

    def start(self) -> None:
        self._publish(KIND_STATE, self._state_payload("online"))

    def tick(self) -> None:
        """1秒ごと：request の最終結果、UART の HEARTBEAT・指令の送信と再送、作品01の生存確認。"""
        self._complete_requests()
        if self.bridge is not None:
            self._uart_tick()

    def drain(self) -> list[Outgoing]:
        items, self.outbox = self.outbox, []
        return items

    def drain_uart(self) -> bytes:
        data, self.uart_tx = bytes(self.uart_tx), bytearray()
        return data

    # ------------------------------------------------------------ MQTT 側：request

    def handle_message(self, topic: str, payload: bytes) -> None:
        if topic not in self.subscriptions():
            return
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.stats["invalid_request"] += 1
            return
        if validate(message) or message.get("schema") != SCHEMA_NAMES[KIND_REQUEST]:
            self.stats["invalid_request"] += 1
            return
        self._on_request(message["payload"])

    def _rule_for(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        args = request.get("args") or {}
        for rule in self.request_rules:
            if "op" in rule and rule["op"] != request["op"]:
                continue
            if "target" in rule and rule["target"] != request["target"]:
                continue
            if "actuator" in rule and rule["actuator"] != args.get("id"):
                continue
            return rule
        return {"result": "done"}

    def _on_request(self, request: Mapping[str, Any]) -> None:
        now = self.clock.now()
        self._purge_requests(now)
        request_id = str(request["request_id"])
        record = self.requests.get(request_id)
        if record is not None:
            self.stats["duplicate_request"] += 1
            if record.status is not None:
                self._respond(record)  # 重複には現在の状態を返す
            return
        rule = self._rule_for(request)
        record = RequestRecord(request_id, str(rule["result"]), rule.get("reason"), received_at=now)
        self.requests[request_id] = record
        if record.result == "no_response":
            return
        if record.result == "rejected":
            record.status = "rejected"
        else:
            record.status = "accepted"
            record.cmd_id = new_ulid(self.clock)
            delay = float(rule.get("done_after_s", DEFAULT_DONE_AFTER_S))
            record.done_at = now + timedelta(seconds=delay)
        self._respond(record)

    def _respond(self, record: RequestRecord) -> None:
        payload: dict[str, Any] = {"request_id": record.request_id, "status": record.status}
        if record.cmd_id is not None:
            payload["cmd_id"] = record.cmd_id
        payload["reason"] = record.reason if record.status in ("rejected", "failed") else None
        record.responses.append(payload)
        self._publish(KIND_REQUEST_ACK, payload, topic_device=DEVICE_APP_01)

    def _complete_requests(self) -> None:
        now = self.clock.now()
        for record in self.requests.values():
            if record.status == "accepted" and record.done_at is not None and now >= record.done_at:
                record.status = "failed" if record.result == "failed" else "done"
                self._respond(record)

    def _purge_requests(self, now: datetime) -> None:
        ttl = timedelta(seconds=REQUEST_DEDUP_TTL_S)
        for request_id in [
            k for k, r in self.requests.items() if r.received_at and now - r.received_at >= ttl
        ]:
            del self.requests[request_id]

    # ------------------------------------------------------------ UART 上位側

    def submit_uart(self, payload: Mapping[str, Any], *, repeat: int = 0) -> CmdRecord:
        """作品01への指令（cmd の payload）を待ち行列に入れる。"""
        cmd = dict(payload)
        cmd.setdefault("cmd_id", new_ulid(self.clock))
        cmd.setdefault("priority", "auto")
        record = CmdRecord(str(cmd["cmd_id"]), cmd, repeat=repeat)
        self.cmds[record.cmd_id] = record
        self._queue.append(record)
        return record

    def feed_uart(self, data: bytes) -> None:
        """作品01から受けたバイト列を処理する。変換した電文は mcu-c-01 名義で outbox に積む。"""
        assert self.bridge is not None
        self._last_uart_rx = self.clock.now()
        if self._uart_online is not True:
            if self._uart_online is False:
                self._publish(
                    KIND_EVENT,
                    {
                        "code": "DEVICE_ONLINE",
                        "severity": "info",
                        "detail": {"device_id": self.bridge.profile.device_id},
                    },
                )
            self._uart_online = True
        self._uart_rx += data
        while (index := self._uart_rx.find(UART_FRAME_DELIMITER)) >= 0:
            chunk = bytes(self._uart_rx[: index + 1])
            del self._uart_rx[: index + 1]
            if len(chunk) > 1:
                self._on_uart_frame(chunk)

    def _on_uart_frame(self, chunk: bytes) -> None:
        assert self.bridge is not None
        result = self.bridge.receive_bytes(chunk)
        if result.discarded is not None:
            self.stats[f"uart_discarded:{result.discarded}"] += 1
        if result.request_state:
            self.submit_uart({"op": "get_status"})
        for envelope in result.envelopes:
            message = envelope.to_dict()
            self.outbox.append(
                Outgoing(
                    self.topic(envelope.kind, envelope.device_id),
                    message,
                    QOS_BY_KIND[envelope.kind],
                    envelope.kind in RETAIN_KINDS,
                )
            )
            if envelope.kind == KIND_CMD_ACK:
                self._on_ack(envelope.payload)

    def _on_ack(self, ack: Mapping[str, Any]) -> None:
        record = self.cmds.get(str(ack["cmd_id"]))
        if record is None:
            self.stats["unknown_ack"] += 1
            return
        if record.ack is not None:
            self.stats["duplicate_ack"] += 1
            if record.ack != dict(ack):
                self.stats["ack_mismatch"] += 1
        record.ack = dict(ack)
        if record.cmd_ref is not None:
            self._in_flight.pop(record.cmd_ref, None)

    def _uart_heartbeat_silenced(self, now: datetime) -> bool:
        return any(f.type == "heartbeat_silence" and f.active(now) for f in self.faults)

    def _uart_tick(self) -> None:
        assert self.bridge is not None
        now = self.clock.now()
        if not self._uart_heartbeat_silenced(now):
            uptime_ms = int((now - self._started) / timedelta(milliseconds=1)) & 0xFFFFFFFF
            self.uart_tx += encode_frame(self.bridge.heartbeat_frame(uptime_ms))
        # 作品01の生存確認（3秒途絶えたら切断とみなす）
        last = self._last_uart_rx
        if (
            self._uart_online
            and last is not None
            and (now - last).total_seconds() >= UART_HEARTBEAT_LOSS_S
        ):
            self._uart_online = False
            self._publish(
                KIND_EVENT,
                {
                    "code": "DEVICE_OFFLINE",
                    "severity": "warning",
                    "detail": {"device_id": self.bridge.profile.device_id},
                },
            )
        self._send_uart_cmds(now)

    def _cmd_slot_free(self, now: datetime) -> bool:
        if self._last_cmd_sent is None:
            return True
        return (now - self._last_cmd_sent).total_seconds() >= 1 / UART_CMD_RATE_PER_S

    def _send_uart_cmds(self, now: datetime) -> None:
        # 再送（ACK が UART_ACK_TIMEOUT_S 来ない指令）を先に扱う
        for record in list(self._in_flight.values()):
            assert record.sent_at is not None and record.cmd_ref is not None
            if (now - record.sent_at).total_seconds() < UART_ACK_TIMEOUT_S:
                continue
            if record.attempts > UART_CMD_MAX_RETRIES:
                record.timed_out = True
                del self._in_flight[record.cmd_ref]
                self.stats["uart_cmd_timeout"] += 1
                continue
            if self._cmd_slot_free(now):
                self._transmit(record, now)
        # 新しい指令（毎秒1件・未完了 UART_CMD_MAX_IN_FLIGHT 件まで）
        while (
            self._queue
            and len(self._in_flight) < UART_CMD_MAX_IN_FLIGHT
            and self._cmd_slot_free(now)
        ):
            record = self._queue.popleft()
            record.cmd_ref = self._next_cmd_ref
            self._next_cmd_ref = (self._next_cmd_ref + 1) % CMD_REF_MODULO
            if not self._transmit(record, now):
                continue
            self._in_flight[record.cmd_ref] = record
            for _ in range(record.repeat):
                self._transmit(record, now, count_attempt=False)

    def _transmit(self, record: CmdRecord, now: datetime, *, count_attempt: bool = True) -> bool:
        assert self.bridge is not None and record.cmd_ref is not None
        frame = self.bridge.command_to_frame(record.payload, record.cmd_ref)
        if isinstance(frame, CommandRejected):
            record.ack = {"cmd_id": record.cmd_id, "status": "rejected", "reason": frame.reason}
            self.stats["uart_cmd_rejected"] += 1
            return False
        self.uart_tx += encode_frame(frame)
        if count_attempt:
            record.attempts += 1
            record.sent_at = now
            self._last_cmd_sent = now
        return True
