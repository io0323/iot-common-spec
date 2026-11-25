"""作品01（mcu-c-01、C × STM32）の代役。UART フレームで作品03（または gw-01 簡易版）とつながる。

作品01の設計書（要件定義・基本設計 第5・8章、詳細設計 第4〜7章）に合わせる（open-questions #32）：
- 1秒ごとに TELEMETRY と HEARTBEAT。INIT を抜けるときに STATE → BOOT イベントの順に送り、
  STATE は以後60秒ごとにも送る（第7章、#23）。BOOT の from は reset_reason、to は 0（v0.2.2 #38）
- UART の seq は方向・フレーム種別ごとの連番（#25）
- 状態：INIT・NORMAL・WARNING・CRITICAL・SENSOR_FAULT（profiles/mcu-c-01.yaml）。
  しきい値の既定は warn 45.00℃・crit 60.00℃・hyst 3.00℃。上昇は1秒継続で確定し、
  WARNING → NORMAL は warn−hyst 未満が1秒続いたら。CRITICAL と SENSOR_FAULT は承認（ボタン）を待つ
- 1回の状態遷移につき EVENT は1件（v0.2.2 #41 の表、TRANSITION_EVENTS）
- ファンの出力は max(手動指定 または auto(t), 状態の下限)。
  下限は INIT・CRITICAL・SENSOR_FAULT 1000、WARNING 700、NORMAL 0。
  下限で手動指定を引き上げ始めたら INTERLOCK_ACTIVE、やめたら INTERLOCK_RELEASED
  （v0.2.2 #39、from・to は同じ状態番号）
- CMD に ACK を返す。直近16件の cmd_ref には前回の ACK を返す（GET_STATUS・GET_DIAG は読み出しなので
  STATE・DIAG も送り直す）。expired は返さない（#40）。GET_DIAG には DIAG → ACK(done)（#37）
- 上位の HEARTBEAT が10秒途絶えたら手動指定を解除し、UPSTREAM_LOST を送る。
  HEARTBEAT が戻ったら UPSTREAM_RESTORED（EVENT code 8、#34）を送る

ボタンはないので、承認はシナリオの acks_at_s（または press_ack）で与える。
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
    UART_BOOT_EVENT_TO,
    UART_CMD_REF_DEDUP_CAPACITY,
    UART_DIAG_U16_SATURATED,
    UART_OP_CLEAR_OVERRIDE,
    UART_OP_GET_DIAG,
    UART_OP_GET_STATUS,
    UART_OP_SET_FAN_OVERRIDE,
    UART_OP_SET_THRESHOLD,
    UART_SEQ_MODULO,
    UART_STATE_PERIOD_S,
    UART_TTL_MAX_S,
    UART_TTL_MIN_S,
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
    Diag,
    Discarded,
    Event,
    Frame,
    FrameReader,
    Heartbeat,
    State,
    Telemetry,
    encode_frame,
)
from iotspec.uart.mapping import EVENT_CODE, QUALITY, REASON, RESET_REASON, SEVERITY, STATUS

__all__ = ["TRANSITION_EVENTS", "McuC01", "Thresholds", "transition_event"]

TEMP_CHANNEL: Final = "temp_board"
FAN: Final = "fan_main"
TEMP_SCALE: Final = 100
FW_VERSION: Final = 0x0002
"""代役の fw_version（"0.2"）。"""
_I16_MIN, _I16_MAX = -0x8000, 0x7FFF
FAULT_TEMP_C100: Final = _I16_MIN
"""センサー故障中の TELEMETRY・EVENT の temp_c100（作品01の設計、ゴールデン18・20）。"""
_LEVEL_MAX = 1000
_U16_MAX = UART_DIAG_U16_SATURATED

# 状態番号（profiles/mcu-c-01.yaml の states と同じ順）
INIT, NORMAL, WARNING, CRITICAL, SENSOR_FAULT = 0, 1, 2, 3, 4

CONFIRM: Final = timedelta(seconds=1)
"""上昇・下降の確定と、故障からの復帰に要る継続時間（作品01は100ms のサンプル10回）。"""

FAN_FLOOR: Final[dict[int, int]] = {
    INIT: 1000,
    NORMAL: 0,
    WARNING: 700,
    CRITICAL: 1000,
    SENSOR_FAULT: 1000,
}
"""状態ごとのファンの下限（‰）。安全側を弱める指定はここまで引き上げ、done・limited とする。"""

AUTO_FAN_START_C100: Final = 3500
AUTO_FAN_FULL_C100: Final = 4500
AUTO_FAN_MAX_PM: Final = 400
"""手動指定がないときのファン（auto(t)）：35℃未満 0、35〜45℃ で 0→40% の線形、45℃以上 40%。"""

# SET_THRESHOLD の検証（作品01の設計 第8章「指令の検証」）
THRESHOLD_MIN_C100: Final = -2000
THRESHOLD_MAX_C100: Final = 8000
HYST_MIN_C100: Final = 50
HYST_MAX_C100: Final = 1000

_SEVERITY_OF_STATE: Final = {
    NORMAL: "info",
    WARNING: "warning",
    CRITICAL: "critical",
}
"""INIT を抜けるときの STATE_CHANGED の重大度は遷移先に従う。"""

TRANSITION_EVENTS: Final[dict[tuple[int, int], tuple[str, str]]] = {
    (NORMAL, WARNING): ("THRESHOLD_EXCEEDED", "warning"),
    (NORMAL, CRITICAL): ("THRESHOLD_EXCEEDED", "critical"),
    (WARNING, CRITICAL): ("THRESHOLD_EXCEEDED", "critical"),
    (WARNING, NORMAL): ("THRESHOLD_CLEARED", "info"),
    (CRITICAL, WARNING): ("THRESHOLD_CLEARED", "info"),
    (CRITICAL, NORMAL): ("THRESHOLD_CLEARED", "info"),
    **{(s, SENSOR_FAULT): ("SENSOR_FAULT", "critical") for s in (INIT, NORMAL, WARNING, CRITICAL)},
    **{(SENSOR_FAULT, s): ("SENSOR_RECOVERED", "info") for s in (NORMAL, WARNING, CRITICAL)},
    **{(INIT, s): ("STATE_CHANGED", _SEVERITY_OF_STATE[s]) for s in (NORMAL, WARNING, CRITICAL)},
}
"""状態遷移 → 送る EVENT（code・severity）。1回の遷移につき1件（v0.2.2 #41、spec.md 第7章）。"""


def transition_event(from_state: int, to_state: int) -> tuple[str, str]:
    """遷移で送る EVENT の (code, severity)。表にない遷移は KeyError（代役では起こらない）。"""
    return TRANSITION_EVENTS[(from_state, to_state)]


@dataclass
class Thresholds:
    """状態の判定に使うしきい値（0.01℃単位）。既定値は作品01の設計（config/thresholds.h）。"""

    warn_c100: int = 4500
    crit_c100: int = 6000
    hyst_c100: int = 300


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
        reset_reason: str = "power_on",
        reset_count: int = 0,
        acks_at: Iterable[datetime] = (),
    ) -> None:
        self.profile = profile
        self.device_id = profile.device_id
        self.clock = clock
        self.signals = signals
        self.faults = tuple(faults)
        self.local_rules = dict(local_rules or {})
        """FAN があれば、手動指定がないときのファンを auto(t) の代わりにこの値にする。"""
        self.thresholds = thresholds or Thresholds()
        self.boot_id = boot_id if boot_id is not None else secrets.randbits(32)
        self.reset_reason = RESET_REASON.to_uart(reset_reason)
        self.reset_count = reset_count
        self.tx = bytearray()
        """作品03へ送るバイト列（符号化済みフレームの連続）。"""
        self.sent: list[Frame] = []
        """送ったフレーム（テスト・確認用）。"""
        self.stats: Counter[str] = Counter()
        self.state = INIT
        self.temp: float = 0.0
        self.override: _Override | None = None
        self.upstream_lost = False
        self.interlocked = False
        """状態の下限で手動指定を引き上げている（INTERLOCK_ACTIVE を送った）。"""
        self._acks_at = sorted(acks_at)
        self._ack_pressed = False
        self._boot_at = clock.now()
        self._last_upstream = clock.now()
        self._last_state_at: datetime | None = None
        self._seq: dict[int, int] = {}
        self._reader = FrameReader()
        self._acks: OrderedDict[int, tuple[int, int, int, int]] = OrderedDict()
        self._last_sample: datetime | None = None
        self._consumed: set[int] = set()
        self._corrupt_left = 0
        # 継続の起点（条件が崩れたときと温度が故障のときだけ None に戻す。状態が変わっても戻さない）
        self._warn_since: datetime | None = None
        self._crit_since: datetime | None = None
        self._below_since: datetime | None = None
        self._valid_since: datetime | None = None

    # ------------------------------------------------------------ 起動・周期処理

    def uptime_ms(self) -> int:
        return int((self.clock.now() - self._boot_at) / timedelta(milliseconds=1)) & UINT32_MAX

    def start(self) -> None:
        """INIT を抜ける：STATE → BOOT（from は reset_reason、to は 0。#23・#38）→ 遷移の EVENT。

        INIT の間はフレームを送らず指令も受けないので、代役は INIT を start の中で抜ける。
        """
        self._sample()
        self._send_state()
        self._send_event("BOOT", "info", self.reset_reason, UART_BOOT_EVENT_TO)
        self._update_state()

    def press_ack(self) -> None:
        """ボタンの承認（短押し）。次の状態判定で1回だけ使い、当たる遷移がなくても消費する。"""
        self._ack_pressed = True

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
        self._update_interlock()
        self._send(
            Telemetry(
                self._next_seq(Telemetry.TYPE),
                self.uptime_ms(),
                self._temp_c100(),
                QUALITY.to_uart("fault" if self._temp_faulty() else "good"),
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

    # ------------------------------------------------------------ 出力

    def auto_fan_pm(self) -> int:
        """手動指定がないときのファン（auto(t)、整数で切り捨て）。"""
        if FAN in self.local_rules:
            return self.local_rules[FAN]
        t = self._measured_c100()
        if t < AUTO_FAN_START_C100:
            return 0
        if t >= AUTO_FAN_FULL_C100:
            return AUTO_FAN_MAX_PM
        return (
            (t - AUTO_FAN_START_C100)
            * AUTO_FAN_MAX_PM
            // (AUTO_FAN_FULL_C100 - AUTO_FAN_START_C100)
        )

    def fan_pm(self) -> int:
        """ファンの出力：max(手動指定 または auto(t), 状態の下限)。"""
        base = self.override.duty_pm if self.override is not None else self.auto_fan_pm()
        return max(base, FAN_FLOOR[self.state])

    def _update_interlock(self) -> None:
        """下限で手動指定を引き上げ始めたら INTERLOCK_ACTIVE、やめたら RELEASED（#39）。"""
        raised = self.override is not None and self.override.duty_pm < FAN_FLOOR[self.state]
        if raised == self.interlocked:
            return
        self.interlocked = raised
        if raised:
            self._send_event("INTERLOCK_ACTIVE", "warning", self.state, self.state)
        else:
            self._send_event("INTERLOCK_RELEASED", "info", self.state, self.state)

    # ------------------------------------------------------------ 計測と状態

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

    def _measured_c100(self) -> int:
        return max(_I16_MIN, min(_I16_MAX, round(self.temp * TEMP_SCALE)))

    def _temp_c100(self) -> int:
        """送る temp_c100（故障中は -32768）。"""
        return FAULT_TEMP_C100 if self._temp_faulty() else self._measured_c100()

    def _take_ack(self) -> bool:
        now = self.clock.now()
        pressed = self._ack_pressed
        self._ack_pressed = False
        while self._acks_at and self._acks_at[0] <= now:
            self._acks_at.pop(0)
            pressed = True
        return pressed

    @staticmethod
    def _held(since: datetime | None, now: datetime) -> bool:
        return since is not None and now - since >= CONFIRM

    def _classify(self, t: int) -> int:
        th = self.thresholds
        if t >= th.crit_c100:
            return CRITICAL
        if t >= th.warn_c100:
            return WARNING
        return NORMAL

    def _update_state(self) -> None:
        """遷移表（作品01の詳細設計 第4章）を上から順に評価する。"""
        now = self.clock.now()
        ack = self._take_ack()
        faulty = self._temp_faulty()
        t, th = self._measured_c100(), self.thresholds
        if faulty:
            self._warn_since = self._crit_since = self._below_since = self._valid_since = None
        else:
            self._valid_since = self._valid_since or now
            self._warn_since = (self._warn_since or now) if t >= th.warn_c100 else None
            self._crit_since = (self._crit_since or now) if t >= th.crit_c100 else None
            below = t < th.warn_c100 - th.hyst_c100
            self._below_since = (self._below_since or now) if below else None

        previous, new = self.state, self.state
        if previous == INIT:
            new = SENSOR_FAULT if faulty else self._classify(t)
        elif faulty:
            new = SENSOR_FAULT
        elif previous == SENSOR_FAULT:
            if ack and self._held(self._valid_since, now):
                new = self._classify(t)
        elif previous in (NORMAL, WARNING) and self._held(self._crit_since, now):
            new = CRITICAL
        elif previous == NORMAL and self._held(self._warn_since, now):
            new = WARNING
        elif previous == WARNING and self._held(self._below_since, now):
            new = NORMAL
        elif previous == CRITICAL and ack and t < th.crit_c100 - th.hyst_c100:
            new = WARNING if t >= th.warn_c100 - th.hyst_c100 else NORMAL
        if new == previous:
            return
        self.state = new
        code, severity = transition_event(previous, new)
        self._send_event(code, severity, previous, new)
        self._update_interlock()

    # ------------------------------------------------------------ 受信

    def feed(self, data: bytes) -> None:
        """作品03から受けたバイト列を処理する。"""
        for frame in self._reader.feed(data):
            if isinstance(frame, Discarded):
                self.stats[f"discarded:{frame.reason}"] += 1
            elif isinstance(frame, Heartbeat):
                self._last_upstream = self.clock.now()
                if self.upstream_lost:
                    self.upstream_lost = False
                    self._send_event("UPSTREAM_RESTORED", "info", self.state, self.state)
            elif isinstance(frame, Cmd):
                self._on_cmd(frame)
            else:
                self.stats["unexpected_frame"] += 1

    def _on_cmd(self, frame: Cmd) -> None:
        cached = self._acks.get(frame.cmd_ref)
        if cached is not None:
            self.stats["duplicate_cmd_ref"] += 1
            self._before_ack(frame.op)  # 読み出しの op は STATE・DIAG も送り直す
            self._send(Ack(self._next_seq(Ack.TYPE), frame.cmd_ref, *cached))
            return
        result = self._execute(frame)
        self._acks[frame.cmd_ref] = result
        self._acks.move_to_end(frame.cmd_ref)
        while len(self._acks) > UART_CMD_REF_DEDUP_CAPACITY:
            self._acks.popitem(last=False)
        self._before_ack(frame.op)
        self._send(Ack(self._next_seq(Ack.TYPE), frame.cmd_ref, *result))
        self._update_interlock()

    def _before_ack(self, op: int) -> None:
        """ACK より先に送るもの：GET_STATUS は STATE、GET_DIAG は DIAG（#37）。"""
        if op == UART_OP_GET_STATUS:
            self._send_state()
        elif op == UART_OP_GET_DIAG:
            self._send(self.diag_frame())

    def _execute(self, frame: Cmd) -> tuple[int, int, int, int]:
        """(status, reason, limited, applied) を返す。expired は返さない（#40）。"""
        done, rejected = STATUS.to_uart("done"), STATUS.to_uart("rejected")
        ok, invalid = REASON.to_uart(None), REASON.to_uart("invalid_arg")
        if frame.op == UART_OP_SET_FAN_OVERRIDE:
            (duty,) = frame.args
            if not 0 <= duty <= _LEVEL_MAX or not UART_TTL_MIN_S <= frame.ttl_s <= UART_TTL_MAX_S:
                return rejected, invalid, 0, 0
            self.override = _Override(duty, self.clock.now() + timedelta(seconds=frame.ttl_s))
            applied = self.fan_pm()
            return done, ok, int(duty < applied), applied
        if frame.op == UART_OP_CLEAR_OVERRIDE:
            self.override = None
            return done, ok, 0, 0
        if frame.op == UART_OP_SET_THRESHOLD:
            if self.state in (CRITICAL, SENSOR_FAULT):
                return rejected, REASON.to_uart("unsafe"), 0, 0
            warn, crit, hyst = frame.args
            if not (
                THRESHOLD_MIN_C100 <= warn < crit <= THRESHOLD_MAX_C100
                and HYST_MIN_C100 <= hyst <= HYST_MAX_C100
                and crit - warn >= hyst
            ):
                return rejected, invalid, 0, 0
            self.thresholds = Thresholds(warn, crit, hyst)
            return done, ok, 0, 0
        if frame.op in (UART_OP_GET_STATUS, UART_OP_GET_DIAG):
            return done, ok, 0, 0
        return rejected, REASON.to_uart("unknown_op"), 0, 0

    # ------------------------------------------------------------ 送信

    def diag_frame(self) -> Diag:
        """いまの診断値（#37）。rx_frame_errors は COBS・長さ・種別・受信あふれの合計。"""
        frame_errors = sum(
            self.stats[k]
            for k in (
                "discarded:cobs_invalid",
                "discarded:too_short",
                "discarded:length_mismatch",
                "discarded:unknown_type",
                "discarded:payload_size",
                "discarded:args_size",
                "unexpected_frame",
            )
        )
        return Diag(
            self._next_seq(Diag.TYPE),
            self.uptime_ms(),
            self.reset_reason,
            min(self.reset_count, _U16_MAX),
            min(self.stats["discarded:crc_mismatch"], _U16_MAX),
            min(frame_errors, _U16_MAX),
            0,  # max_loop_us：代役にはループの計測がない
            0,  # overruns
        )

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
