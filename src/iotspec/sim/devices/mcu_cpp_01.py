"""作品02（mcu-cpp-01、C++ × ESP32）の代役。MQTT に直接つながる。

作品02の設計書の要点に従う：
- 受けた set_actuator はリース付きの上位指令。リースが切れたらローカル制御（owner=local）に戻る
- 安全インターロック：ポンプは連続30秒（プロファイルの max_on_s）で強制停止。
  気温45℃以上でファン100%。
  値を丸めたら ack に limited と applied、telemetry の owner は interlock
- 重複した cmd_id（直近64件・10分）には前回の ack をそのまま返す
- 優先度：インターロック ＞ 上位指令（リース中）＞ 本体の手動操作 ＞ ローカルルール ＞ 既定値
  （本体の手動操作はシミュレータでは起こさない）

このクラスは通信を持たない。発行するメッセージは outbox に積み、MQTT への送信は sim.mqtt が行う。
時刻はすべて Clock から取る。
"""

from __future__ import annotations

import json
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from iotspec import SPEC_VERSION
from iotspec.clock import Clock, parse_ts
from iotspec.constants import (
    CMD_DEDUP_CAPACITY,
    CMD_DEDUP_TTL_S,
    DEFAULT_SITE,
    KIND_CMD,
    KIND_CMD_ACK,
    KIND_EVENT,
    KIND_STATE,
    KIND_TELEMETRY,
    QOS_BY_KIND,
    RETAIN_KINDS,
    SCHEMA_NAMES,
    TOPIC_FORMAT,
)
from iotspec.envelope import EnvelopeBuilder, SeqCounter
from iotspec.lease import Lease, resolve_lease_s
from iotspec.profile import Profile
from iotspec.schema import validate
from iotspec.sim.devices import Outgoing
from iotspec.sim.ratelimit import RateLimiter
from iotspec.sim.scenario import Fault
from iotspec.sim.signals import SignalSet

__all__ = ["CmdDedup", "McuCpp01"]

# 作品02の設計書の値（spec.md ではなく作品02の安全設計）
FAN_INTERLOCK_ACTUATOR: Final = "fan_main"
FAN_INTERLOCK_CHANNEL: Final = "temp_air"
FAN_INTERLOCK_TEMP_C: Final = 45.0
"""気温がこの値以上でファンを最大出力にする。"""
FW_VERSION: Final = f"sim-{SPEC_VERSION}"
BOOT_RESET_REASON: Final = "power_on"


@dataclass
class _LeaseState:
    lease: Lease
    level: int
    priority: str
    cmd_id: str


class CmdDedup:
    """直近 capacity 件・ttl_s 秒の cmd_id と ack の payload を覚える（第4章）。"""

    def __init__(self, capacity: int = CMD_DEDUP_CAPACITY, ttl_s: float = CMD_DEDUP_TTL_S) -> None:
        self.capacity = capacity
        self.ttl = timedelta(seconds=ttl_s)
        self._items: OrderedDict[str, tuple[datetime, dict[str, Any]]] = OrderedDict()

    def get(self, cmd_id: str, now: datetime) -> dict[str, Any] | None:
        self._purge(now)
        item = self._items.get(cmd_id)
        return None if item is None else item[1]

    def put(self, cmd_id: str, ack: dict[str, Any], now: datetime) -> None:
        self._purge(now)
        self._items[cmd_id] = (now, ack)
        self._items.move_to_end(cmd_id)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def _purge(self, now: datetime) -> None:
        while self._items:
            stored_at = next(iter(self._items.values()))[0]
            if now - stored_at < self.ttl:
                break
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


class McuCpp01:
    def __init__(
        self,
        profile: Profile,
        clock: Clock,
        signals: SignalSet,
        *,
        site: str = DEFAULT_SITE,
        faults: Iterable[Fault] = (),
        local_rules: Mapping[str, int] | None = None,
        initial_mode: str | None = None,
        boot_id: str | None = None,
    ) -> None:
        self.profile = profile
        self.clock = clock
        self.signals = signals
        self.site = site
        self.device_id = profile.device_id
        self.builder = EnvelopeBuilder(self.device_id, clock, SeqCounter(boot_id))
        self.faults = tuple(faults)
        self.local_rules = dict(local_rules or {})
        self.mode = initial_mode if initial_mode is not None else profile.modes[0]
        self.outbox: list[Outgoing] = []
        self.stats: Counter[str] = Counter()
        """送らなかった電文・受け付けなかった指令の件数（rate_limited:{kind} など）。"""
        self.leases: dict[str, _LeaseState] = {}
        self.levels: dict[str, int] = {a.id: 0 for a in profile.actuators}
        self.owners: dict[str, str] = {a.id: "none" for a in profile.actuators}
        self.readings: dict[str, float] = {}
        self._interlocked: set[str] = set()
        self._on_since: dict[str, datetime] = {}
        self._locked_out: set[str] = set()
        self._faulty: set[str] = set()
        self._last_sample: datetime | None = None
        self._dedup = CmdDedup()
        self._limiters: dict[str, RateLimiter] = {}
        self._consumed: Counter[int] = Counter()

    # ------------------------------------------------------------ トピック

    def topic(self, kind: str) -> str:
        return TOPIC_FORMAT.format(site=self.site, device_id=self.device_id, kind=kind)

    @property
    def cmd_topic(self) -> str:
        return self.topic(KIND_CMD)

    @property
    def boot_id(self) -> str:
        return self.builder.boot_id

    # ------------------------------------------------------------ 起動・周期処理

    def will(self) -> Outgoing:
        """LWT（接続時に登録する offline の state。retain）。"""
        return self._outgoing(KIND_STATE, self._state_payload("offline"))

    def start(self) -> None:
        """起動：計測を1回行い、state（online）と BOOT イベントを送る。"""
        self._sample()
        self._evaluate()
        self.publish_state()
        self._event("BOOT", "info", detail={"reset_reason": BOOT_RESET_REASON})

    def tick(self) -> None:
        """計測周期ごとの処理：計測、異常の注入、出力の評価、telemetry の送信。"""
        self._sample()
        self._update_sensor_faults()
        self._evaluate()
        readings = [
            {
                "channel": c.channel,
                "value": self.readings[c.channel],
                "unit": c.unit,
                "quality": "fault" if c.channel in self._faulty else "good",
            }
            for c in self.profile.channels
        ]
        actuators = [
            {"id": a.id, "level": self.levels[a.id], "owner": self.owners[a.id]}
            for a in self.profile.actuators
        ]
        self._send(KIND_TELEMETRY, {"readings": readings, "actuators": actuators})

    def shutdown(self) -> Outgoing:
        """正常終了で送る offline の state（LWT と同じ内容。retain）。"""
        return self._outgoing(KIND_STATE, self._state_payload("offline"))

    def publish_state(self) -> None:
        self._send(KIND_STATE, self._state_payload("online"))

    def _state_payload(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "mode": self.mode,
            "channels": [
                {"channel": c.channel, "unit": c.unit, "period_ms": c.period_ms}
                for c in self.profile.channels
            ],
            "actuators": [
                {"id": a.id, "min": a.min, "max": a.max, "max_on_s": a.max_on_s}
                for a in self.profile.actuators
            ],
            "fw_version": FW_VERSION,
        }

    def _sample(self) -> None:
        now = self.clock.now()
        dt = 0.0 if self._last_sample is None else (now - self._last_sample).total_seconds()
        self._last_sample = now
        self.readings = self.signals.sample(now, dt, self.levels)

    def _update_sensor_faults(self) -> None:
        now = self.clock.now()
        active = {
            f.channel
            for f in self.faults
            if f.type == "sensor_fault" and f.channel is not None and f.active(now)
        }
        for channel in sorted(active - self._faulty):
            self._event("SENSOR_FAULT", "critical", channel=channel)
        for channel in sorted(self._faulty - active):
            self._event("SENSOR_RECOVERED", "info", channel=channel)
        self._faulty = active

    # ------------------------------------------------------------ 出力の決定

    def _evaluate(self) -> None:
        """リースの期限切れとインターロックを反映し、各出力と owner を決める。"""
        now = self.clock.now()
        for spec in self.profile.actuators:
            act = spec.id
            lease = self.leases.get(act)
            if lease is not None and lease.lease.is_expired(now):
                del self.leases[act]
                lease = None
            if lease is not None:
                requested, owner = lease.level, lease.priority
            elif act in self.local_rules:
                requested, owner = self.local_rules[act], "local"
            else:
                requested, owner = 0, "none"
            level, interlocked = self._apply_interlocks(act, spec.max_on_s, requested, now)
            if interlocked and act not in self._interlocked:
                self._event("INTERLOCK_ACTIVE", "warning", detail={"actuator": act})
            elif not interlocked and act in self._interlocked:
                self._event("INTERLOCK_RELEASED", "info", detail={"actuator": act})
            if interlocked:
                self._interlocked.add(act)
            else:
                self._interlocked.discard(act)
            self.levels[act] = level
            self.owners[act] = "interlock" if interlocked else owner

    def _apply_interlocks(
        self, act: str, max_on_s: float | None, requested: int, now: datetime
    ) -> tuple[int, bool]:
        if act == FAN_INTERLOCK_ACTUATOR:
            temp = self.readings.get(FAN_INTERLOCK_CHANNEL)
            hot = (
                temp is not None
                and FAN_INTERLOCK_CHANNEL not in self._faulty
                and temp >= FAN_INTERLOCK_TEMP_C
            )
            if hot:
                spec = self.profile.actuator(act)
                return (int(spec.max) if spec is not None else requested), True
        if max_on_s is not None:
            if act in self._locked_out:
                if requested == 0:
                    self._locked_out.discard(act)
                    return 0, False
                return 0, True
            if requested > 0:
                since = self._on_since.setdefault(act, now)
                if (now - since).total_seconds() >= max_on_s:
                    self._locked_out.add(act)
                    del self._on_since[act]
                    return 0, True
            else:
                self._on_since.pop(act, None)
        return requested, False

    # ------------------------------------------------------------ 指令

    def handle_message(self, topic: str, payload: bytes) -> None:
        """MQTT で受けたメッセージを処理する（自分の cmd トピック以外は無視）。"""
        if topic != self.cmd_topic:
            return
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.stats["invalid_cmd"] += 1
            return
        if validate(message) or message.get("schema") != SCHEMA_NAMES[KIND_CMD]:
            self.stats["invalid_cmd"] += 1
            return
        cmd = message["payload"]
        now = self.clock.now()
        cached = self._dedup.get(cmd["cmd_id"], now)
        if cached is not None:
            self.stats["duplicate_cmd"] += 1
            self._send(KIND_CMD_ACK, cached)
            return
        ack = self._execute(cmd, now)
        self._dedup.put(cmd["cmd_id"], ack, now)
        self._send(KIND_CMD_ACK, ack)

    def _execute(self, cmd: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        cmd_id = str(cmd["cmd_id"])
        expires_at = cmd.get("expires_at")
        if expires_at is not None and now > parse_ts(expires_at):
            return _rejected(cmd_id, "expired")
        op = cmd["op"]
        args = cmd.get("args") or {}
        if op == "set_actuator":
            spec = self.profile.actuator(args["id"])
            if spec is None:
                return _rejected(cmd_id, "unknown_target")
            level = int(args["level"])
            if not spec.min <= level <= spec.max:
                return _rejected(cmd_id, "invalid_arg")
            lease_s = resolve_lease_s(cmd)
            assert lease_s is not None
            self.leases[spec.id] = _LeaseState(
                Lease(now, lease_s), level, str(cmd["priority"]), cmd_id
            )
            self._evaluate()
            applied = self.levels[spec.id]
            will_be_cut = spec.max_on_s is not None and level > 0 and lease_s > spec.max_on_s
            limited = applied != level or will_be_cut
            return _done(cmd_id, limited=limited, applied={"level": applied})
        if op == "release":
            if self.profile.actuator(args["id"]) is None:
                return _rejected(cmd_id, "unknown_target")
            self.leases.pop(args["id"], None)
            self._evaluate()
            return _done(cmd_id)
        if op == "set_mode":
            mode = args["mode"]
            if mode not in self.profile.modes:
                return _rejected(cmd_id, "invalid_arg")
            if mode != self.mode:
                previous, self.mode = self.mode, mode
                self._event("MODE_CHANGED", "info", detail={"from": previous, "to": mode})
                self.publish_state()
            return _done(cmd_id)
        if op == "get_status":
            self.publish_state()
            return _done(cmd_id)
        return _rejected(cmd_id, "unknown_op")

    # ------------------------------------------------------------ 送信

    def _event(
        self,
        code: str,
        severity: str,
        *,
        channel: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"code": code, "severity": severity}
        if channel is not None:
            payload["channel"] = channel
            if channel in self.readings:
                payload["value"] = self.readings[channel]
        if detail is not None:
            payload["detail"] = detail
        self._send(KIND_EVENT, payload)

    def _outgoing(self, kind: str, payload: Mapping[str, Any]) -> Outgoing:
        now = self.clock.now()
        skew = sum(
            (f.offset_s for f in self.faults if f.type == "clock_skew" and f.active(now)), 0.0
        )
        envelope = self.builder.build(kind, payload, ts=now + timedelta(seconds=skew))
        return Outgoing(
            topic=self.topic(kind),
            message=envelope.to_dict(),
            qos=QOS_BY_KIND[kind],
            retain=kind in RETAIN_KINDS,
        )

    def _one_shot(self, fault_type: str, kind: str) -> int:
        """発生時刻を過ぎた一度きりの異常（seq_gap・duplicate）の残り回数を消費して返す。"""
        now = self.clock.now()
        total = 0
        for index, f in enumerate(self.faults):
            if f.type != fault_type or f.kind != kind or f.at > now:
                continue
            if self._consumed[index] == 0:
                self._consumed[index] = 1
                total += f.count
        return total

    def _send(self, kind: str, payload: Mapping[str, Any]) -> None:
        limiter = self._limiters.get(kind)
        if limiter is None:
            limiter = self._limiters[kind] = RateLimiter(self.clock)
        if not limiter.try_acquire():
            self.stats[f"rate_limited:{kind}"] += 1
            return
        for _ in range(self._one_shot("seq_gap", kind)):
            self.builder.counter.next(kind)
        out = self._outgoing(kind, payload)
        now = self.clock.now()
        if any(f.type == "silence" and f.active(now) for f in self.faults):
            # 送信途絶：電文は作るが届かない（seq は進むので受信側から欠損として見える）
            self.stats["silenced"] += 1
            return
        self.outbox.append(out)
        for _ in range(self._one_shot("duplicate", kind)):
            self.outbox.append(out)

    def drain(self) -> list[Outgoing]:
        """積んだメッセージを取り出す。"""
        items, self.outbox = self.outbox, []
        return items


def _done(
    cmd_id: str, *, limited: bool = False, applied: dict[str, Any] | None = None
) -> dict[str, Any]:
    ack: dict[str, Any] = {"cmd_id": cmd_id, "status": "done", "limited": limited}
    if applied is not None:
        ack["applied"] = applied
    ack["reason"] = None
    return ack


def _rejected(cmd_id: str, reason: str) -> dict[str, Any]:
    return {"cmd_id": cmd_id, "status": "rejected", "limited": False, "reason": reason}
