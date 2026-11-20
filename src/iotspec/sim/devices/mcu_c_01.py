"""作品01（mcu-c-01、C × STM32）の代役。UART フレームで作品03（または gw-01 簡易版）とつながる。

作品01の設計書の要点に従う：
- 1秒ごとに TELEMETRY と HEARTBEAT、状態変化で EVENT。起動後は STATE → BOOT イベントの順に送り、
  STATE は以後60秒ごとにも送る（第7章、#23）
- UART の seq は方向・フレーム種別ごとの連番（#25）
- CMD に ACK を返す。直近16件の cmd_ref には前回の ACK を返す
- 上位の HEARTBEAT が10秒途絶えたら手動指定（SET_FAN_OVERRIDE）を解除し、UPSTREAM_LOST を送る
- 状態：INIT・NORMAL・WARNING・CRITICAL・SENSOR_FAULT（profiles/mcu-c-01.yaml）

通信は持たない。送るバイト列は tx に積み、受けたバイト列は feed で渡す。時刻は Clock から取る。
"""

from __future__ import annotations

import secrets
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from iotspec.clock import Clock
from iotspec.constants import (
    UART_CMD_REF_DEDUP_CAPACITY,
    UART_OP_CLEAR_OVERRIDE,
    UART_OP_GET_DIAG,
    UART_OP_GET_STATUS,
    UART_OP_SET_FAN_OVERRIDE,
    UART_OP_SET_THRESHOLD,
    UART_SEQ_MODULO,
    UART_STATE_PERIOD_S,
    UART_UPSTREAM_LOST_S,
    UINT32_MAX,
)
from iotspec.profile import Profile
from iotspec.sim.scenario import Fault
from iotspec.sim.signals import SignalSet
from iotspec.uart import cobs
from iotspec.uart.frames import (
    Ack,
    Cmd,
    Discarded,
    Event,
    Frame,
    FrameReader,
    Heartbeat,
    State,
    Telemetry,
    encode_frame,
)
from iotspec.uart.mapping import EVENT_CODE, QUALITY, REASON, SEVERITY, STATUS

__all__ = ["McuC01", "Thresholds"]

TEMP_CHANNEL: Final = "temp_board"
FAN: Final = "fan_main"
TEMP_SCALE: Final = 100
FW_VERSION: Final = 0x0002
"""代役の fw_version（"0.2"）。"""
_I16_MIN, _I16_MAX = -0x8000, 0x7FFF
_LEVEL_MAX = 1000

# 状態番号（profiles/mcu-c-01.yaml の states と同じ順）
INIT, NORMAL, WARNING, CRITICAL, SENSOR_FAULT = 0, 1, 2, 3, 4
_SEVERITY_OF_STATE: Final = {
    INIT: "info",
    NORMAL: "info",
    WARNING: "warning",
    CRITICAL: "critical",
    SENSOR_FAULT: "critical",
}
"""STATE_CHANGED の重大度は遷移先に従う（第6章）。状態ごとの値は open-questions #32。"""


@dataclass
class Thresholds:
    """状態の判定に使うしきい値（0.01℃単位）。既定値は open-questions #32。"""

    warn_c100: int = 3500
    crit_c100: int = 4500
    hyst_c100: int = 200


@dataclass
class _Override:
    duty_pm: int
    until: datetime


class McuC01:
    def __init__(
        self,
        profile: Profile,
        clock: Clock,
        signals: SignalSet,
        *,
        faults: Iterable[Fault] = (),
        local_rules: Mapping[str, int] | None = None,
        thresholds: Thresholds | None = None,
        boot_id: int | None = None,
    ) -> None:
        self.profile = profile
        self.device_id = profile.device_id
        self.clock = clock
        self.signals = signals
        self.faults = tuple(faults)
        self.local_rules = dict(local_rules or {})
        self.thresholds = thresholds or Thresholds()
        self.boot_id = boot_id if boot_id is not None else secrets.randbits(32)
        self.tx = bytearray()
        """作品03へ送るバイト列（符号化済みフレームの連続）。"""
        self.sent: list[Frame] = []
        """送ったフレーム（テスト・確認用）。"""
        self.stats: Counter[str] = Counter()
        self.state = INIT
        self.temp: float = 0.0
        self.override: _Override | None = None
        self.upstream_lost = False
        self._boot_at = clock.now()
        self._last_upstream = clock.now()
        self._last_state_at: datetime | None = None
        self._seq: dict[int, int] = {}
        self._reader = FrameReader()
        self._acks: OrderedDict[int, tuple[int, int, int, int]] = OrderedDict()
        self._last_sample: datetime | None = None
        self._consumed: set[int] = set()
        self._corrupt_left = 0

    # ------------------------------------------------------------ 起動・周期処理

    def uptime_ms(self) -> int:
        return int((self.clock.now() - self._boot_at) / timedelta(milliseconds=1)) & UINT32_MAX

    def start(self) -> None:
        """起動：STATE → BOOT イベントの順に送る（#23）。"""
        self._sample()
        self._send_state()
        self._send_event("BOOT", "info", INIT, INIT)

    def tick(self) -> None:
        """1秒ごと：計測、状態の判定、上位の生存確認、TELEMETRY と HEARTBEAT、60秒ごとの STATE。"""
        now = self.clock.now()
        self._sample()
        self._update_state()
        if (
            not self.upstream_lost
            and (now - self._last_upstream).total_seconds() >= UART_UPSTREAM_LOST_S
        ):
            self.upstream_lost = True
            self.override = None
            self._send_event("UPSTREAM_LOST", "warning", self.state, self.state)
        if self.override is not None and now >= self.override.until:
            self.override = None
        faulty = self._temp_faulty()
        self._send(
            Telemetry(
                self._next_seq(Telemetry.TYPE),
                self.uptime_ms(),
                self._temp_c100(),
                QUALITY.to_uart("fault" if faulty else "good"),
                self.state,
                self.fan_pm(),
            )
        )
        self._send(Heartbeat(self._next_seq(Heartbeat.TYPE), self.uptime_ms()))
        if (
            self._last_state_at is None
            or (now - self._last_state_at).total_seconds() >= UART_STATE_PERIOD_S
        ):
            self._send_state()

    def fan_pm(self) -> int:
        if self.override is not None:
            return self.override.duty_pm
        return self.local_rules.get(FAN, 0)

    def _sample(self) -> None:
        now = self.clock.now()
        dt = 0.0 if self._last_sample is None else (now - self._last_sample).total_seconds()
        self._last_sample = now
        self.temp = self.signals.sample(now, dt, {FAN: self.fan_pm()})[TEMP_CHANNEL]

    def _temp_faulty(self) -> bool:
        now = self.clock.now()
        return any(
            f.type == "sensor_fault" and f.channel == TEMP_CHANNEL and f.active(now)
            for f in self.faults
        )

    def _temp_c100(self) -> int:
        return max(_I16_MIN, min(_I16_MAX, round(self.temp * TEMP_SCALE)))

    def _update_state(self) -> None:
        previous = self.state
        if self._temp_faulty():
            new = SENSOR_FAULT
        else:
            t, th = self._temp_c100(), self.thresholds
            if t >= th.crit_c100:
                new = CRITICAL
            elif t >= th.warn_c100:
                new = (
                    CRITICAL
                    if previous == CRITICAL and t > th.crit_c100 - th.hyst_c100
                    else WARNING
                )
            elif previous in (WARNING, CRITICAL) and t > th.warn_c100 - th.hyst_c100:
                new = WARNING
            else:
                new = NORMAL
        if new == previous:
            return
        self.state = new
        if new == SENSOR_FAULT:
            self._send_event("SENSOR_FAULT", "critical", previous, new)
        elif previous == SENSOR_FAULT:
            self._send_event("SENSOR_RECOVERED", "info", previous, new)
        self._send_event("STATE_CHANGED", _SEVERITY_OF_STATE[new], previous, new)

    # ------------------------------------------------------------ 受信

    def feed(self, data: bytes) -> None:
        """作品03から受けたバイト列を処理する。"""
        for frame in self._reader.feed(data):
            if isinstance(frame, Discarded):
                self.stats[f"discarded:{frame.reason}"] += 1
            elif isinstance(frame, Heartbeat):
                self._last_upstream = self.clock.now()
                self.upstream_lost = False
            elif isinstance(frame, Cmd):
                self._on_cmd(frame)
            else:
                self.stats["unexpected_frame"] += 1

    def _on_cmd(self, frame: Cmd) -> None:
        cached = self._acks.get(frame.cmd_ref)
        if cached is not None:
            self.stats["duplicate_cmd_ref"] += 1
            self._send(Ack(self._next_seq(Ack.TYPE), frame.cmd_ref, *cached))
            return
        result = self._execute(frame)
        self._acks[frame.cmd_ref] = result
        self._acks.move_to_end(frame.cmd_ref)
        while len(self._acks) > UART_CMD_REF_DEDUP_CAPACITY:
            self._acks.popitem(last=False)
        self._send(Ack(self._next_seq(Ack.TYPE), frame.cmd_ref, *result))

    def _execute(self, frame: Cmd) -> tuple[int, int, int, int]:
        """(status, reason, limited, applied) を返す。"""
        done, rejected = STATUS.to_uart("done"), STATUS.to_uart("rejected")
        ok = REASON.to_uart(None)
        if frame.op == UART_OP_SET_FAN_OVERRIDE:
            (duty,) = frame.args
            if not 0 <= duty <= _LEVEL_MAX or frame.ttl_s == 0:
                return rejected, REASON.to_uart("invalid_arg"), 0, 0
            self.override = _Override(duty, self.clock.now() + timedelta(seconds=frame.ttl_s))
            return done, ok, 0, duty
        if frame.op == UART_OP_CLEAR_OVERRIDE:
            self.override = None
            return done, ok, 0, 0
        if frame.op == UART_OP_SET_THRESHOLD:
            warn, crit, hyst = frame.args
            if not warn < crit or hyst < 0:
                return rejected, REASON.to_uart("invalid_arg"), 0, 0
            self.thresholds = Thresholds(warn, crit, hyst)
            return done, ok, 0, 0
        if frame.op == UART_OP_GET_STATUS:
            self._send_state()
            return done, ok, 0, 0
        if frame.op == UART_OP_GET_DIAG:
            return done, ok, 0, 0
        return rejected, REASON.to_uart("unknown_op"), 0, 0

    # ------------------------------------------------------------ 送信

    def _next_seq(self, frame_type: int) -> int:
        seq = self._seq.get(frame_type, 0)
        self._seq[frame_type] = (seq + 1) % UART_SEQ_MODULO
        return seq

    def _send_state(self) -> None:
        self._last_state_at = self.clock.now()
        period_ms = min(c.period_ms for c in self.profile.channels)
        self._send(State(self._next_seq(State.TYPE), FW_VERSION, self.boot_id, period_ms))

    def _send_event(self, code: str, severity: str, from_state: int, to_state: int) -> None:
        self._send(
            Event(
                self._next_seq(Event.TYPE),
                self.uptime_ms(),
                EVENT_CODE.to_uart(code),
                SEVERITY.to_uart(severity),
                from_state,
                to_state,
                self._temp_c100(),
            )
        )

    def _send(self, frame: Frame) -> None:
        data = encode_frame(frame)
        now = self.clock.now()
        for index, f in enumerate(self.faults):
            if f.type == "corrupt_frame" and f.at <= now and index not in self._consumed:
                self._consumed.add(index)
                self._corrupt_left += f.count
        if self._corrupt_left > 0:
            self._corrupt_left -= 1
            raw = bytearray(cobs.decode(data[:-1]))
            raw[-1] ^= 0xFF  # CRC の上位バイトを壊す
            data = cobs.encode(bytes(raw)) + b"\x00"
            self.stats["corrupted_sent"] += 1
        self.sent.append(frame)
        self.tx += data

    def drain_tx(self) -> bytes:
        data, self.tx = bytes(self.tx), bytearray()
        return data
