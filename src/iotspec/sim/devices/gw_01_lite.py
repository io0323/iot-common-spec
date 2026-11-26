"""作品03（gw-01）の簡易版。2つの役割を持つ。

- MQTT 側：app-01 の request を受け、シナリオで指定した結果（accepted → done、rejected＋理由、
  無応答など）を request/ack で返す。同じ request_id の再送には現在の状態を返す（第4章、1時間記憶）
- UART 上位側：作品01（またはその代役）に1秒ごとの HEARTBEAT と CMD を送り、ACK を検証する。
  指令は毎秒1件・未完了4件まで、ACK が2秒来なければ同じ cmd_ref で最大3回再送する（第4章）。
  受けたフレームは UartBridge で mcu-c-01 名義の電文に変換して発行する。
  expires_at を過ぎた指令は UART に送らずに終え、mcu-c-01 名義の cmd/ack は作らない。要求元には
  request/ack（rejected、reason expired）で伝える（v0.2.2 #40）。
  GET_DIAG は内部の操作（submit_internal）で、DIAG は MQTT に変換せず diag_history に残し、
  その ACK も cmd/ack にしない（v0.2.2 #37）

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

from iotspec.clock import Clock, parse_ts
from iotspec.constants import (
    CMD_REF_MODULO,
    DEVICE_APP_01,
    KIND_CMD_ACK,
    KIND_EVENT,
    KIND_REQUEST,
    KIND_REQUEST_ACK,
    KIND_STATE,
    KIND_TELEMETRY,
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
from iotspec.uart.bridge import NOT_CONVERTED, CommandRejected, UartBridge
from iotspec.uart.frames import encode_frame

__all__ = ["CmdRecord", "Gw01Lite", "RequestRecord"]

DEFAULT_DONE_AFTER_S: Final = 1.0
FW_VERSION: Final = "sim-gw-0.2"
NORMAL_MODE: Final = "NORMAL"
"""EMERGENCY の解除の承認で戻すモード（v0.2.3 #46）。"""
_MODE_SEVERITY: Final = {"NORMAL": "info", "ALERT": "warning", "EMERGENCY": "critical"}
"""SYSTEM_MODE_CHANGED の重大度（遷移先に従う。代役の選択）。"""
QUALITY_COUNT_CHANNELS: Final = (
    "mqtt_rejected",
    "mqtt_duplicates",
    "mqtt_gaps",
    "mqtt_clock_skew",
    "mqtt_late",
    "stale_transitions",
    "uart_discarded",
    "queue_dropped",
)
"""品質の件数（v0.2.3 #45、unit count、60秒ごと）。"""
HOST_VALUES: Final[dict[str, float]] = {
    "cpu_temp": 45.0,
    "cpu_usage": 10.0,
    "mem_used": 30.0,
    "disk_used": 20.0,
}
"""自己監視（FR-06、10秒ごと）。簡易版は決まった値を送る（実機の値は作品03 が測る）。"""


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
    request_id: str | None = None
    """この指令を生んだ request（app-01 の要求を UART の指令に変えた場合）。"""
    internal_op: int | None = None
    """作品03の内部の操作（GET_DIAG など）。汎用指令との対応がなく、ACK は cmd/ack にしない。"""
    expired: bool = False
    """expires_at を過ぎていたので UART に送らずに終えた（#40）。"""


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
        initial_mode: str | None = None,
        emergency_condition_until: datetime | None = None,
    ) -> None:
        self.profile = profile
        self.system_mode: str = initial_mode or (profile.modes[0] if profile.modes else NORMAL_MODE)
        """システムのモード（NORMAL・ALERT・EMERGENCY。v0.2.3 #46）。"""
        self.emergency_condition_until = emergency_condition_until
        """この時刻まで EMERGENCY の条件が回復していない（承認は unsafe）。None は回復済み。"""
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
        self.diag_history: list[dict[str, Any]] = []
        """受けた DIAG（復号した項目、受信時刻、boot_id）。MQTT には出さない（#37）。"""
        self.bridge = (
            UartBridge(uart_profile, clock, bridged_by=self.device_id)
            if uart_profile is not None
            else None
        )
        self._uart_rx = bytearray()
        self._queue: deque[CmdRecord] = deque()
        self._in_flight: dict[int, CmdRecord] = {}
        self._next_cmd_ref = 0
        self._last_cmd_sent: datetime | None = None
        self._last_uart_rx: datetime | None = None
        self._uart_online: bool | None = None
        self._started = clock.now()
        self._own_telemetry_at: dict[str, datetime] = {}

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
            "mode": self.system_mode,
            "channels": [
                {"channel": c.channel, "unit": c.unit, "period_ms": c.period_ms}
                for c in self.profile.channels
            ],
            "actuators": [],
            "fw_version": FW_VERSION,
        }

    def _period(self, channels: Iterable[str]) -> timedelta | None:
        """チャネルの組の周期（プロファイルの period_ms）。無ければ None（発行しない）。"""
        periods = [c.period_ms for c in self.profile.channels if c.channel in set(channels)]
        return timedelta(milliseconds=min(periods)) if periods else None

    def _quality_counts(self) -> dict[str, int]:
        """品質の件数（簡易版の数え方：要求の不正・重複と、UART で捨てたフレーム）。"""
        uart_discarded = 0
        if self.bridge is not None:
            uart_discarded = sum(
                n for reason, n in self.bridge.discards.items() if reason != NOT_CONVERTED
            )
        counts = dict.fromkeys(QUALITY_COUNT_CHANNELS, 0)
        counts["mqtt_rejected"] = self.stats["invalid_request"]
        counts["mqtt_duplicates"] = self.stats["duplicate_request"]
        counts["uart_discarded"] = uart_discarded
        return counts

    def _publish_own_telemetry(self, now: datetime) -> None:
        """品質の件数と自己監視を、それぞれの周期で別々の telemetry で発行する（#45）。"""
        units = {c.channel: c.unit for c in self.profile.channels}
        groups: list[tuple[str, dict[str, float | int]]] = [
            ("counts", dict(self._quality_counts())),
            ("host", dict(HOST_VALUES)),
        ]
        for name, values in groups:
            period = self._period(values)
            last = self._own_telemetry_at.get(name, self._started)
            if period is None or now - last < period:
                continue
            self._own_telemetry_at[name] = now
            readings = [
                {"channel": ch, "value": value, "unit": units[ch], "quality": "good"}
                for ch, value in values.items()
                if ch in units
            ]
            self._publish(KIND_TELEMETRY, {"readings": readings})

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
        """1秒ごと：request の結果、自分の telemetry、UART の HEARTBEAT・指令、生存確認。"""
        self._complete_requests()
        self._publish_own_telemetry(self.clock.now())
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
        if request["target"] == self.device_id and request["op"] == "set_mode":
            return {"result": "system_mode"}
        if self.bridge is not None and request["target"] == self.bridge.profile.device_id:
            return {"result": "uart"}
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
        elif record.result == "system_mode":
            self._on_mode_request(record, request, now)
        elif record.result == "uart":
            # UART のデバイス宛て：作品03と同じく指令に変えて送り、ACK で最終結果を返す
            payload = {k: request[k] for k in ("op", "args", "lease_s") if k in request}
            payload["priority"] = "schedule" if request["kind"] == "schedule" else "manual"
            cmd = self.submit_uart(payload)
            cmd.request_id = request_id
            record.status = "accepted"
            record.cmd_id = cmd.cmd_id
        else:
            record.status = "accepted"
            record.cmd_id = new_ulid(self.clock)
            delay = float(rule.get("done_after_s", DEFAULT_DONE_AFTER_S))
            record.done_at = now + timedelta(seconds=delay)
        self._respond(record)

    def _on_mode_request(
        self, record: RequestRecord, request: Mapping[str, Any], now: datetime
    ) -> None:
        """gw-01 宛ての set_mode：EMERGENCY の解除の承認（v0.2.3 #46）。"""
        mode = (request.get("args") or {}).get("mode")
        recovered = self.emergency_condition_until is None or now >= self.emergency_condition_until
        if mode != NORMAL_MODE:
            record.status, record.reason = "rejected", "not_permitted"
        elif not recovered:
            record.status, record.reason = "rejected", "unsafe"
        else:
            # 承認の処理を表す ULID を cmd_id に入れる（accepted・done では cmd_id が必須。#7）
            record.status = "accepted"
            record.cmd_id = new_ulid(self.clock)
            record.done_at = now
            record.result = "approve"

    def _set_system_mode(self, mode: str) -> None:
        if mode == self.system_mode:
            return
        previous, self.system_mode = self.system_mode, mode
        self._publish(
            KIND_EVENT,
            {
                "code": "SYSTEM_MODE_CHANGED",
                "severity": _MODE_SEVERITY.get(mode, "info"),
                "detail": {"from": previous, "to": mode},
            },
        )
        self._publish(KIND_STATE, self._state_payload("online"))

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
                if record.result == "approve":
                    self._set_system_mode(NORMAL_MODE)
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

    def submit_internal(self, op: int, args: tuple[int, ...] = ()) -> CmdRecord:
        """内部の操作（GET_DIAG・SET_THRESHOLD）を待ち行列に入れる。ACK は cmd/ack にしない。"""
        record = CmdRecord(new_ulid(self.clock), {"args": list(args)}, internal_op=op)
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
        if result.diag is not None:
            self.diag_history.append(result.diag)
        if result.internal_ack is not None:
            record = self._in_flight.pop(int(result.internal_ack["cmd_ref"]), None)
            if record is not None:
                record.ack = dict(result.internal_ack)
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
        self._finish_request(record)

    def _finish_request(self, cmd: CmdRecord) -> None:
        """UART の指令の結果を、元の request の最終結果として返す。"""
        if cmd.request_id is None:
            return
        request = self.requests.get(cmd.request_id)
        if request is None or request.status != "accepted":
            return
        status = cmd.ack["status"] if cmd.ack is not None else None
        if status == "accepted":
            return
        if cmd.expired:
            request.status, request.reason = "rejected", "expired"  # #40
        elif status == "done":
            request.status, request.reason = "done", None
        else:
            # 指令が拒否・失敗・タイムアウト：要求元には failed
            # （代役の選択。タイムアウトの reason は target_offline）
            request.status = "failed"
            reason = cmd.ack.get("reason") if cmd.ack is not None else None
            request.reason = str(reason) if reason else "target_offline"
        self._respond(request)

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
            # UART デバイス名義の offline の state（retain、bridged_by 付き。#36）
            offline = self.bridge.offline_state()
            if offline is not None:
                self.outbox.append(
                    Outgoing(
                        self.topic(KIND_STATE, offline.device_id),
                        offline.to_dict(),
                        QOS_BY_KIND[KIND_STATE],
                        True,
                    )
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
                self._finish_request(record)
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

    def _expired(self, record: CmdRecord, now: datetime) -> bool:
        expires_at = record.payload.get("expires_at")
        return isinstance(expires_at, str) and now >= parse_ts(expires_at)

    def _transmit(self, record: CmdRecord, now: datetime, *, count_attempt: bool = True) -> bool:
        assert self.bridge is not None and record.cmd_ref is not None
        if record.attempts == 0 and self._expired(record, now):
            # expires_at を過ぎた指令は UART に送らずに終える。cmd/ack は作らない（#40）
            record.expired = True
            self.stats["uart_cmd_expired"] += 1
            self._finish_request(record)
            return False
        if record.internal_op is not None:
            args = tuple(int(a) for a in record.payload.get("args", []))
            self.uart_tx += encode_frame(
                self.bridge.internal_frame(record.internal_op, record.cmd_ref, args)
            )
            if count_attempt:
                record.attempts += 1
                record.sent_at = now
                self._last_cmd_sent = now
            return True
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
