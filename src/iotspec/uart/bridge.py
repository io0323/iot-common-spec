"""UART フレーム ⇔ 共通エンベロープの変換（作品03 が行う変換の参照実装。第7章）。

- 受信（01 → 03）：TELEMETRY・EVENT・STATE・ACK を telemetry・event・state・cmd/ack に変換する。
  ts は受信時刻（Clock から取る）、ts_source は gateway、uptime_ms はエンベロープに残す。
  seq は UART の16ビット連番（方向・フレーム種別ごと。v0.2.1 #25）を種別ごとの SeqExtender で
  32ビットに広げた値で、対応する kind の seq にそのまま使う。
- 送信（03 → 01）：cmd の payload をプロファイルの commands で CMD に変換する。
  cmd_ref は呼び出し側が渡す。
- uptime_ms が減ったら再起動とみなし、新しい STATE を受けるまで TELEMETRY を変換せずに数える。
- DIAG（v0.2.2 #37）は電文にせず、復号した項目を BridgeResult.diag に入れて not_converted で数える。
  作品03の内部の操作（GET_DIAG など。internal_frame で送る）の ACK も cmd/ack にせず not_converted。
- BOOT の EVENT は from を reset_reason とし、detail を {"reset_reason": ...} だけにする（#38）。

変換結果は必ず schemas/ で検証し、適合しないものは破棄（理由つき）にする。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from iotspec.clock import Clock, format_ts
from iotspec.constants import (
    KIND_CMD_ACK,
    KIND_EVENT,
    KIND_STATE,
    KIND_TELEMETRY,
    SCHEMA_NAMES,
    UART_BOOT_EVENT_TO,
    UART_INITIAL_DEVICE_STATE,
    UART_OP_NAMES,
    UART_OP_SET_FAN_OVERRIDE,
    UART_SEQ_MODULO,
    UINT32_MAX,
)
from iotspec.envelope import Envelope
from iotspec.ids import new_ulid
from iotspec.lease import InvalidLeaseError, resolve_lease_s
from iotspec.profile import Profile
from iotspec.schema import validate
from iotspec.uart.frames import (
    CMD_ARG_NAMES,
    CMD_ARGS_FORMAT,
    Ack,
    Cmd,
    Diag,
    Discarded,
    Event,
    Frame,
    Heartbeat,
    State,
    Telemetry,
    decode_frame,
)
from iotspec.uart.mapping import (
    EVENT_CODE,
    QUALITY,
    REASON,
    RESET_REASON,
    SEVERITY,
    STATUS,
    UnknownValueError,
)

__all__ = [
    "BridgeResult",
    "CommandRejected",
    "SeqExtender",
    "UartBridge",
    "format_boot_id",
    "format_fw_version",
]

# 第7章「種別と対応」：TELEMETRY は temp_board と fan_main に対応する
TELEMETRY_CHANNEL = "temp_board"
TELEMETRY_UNIT = "degC"
TELEMETRY_ACTUATOR = "fan_main"
TEMP_SCALE = 100  # temp_c100 ÷ 100 = degC
_UART_SEQ_MAX = UART_SEQ_MODULO - 1
_NO_LEASE_TTL_S = 0  # リースを持たない op の ttl_s（open-questions #21）
APPLIED_STATUSES: Final = frozenset({"accepted", "done"})
"""cmd/ack に applied を付ける status（v0.2.2 #44）。"""
_BOOT_CODE = EVENT_CODE.to_uart("BOOT")
NOT_CONVERTED = "not_converted"
"""MQTT に変換しないフレームの破棄理由（DIAG と内部の操作の ACK。v0.2.2 #37）。"""


def format_boot_id(boot_id: int) -> str:
    """STATE の boot_id:u32 → 16進小文字8桁。"""
    if not 0 <= boot_id <= UINT32_MAX:
        raise ValueError(f"boot_id が u32 の範囲外: {boot_id}")
    return f"{boot_id:08x}"


def format_fw_version(fw_version: int) -> str:
    """fw_version:u16 → "{major}.{minor}"（上位バイト＝メジャー、下位バイト＝マイナー。#18）。"""
    if not 0 <= fw_version <= _UART_SEQ_MAX:
        raise ValueError(f"fw_version が u16 の範囲外: {fw_version}")
    return f"{fw_version >> 8}.{fw_version & 0xFF}"


class SeqExtender:
    """UART の16ビット連番を32ビットに広げる（一周ごとに65,536を加える。第7章）。

    UART は順序が入れ替わらないので、前の値より小さくなったら一周したとみなす。
    同じ値が続いたときは一周とはみなさない。再起動（新しい STATE）では reset する。
    """

    def __init__(self) -> None:
        self._last: int | None = None
        self._base = 0

    def extend(self, seq16: int) -> int:
        if not 0 <= seq16 <= _UART_SEQ_MAX:
            raise ValueError(f"seq が u16 の範囲外: {seq16}")
        if self._last is not None and seq16 < self._last:
            self._base += UART_SEQ_MODULO
        self._last = seq16
        return (self._base + seq16) & UINT32_MAX

    def reset(self) -> None:
        self._last = None
        self._base = 0


@dataclass(frozen=True)
class BridgeResult:
    """受信したフレーム1件の処理結果。

    envelopes：発行する電文（0件か1件）。discarded：破棄した理由（破棄しなければ None）。
    request_state：GET_STATUS で STATE を求めるべきとき True（再起動の検知、STATE 未受信）。
    diag：DIAG を受けたとき、復号した項目に受信時刻（received_at）と boot_id を加えたもの（#37）。
    internal_ack：内部の操作（internal_frame で送った CMD）の ACK を受けたとき、その内容。
    """

    envelopes: tuple[Envelope, ...] = ()
    discarded: str | None = None
    detail: str = ""
    request_state: bool = False
    diag: dict[str, Any] | None = None
    internal_ack: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommandRejected:
    """cmd を CMD に変換できなかった。reason は cmd/ack の reason にそのまま使える値。"""

    reason: str
    detail: str


class UartBridge:
    """1台の UART デバイス（例：mcu-c-01）と MQTT の間の変換。

    new_id を渡すと msg_id の生成を差し替えられる（ゴールデンの再現用）。
    """

    def __init__(
        self,
        profile: Profile,
        clock: Clock,
        new_id: Callable[[], str] | None = None,
        bridged_by: str | None = None,
    ) -> None:
        if profile.transport != "uart":
            raise ValueError(f"UART のプロファイルではない: {profile.device_id}")
        self.profile = profile
        self.clock = clock
        self.bridged_by = bridged_by
        """state に付ける bridged_by（変換する作品03の device_id。v0.2.1 #36）。"""
        self._new_id = new_id if new_id is not None else (lambda: new_ulid(clock))
        self.boot_id: str | None = None
        self.mode: str = UART_INITIAL_DEVICE_STATE
        self.awaiting_state = True
        """新しい STATE を待っている（起動直後、または uptime_ms の減少で再起動を検知した）。"""
        self.dropped_telemetry = 0
        """STATE 待ちの間に変換せず破棄した TELEMETRY の件数（第7章）。"""
        self.discards: Counter[str] = Counter()
        """破棄の理由ごとの件数（第5章：破棄や置き換えの件数は状態として公開する）。"""
        self._rx: dict[int, SeqExtender] = {}
        self._rx_last: dict[int, int] = {}
        self.uart_gaps: Counter[str] = Counter()
        """受信した UART フレームの種別ごとの欠番の数（HEARTBEAT を含む）。"""
        self._last_uptime: int | None = None
        self._reboot_detected = False
        self._state_requested = False
        self._tx_seq: dict[int, int] = {}
        self._period_ms: int | None = None
        self._fw_version = "0.0"
        self._cmd_table: dict[int, tuple[str, int]] = {}
        self._internal_refs: dict[int, int] = {}
        """内部の操作の cmd_ref → UART の op（ACK を cmd/ack にしない。#37）。"""

    # ------------------------------------------------------------ 受信（01 → 03）

    def receive_bytes(self, data: bytes) -> BridgeResult:
        """符号化済みのフレーム1件を処理する。壊れたフレームは破棄（理由つき）。"""
        frame = decode_frame(data)
        if isinstance(frame, Discarded):
            return self._discard(frame.reason, frame.detail)
        return self.receive(frame)

    def receive(self, frame: Frame) -> BridgeResult:
        """復号済みのフレーム1件を処理する。"""
        if isinstance(frame, Cmd):
            return self._discard("unexpected_direction", "CMD は 03 → 01 の向き")
        if isinstance(frame, State):
            return self._on_state(frame)

        request = False
        if isinstance(frame, Telemetry | Event | Heartbeat | Diag):
            if self._last_uptime is not None and frame.uptime_ms < self._last_uptime:
                self.awaiting_state = True
                self._reboot_detected = True
                self._state_requested = False
            self._last_uptime = frame.uptime_ms
        if self.awaiting_state and not self._state_requested:
            self._state_requested = True
            request = True
        seq = self._extend(frame.TYPE, type(frame).__name__.upper(), frame.seq)

        if isinstance(frame, Heartbeat):
            return BridgeResult(request_state=request)
        if self.awaiting_state:
            if isinstance(frame, Telemetry):
                self.dropped_telemetry += 1
            return self._discard("awaiting_state", type(frame).__name__, request)
        if isinstance(frame, Diag):
            return self._discard(NOT_CONVERTED, "DIAG", request, diag=self._diag(frame))
        if isinstance(frame, Ack) and frame.cmd_ref in self._internal_refs:
            return self._internal_ack(frame, request)
        try:
            if isinstance(frame, Telemetry):
                envelope = self._telemetry(frame, seq)
            elif isinstance(frame, Event):
                envelope = self._event(frame, seq)
            else:
                lookup = self._cmd_table.get(frame.cmd_ref)
                if lookup is None:
                    return self._discard("unknown_cmd_ref", f"cmd_ref {frame.cmd_ref}", request)
                envelope = self._ack(frame, seq, *lookup)
        except (UnknownValueError, KeyError) as exc:
            return self._discard("unknown_value", str(exc), request)
        return self._emit(envelope, request)

    def _on_state(self, frame: State) -> BridgeResult:
        boot_id = format_boot_id(frame.boot_id)
        if boot_id != self.boot_id or self._reboot_detected:
            self._rx.clear()
            self._rx_last.clear()
            self.mode = UART_INITIAL_DEVICE_STATE
        self.boot_id = boot_id
        self.awaiting_state = False
        self._reboot_detected = False
        self._state_requested = False
        self._last_uptime = None
        seq = self._extend(frame.TYPE, "STATE", frame.seq)
        self._period_ms = frame.period_ms
        self._fw_version = format_fw_version(frame.fw_version)
        payload = self._state_payload("online")
        return self._emit(self._envelope(KIND_STATE, seq, payload, None), False)

    def _state_payload(self, status: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "mode": self.mode,
            "channels": [
                {"channel": c.channel, "unit": c.unit, "period_ms": self._period_ms or c.period_ms}
                for c in self.profile.channels
            ],
            "actuators": [
                {"id": a.id, "min": a.min, "max": a.max, "max_on_s": a.max_on_s}
                for a in self.profile.actuators
            ],
            "fw_version": self._fw_version,
        }
        if self.bridged_by is not None:
            payload["bridged_by"] = self.bridged_by
        return payload

    def offline_state(self) -> Envelope | None:
        """UART デバイスの HEARTBEAT が途絶えたときに作品03が発行する offline の state（#36）。

        retain で発行する。seq は進めず次の値を入れる（#29：受信側は seq の判定から外す）。
        STATE をまだ受けていない（boot_id が分からない）ときは None。
        """
        if self.boot_id is None:
            return None
        seq = self._rx_last.get(State.TYPE, -1) + 1
        envelope = self._envelope(
            KIND_STATE, seq & UINT32_MAX, self._state_payload("offline"), None
        )
        return envelope if not validate(envelope.to_dict()) else None

    def _telemetry(self, frame: Telemetry, seq: int) -> Envelope:
        device_state = self.profile.state_name(frame.state)
        payload = {
            "readings": [
                {
                    "channel": TELEMETRY_CHANNEL,
                    "value": frame.temp_c100 / TEMP_SCALE,
                    "unit": TELEMETRY_UNIT,
                    "quality": QUALITY.to_common(frame.quality),
                }
            ],
            "device_state": device_state,
            "actuators": [{"id": TELEMETRY_ACTUATOR, "level": frame.fan_pm}],
        }
        self.mode = device_state
        return self._envelope(KIND_TELEMETRY, seq, payload, frame.uptime_ms)

    def _event(self, frame: Event, seq: int) -> Envelope:
        if frame.code == _BOOT_CODE:
            # BOOT に限り from は reset_reason、to は 0（v0.2.2 #38）
            if frame.to_state != UART_BOOT_EVENT_TO:
                raise UnknownValueError(
                    f"BOOT の to が {UART_BOOT_EVENT_TO} でない: {frame.to_state}"
                )
            detail: dict[str, Any] = {"reset_reason": RESET_REASON.to_common(frame.from_state)}
        else:
            detail = {
                "from_state": self.profile.state_name(frame.from_state),
                "to_state": self.profile.state_name(frame.to_state),
            }
        payload = {
            "code": EVENT_CODE.to_common(frame.code),
            "severity": SEVERITY.to_common(frame.severity),
            "channel": TELEMETRY_CHANNEL,
            "value": frame.temp_c100 / TEMP_SCALE,
            "detail": detail,
        }
        return self._envelope(KIND_EVENT, seq, payload, frame.uptime_ms)

    def _diag(self, frame: Diag) -> dict[str, Any]:
        """DIAG の項目（復号した値そのまま）に受信時刻と boot_id を加える（#37）。"""
        return {
            "received_at": format_ts(self.clock.now()),
            "boot_id": self.boot_id,
            "uptime_ms": frame.uptime_ms,
            "reset_reason": frame.reset_reason,
            "reset_count": frame.reset_count,
            "rx_crc_errors": frame.rx_crc_errors,
            "rx_frame_errors": frame.rx_frame_errors,
            "max_loop_us": frame.max_loop_us,
            "overruns": frame.overruns,
        }

    def _internal_ack(self, frame: Ack, request: bool) -> BridgeResult:
        """内部の操作の ACK：cmd/ack にせず not_converted で数え、内容を返す（#37）。"""
        op = self._internal_refs[frame.cmd_ref]
        try:
            ack = {
                "cmd_ref": frame.cmd_ref,
                "op": UART_OP_NAMES.get(op, f"0x{op:02x}"),
                "status": STATUS.to_common(frame.status),
                "reason": REASON.to_common(frame.reason),
                "limited": frame.limited,
                "applied": frame.applied,
            }
        except UnknownValueError as exc:
            return self._discard("unknown_value", str(exc), request)
        return self._discard(NOT_CONVERTED, f"{ack['op']} の ACK", request, internal_ack=ack)

    def _ack(self, frame: Ack, seq: int, cmd_id: str, uart_op: int) -> Envelope:
        if frame.limited not in (0, 1):
            raise UnknownValueError(f"limited: 0/1 以外の値 {frame.limited}")
        payload: dict[str, Any] = {
            "cmd_id": cmd_id,
            "status": STATUS.to_common(frame.status),
            "limited": bool(frame.limited),
            "reason": REASON.to_common(frame.reason),
        }
        # applied は出力を扱う op で、指令を受け付けた・実行したときだけ付ける（#19、v0.2.2 #44）。
        # rejected・failed では ACK の applied の値によらず付けない
        # （受信側に「出力が0になった」と読まれないため）
        if uart_op == UART_OP_SET_FAN_OVERRIDE and payload["status"] in APPLIED_STATUSES:
            payload["applied"] = {"level": frame.applied}
        return self._envelope(KIND_CMD_ACK, seq, payload, None)

    def _envelope(
        self, kind: str, seq: int, payload: dict[str, Any], uptime_ms: int | None
    ) -> Envelope:
        assert self.boot_id is not None
        return Envelope(
            schema=SCHEMA_NAMES[kind],
            msg_id=self._new_id(),
            device_id=self.profile.device_id,
            seq=seq,
            boot_id=self.boot_id,
            ts=self.clock.now(),
            ts_source="gateway",
            payload=payload,
            uptime_ms=uptime_ms,
        )

    def _extend(self, frame_type: int, name: str, seq16: int) -> int:
        """種別ごとの連番を広げ、欠番を数える。"""
        extended = self._rx.setdefault(frame_type, SeqExtender()).extend(seq16)
        last = self._rx_last.get(frame_type)
        if last is not None and extended > last + 1:
            self.uart_gaps[name] += extended - last - 1
        self._rx_last[frame_type] = extended
        return extended

    def _emit(self, envelope: Envelope, request: bool) -> BridgeResult:
        errors = validate(envelope.to_dict())
        if errors:
            return self._discard("schema_invalid", "; ".join(errors), request)
        return BridgeResult(envelopes=(envelope,), request_state=request)

    def _discard(
        self,
        reason: str,
        detail: str,
        request: bool = False,
        *,
        diag: dict[str, Any] | None = None,
        internal_ack: dict[str, Any] | None = None,
    ) -> BridgeResult:
        self.discards[reason] += 1
        return BridgeResult(
            discarded=reason,
            detail=detail,
            request_state=request,
            diag=diag,
            internal_ack=internal_ack,
        )

    # ------------------------------------------------------------ 送信（03 → 01）

    def next_tx_seq(self, frame_type: int) -> int:
        """03 → 01 の向きの、フレーム種別ごとの UART の連番（u16、一周したら0に戻る。#25）。"""
        seq = self._tx_seq.get(frame_type, 0)
        self._tx_seq[frame_type] = (seq + 1) % UART_SEQ_MODULO
        return seq

    def command_to_frame(self, cmd: Mapping[str, Any], cmd_ref: int) -> Cmd | CommandRejected:
        """cmd の payload を CMD に変換し、cmd_ref ⇔ cmd_id の対応を記録する。

        対応する指令がプロファイルにない・引数が足りない・lease_s が不正なら CommandRejected。
        """
        if not 0 <= cmd_ref <= _UART_SEQ_MAX:
            raise ValueError(f"cmd_ref が u16 の範囲外: {cmd_ref}")
        op = cmd.get("op")
        args = cmd.get("args") or {}
        target = args.get("id") if isinstance(args, Mapping) else None
        spec = self.profile.find_command(str(op), target)
        if spec is None:
            known_op = any(c.op == op for c in self.profile.commands)
            reason = "unknown_target" if known_op else "unknown_op"
            return CommandRejected(reason, f"op={op!r} target={target!r}")
        try:
            fields = dict(spec.args)
            values = tuple(int(args[fields[name]]) for name in CMD_ARG_NAMES[spec.uart_op])
        except (KeyError, TypeError, ValueError) as exc:
            return CommandRejected("invalid_arg", f"args: {exc}")
        ttl_s = _NO_LEASE_TTL_S
        if spec.ttl_from_lease:
            try:
                lease_s = resolve_lease_s(cmd)
            except InvalidLeaseError as exc:
                return CommandRejected("invalid_arg", str(exc))
            ttl_s = lease_s if lease_s is not None else _NO_LEASE_TTL_S
        frame = Cmd(self.next_tx_seq(Cmd.TYPE), cmd_ref, spec.uart_op, ttl_s, values)
        self._cmd_table[cmd_ref] = (str(cmd["cmd_id"]), spec.uart_op)
        self._internal_refs.pop(cmd_ref, None)
        return frame

    def internal_frame(self, op: int, cmd_ref: int, args: tuple[int, ...] = ()) -> Cmd:
        """作品03の内部の操作（汎用指令との対応がない GET_DIAG・SET_THRESHOLD）の CMD を作る。

        cmd_ref を記憶し、その ACK は cmd/ack に変換しない（not_converted。v0.2.2 #37）。
        リースを持たない op なので ttl_s は 0（#21）。
        """
        if not 0 <= cmd_ref <= _UART_SEQ_MAX:
            raise ValueError(f"cmd_ref が u16 の範囲外: {cmd_ref}")
        if op == UART_OP_SET_FAN_OVERRIDE or op not in CMD_ARGS_FORMAT:
            raise ValueError(f"内部の操作にできない op: 0x{op:02x}")
        if len(args) != len(CMD_ARG_NAMES[op]):
            raise ValueError(f"op 0x{op:02x} の args は {len(CMD_ARG_NAMES[op])} 個")
        frame = Cmd(self.next_tx_seq(Cmd.TYPE), cmd_ref, op, _NO_LEASE_TTL_S, tuple(args))
        self._internal_refs[cmd_ref] = op
        self._cmd_table.pop(cmd_ref, None)
        return frame

    def heartbeat_frame(self, uptime_ms: int) -> Heartbeat:
        """03 から 01 への HEARTBEAT。"""
        return Heartbeat(self.next_tx_seq(Heartbeat.TYPE), uptime_ms)
