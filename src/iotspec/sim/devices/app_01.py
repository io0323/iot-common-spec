"""作品04（app-01）の代役。シナリオに従って request を送り、request/ack を記録する（第4章）。

- 最初の応答まで REQUEST_RESPONSE_TIMEOUT_S（5秒）待ち、来なければ同じ request_id で最大
  REQUEST_MAX_RETRIES（3回）再送する。それでも来なければ failed（gateway_timeout）とする
- 受理（accepted）から REQUEST_FINAL_RESULT_TIMEOUT_S（60秒）以内に最終結果が来なければ
  failed（gateway_timeout）
- 手動操作の request_id は通常の ULID、スケジュール実行は schedule_request_id で決定的に作る

通信は持たない。送る電文は outbox に積む。時刻は Clock から取る。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iotspec.clock import Clock, format_ts
from iotspec.constants import (
    KIND_REQUEST,
    KIND_REQUEST_ACK,
    KIND_STATE,
    QOS_BY_KIND,
    REQUEST_FINAL_RESULT_TIMEOUT_S,
    REQUEST_MAX_RETRIES,
    REQUEST_RESPONSE_TIMEOUT_S,
    RETAIN_KINDS,
    SCHEMA_NAMES,
    TOPIC_FORMAT,
)
from iotspec.envelope import Envelope, EnvelopeBuilder, SeqCounter
from iotspec.ids import new_ulid, schedule_request_id
from iotspec.profile import Profile
from iotspec.schema import validate
from iotspec.sim.devices import Outgoing

__all__ = ["App01", "RequestState"]

FW_VERSION = "sim-app-0.2"
_FINAL = frozenset({"done", "failed", "rejected"})


@dataclass
class RequestState:
    request_id: str
    payload: dict[str, Any]
    attempts: int = 0
    last_sent: datetime | None = None
    accepted_at: datetime | None = None
    status: str = "pending"
    """pending → accepted → done／failed／rejected。応答がなければ failed（gateway_timeout）。"""
    reason: str | None = None
    acks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.status in _FINAL


class App01:
    def __init__(
        self, profile: Profile, clock: Clock, *, site: str, boot_id: str | None = None
    ) -> None:
        self.profile = profile
        self.device_id = profile.device_id
        self.clock = clock
        self.site = site
        self.builder = EnvelopeBuilder(self.device_id, clock, SeqCounter(boot_id))
        self.outbox: list[Outgoing] = []
        self.requests: dict[str, RequestState] = {}
        self.stats: Counter[str] = Counter()

    @property
    def boot_id(self) -> str:
        return self.builder.boot_id

    def topic(self, kind: str) -> str:
        return TOPIC_FORMAT.format(site=self.site, device_id=self.device_id, kind=kind)

    def subscriptions(self) -> list[str]:
        return [self.topic(KIND_REQUEST_ACK)]

    def _state(self, status: str, *, advance: bool) -> Outgoing:
        payload = {
            "status": status,
            "mode": "normal",
            "channels": [],
            "actuators": [],
            "fw_version": FW_VERSION,
        }
        if advance:
            envelope = self.builder.build(KIND_STATE, payload)
        else:
            envelope = Envelope(
                SCHEMA_NAMES[KIND_STATE],
                new_ulid(self.clock),
                self.device_id,
                self.builder.counter.peek(KIND_STATE),
                self.boot_id,
                self.clock.now(),
                "device",
                payload,
            )
        return Outgoing(self.topic(KIND_STATE), envelope.to_dict(), QOS_BY_KIND[KIND_STATE], True)

    def will(self) -> Outgoing:
        return self._state("offline", advance=False)

    def shutdown(self) -> Outgoing:
        return self._state("offline", advance=False)

    def start(self) -> None:
        self.outbox.append(self._state("online", advance=True))

    def drain(self) -> list[Outgoing]:
        items, self.outbox = self.outbox, []
        return items

    # ------------------------------------------------------------ 送信

    def request(self, spec: Mapping[str, Any]) -> RequestState:
        """シナリオの request 定義から request を作って送る。"""
        now = self.clock.now()
        kind = spec["kind"]
        if kind == "schedule":
            request_id = schedule_request_id(int(spec["schedule_id"]), now)
        else:
            request_id = new_ulid(self.clock)
        payload: dict[str, Any] = {
            "request_id": request_id,
            "kind": kind,
            "target": spec["target"],
            "op": spec["op"],
        }
        for key in ("args", "lease_s"):
            if key in spec:
                payload[key] = spec[key]
        if kind == "schedule":
            payload["schedule"] = {"id": int(spec["schedule_id"]), "planned_at": format_ts(now)}
        state = self.requests.setdefault(request_id, RequestState(request_id, payload))
        self._transmit(state)
        return state

    def _transmit(self, state: RequestState) -> None:
        envelope = self.builder.build(KIND_REQUEST, state.payload)
        self.outbox.append(
            Outgoing(
                self.topic(KIND_REQUEST),
                envelope.to_dict(),
                QOS_BY_KIND[KIND_REQUEST],
                KIND_REQUEST in RETAIN_KINDS,
            )
        )
        state.attempts += 1
        state.last_sent = self.clock.now()

    def tick(self) -> None:
        """1秒ごと：応答待ちの再送と、最終結果の期限切れ。"""
        now = self.clock.now()
        for state in self.requests.values():
            if state.final:
                continue
            if state.status == "pending":
                assert state.last_sent is not None
                if (now - state.last_sent).total_seconds() < REQUEST_RESPONSE_TIMEOUT_S:
                    continue
                if state.attempts <= REQUEST_MAX_RETRIES:
                    self.stats["retransmitted"] += 1
                    self._transmit(state)
                else:
                    self._fail(state)
            elif state.status == "accepted":
                assert state.accepted_at is not None
                if (now - state.accepted_at).total_seconds() >= REQUEST_FINAL_RESULT_TIMEOUT_S:
                    self._fail(state)

    def _fail(self, state: RequestState) -> None:
        state.status = "failed"
        state.reason = "gateway_timeout"
        self.stats["gateway_timeout"] += 1

    # ------------------------------------------------------------ 受信

    def handle_message(self, topic: str, payload: bytes) -> None:
        if topic not in self.subscriptions():
            return
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.stats["invalid_ack"] += 1
            return
        if validate(message) or message.get("schema") != SCHEMA_NAMES[KIND_REQUEST_ACK]:
            self.stats["invalid_ack"] += 1
            return
        ack = message["payload"]
        state = self.requests.get(str(ack["request_id"]))
        if state is None:
            self.stats["unknown_ack"] += 1
            return
        state.acks.append(dict(ack))
        if state.final:
            return
        status = str(ack["status"])
        if status == "accepted":
            if state.accepted_at is None:
                state.accepted_at = self.clock.now()
            state.status = "accepted"
        else:
            state.status = status
            state.reason = ack.get("reason")
