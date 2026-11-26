"""UART ゴールデンフレームを生成する（第7章「値の対応表」の後段、uart/golden/）。

使い方:
    uv run python tools/gen_golden.py           # uart/golden/ に書き出す
    uv run python tools/gen_golden.py --check   # 書き出さずに差分を調べる（差分ありで1）

UART の seq は方向・フレーム種別ごとの連番（v0.2.1 #25）。
ゴールデンは1本のセッション（order の順）として作る。受信側（01 → 03）のフレームは、
直前までのフレームを順に UartBridge に通した状態で変換した結果を expected に持つ。
時刻は固定の VirtualClock（1フレームごとに 500ms 進める）、msg_id は固定の規則で作るので、
再生成しても差分は出ない。

各ゴールデンは2つのファイルからなる。
    {order:02d}_{name}.hex   符号化済みフレーム（末尾の 0x00 を含む）のスペース区切り16進
    {order:02d}_{name}.json  フレームのフィールド値と、変換後の期待エンベロープ
                             （03 → 01 は入力の cmd と期待する CMD）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ulid import ULID

from iotspec.clock import VirtualClock, format_ts
from iotspec.constants import UART_OP_GET_DIAG, UART_OP_NAMES
from iotspec.profile import load_profile
from iotspec.uart.bridge import CommandRejected, UartBridge
from iotspec.uart.frames import (
    Ack,
    Cmd,
    Diag,
    Event,
    Frame,
    Heartbeat,
    State,
    Telemetry,
    encode_frame,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "uart" / "golden"
PROFILE_ID = "mcu-c-01"
BRIDGED_BY = "gw-01"
"""変換する作品03の device_id（state の bridged_by。v0.2.1 #36）。"""
START = datetime(2026, 11, 20, 6, 0, 0, tzinfo=UTC)
STEP = timedelta(milliseconds=500)


@dataclass
class Golden:
    name: str
    description: str
    direction: str
    frame: Frame
    data: bytes
    extra: dict[str, Any] = field(default_factory=dict)


def _frame_fields(frame: Frame) -> dict[str, Any]:
    fields: dict[str, Any] = {"type": type(frame).__name__.upper(), "type_code": frame.TYPE}
    values = asdict(frame)
    if isinstance(frame, Cmd):
        values["op_name"] = UART_OP_NAMES.get(frame.op)
        values["args"] = list(frame.args)
        values["raw_args"] = frame.raw_args.hex(" ")
    fields.update(values)
    return fields


def _hex(data: bytes) -> str:
    return data.hex(" ")


class _Session:
    def __init__(self) -> None:
        self.clock = VirtualClock(START)
        self._counter = 0
        self.bridge = UartBridge(
            load_profile(PROFILE_ID), self.clock, new_id=self._new_id, bridged_by=BRIDGED_BY
        )
        self.goldens: list[Golden] = []

    def _new_id(self) -> str:
        """固定の msg_id：時刻部＝現在の仮想時刻、乱数部＝通し番号。"""
        self._counter += 1
        millis = (self.clock.now() - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
        return str(ULID.from_bytes(millis.to_bytes(6, "big") + self._counter.to_bytes(10, "big")))

    def rx(self, name: str, description: str, frame: Frame, *, corrupt: bool = False) -> None:
        """01 → 03 のフレームを受信させ、期待結果を記録する。"""
        self.clock.advance(STEP)
        data = encode_frame(frame)
        if corrupt:
            # CRC の下位バイトを反転させた壊れたフレーム（COBS の符号化はやり直す）
            from iotspec.uart import cobs

            raw = bytearray(cobs.decode(data[:-1]))
            raw[-2] ^= 0xFF
            data = cobs.encode(bytes(raw)) + b"\x00"
        result = self.bridge.receive_bytes(data)
        expected = {
            "envelope": result.envelopes[0].to_dict() if result.envelopes else None,
            "discarded": result.discarded,
            "request_state": result.request_state,
            "dropped_telemetry_total": self.bridge.dropped_telemetry,
        }
        if result.diag is not None:
            expected["diag"] = result.diag
        extra = {"received_at": format_ts(self.clock.now()), "expected": expected}
        self.goldens.append(Golden(name, description, "01->03", frame, data, extra))

    def tx_cmd(self, name: str, description: str, cmd: dict[str, Any], cmd_ref: int) -> None:
        """03 → 01：cmd の payload を CMD に変換する。"""
        self.clock.advance(STEP)
        frame = self.bridge.command_to_frame(cmd, cmd_ref)
        if isinstance(frame, CommandRejected):
            raise RuntimeError(f"{name}: 変換できない {frame}")
        extra = {"input_cmd_payload": cmd, "cmd_ref": cmd_ref}
        self.goldens.append(Golden(name, description, "03->01", frame, encode_frame(frame), extra))

    def tx_internal(self, name: str, description: str, op: int, cmd_ref: int) -> None:
        """03 → 01：作品03の内部の操作（GET_DIAG など）。ACK は cmd/ack にしない。"""
        self.clock.advance(STEP)
        frame = self.bridge.internal_frame(op, cmd_ref)
        extra = {"internal_op": UART_OP_NAMES[op], "cmd_ref": cmd_ref}
        self.goldens.append(Golden(name, description, "03->01", frame, encode_frame(frame), extra))

    def tx_raw(self, name: str, description: str, frame: Frame) -> None:
        """03 → 01：汎用指令との対応がない CMD・HEARTBEAT（符号化だけを照合する）。"""
        self.clock.advance(STEP)
        self.goldens.append(Golden(name, description, "03->01", frame, encode_frame(frame)))


def build_session() -> list[Golden]:
    s = _Session()
    b = s.bridge
    cmd_fan = "01JD3R0A5K7M2N4P6R8T0V2X4Y"

    # 03 → 01：指令（ACK の照合に使う cmd_ref を先に登録する）
    s.tx_cmd(
        "cmd_set_fan_override",
        "set_actuator fan_main level 800 lease_s 60 → SET_FAN_OVERRIDE duty_pm 800 ttl_s 60",
        {
            "cmd_id": cmd_fan,
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 800},
            "priority": "auto",
            "lease_s": 60,
        },
        0x1234,
    )
    s.tx_cmd(
        "cmd_set_fan_override_max",
        "境界値：level 1000、lease_s 600、cmd_ref 0xFFFF",
        {
            "cmd_id": "01JD3R0B5K7M2N4P6R8T0V2X4Y",
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 1000},
            "priority": "manual",
            "lease_s": 600,
        },
        0xFFFF,
    )
    s.tx_cmd(
        "cmd_set_fan_override_lease_omitted",
        "境界値：level 0、lease_s 省略 → ttl_s 60（既定値）",
        {
            "cmd_id": "01JD3R0C5K7M2N4P6R8T0V2X4Y",
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 0},
            "priority": "auto",
        },
        0x0000,
    )
    s.tx_cmd(
        "cmd_clear_override",
        "release fan_main → CLEAR_OVERRIDE（引数なし、ttl_s 0）",
        {
            "cmd_id": "01JD3R0D5K7M2N4P6R8T0V2X4Y",
            "op": "release",
            "args": {"id": "fan_main"},
            "priority": "auto",
        },
        0x0001,
    )
    s.tx_cmd(
        "cmd_get_status",
        "get_status → GET_STATUS（引数なし、ttl_s 0）",
        {"cmd_id": "01JD3R0E5K7M2N4P6R8T0V2X4Y", "op": "get_status", "priority": "auto"},
        0x0002,
    )
    s.tx_raw(
        "cmd_set_threshold",
        "SET_THRESHOLD warn 35.00℃ crit 45.00℃ hyst 2.00℃（汎用指令との対応は未定。符号化のみ）",
        Cmd(b.next_tx_seq(Cmd.TYPE), 0x0003, 0x03, 0, (3500, 4500, 200)),
    )
    s.tx_raw(
        "cmd_set_threshold_negative",
        "SET_THRESHOLD の i16 の負の値と最小値（符号化のみ）",
        Cmd(b.next_tx_seq(Cmd.TYPE), 0x0004, 0x03, 0, (-32768, -1000, 0)),
    )
    s.tx_raw(
        "cmd_get_diag",
        "GET_DIAG（引数なし。汎用指令との対応はない。符号化のみ）",
        Cmd(b.next_tx_seq(Cmd.TYPE), 0x0005, 0x11, 0),
    )
    s.tx_raw("heartbeat_tx", "03 → 01 の HEARTBEAT", b.heartbeat_frame(123_456))

    # 01 → 03：起動から
    s.rx(
        "telemetry_before_state",
        "STATE より前の TELEMETRY：変換せず破棄し、STATE を求める",
        Telemetry(0, 900, 2500, 0, 0, 0),
    )
    s.rx(
        "state_boot",
        "起動後の STATE：fw_version 0x0102 → '1.2'、boot_id → '7f3a9c01'、mode は INIT",
        State(0, 0x0102, 0x7F3A9C01, 1000),
    )
    s.rx(
        "telemetry_normal",
        "TELEMETRY：25.12℃、good、NORMAL、fan 0",
        Telemetry(1, 1000, 2512, 0, 1, 0),
    )
    s.rx(
        "event_threshold_exceeded",
        "EVENT：THRESHOLD_EXCEEDED critical、NORMAL → CRITICAL",
        Event(0, 1500, 3, 2, 1, 3, 4500),
    )
    s.rx("heartbeat_rx", "01 → 03 の HEARTBEAT（電文にはしない）", Heartbeat(0, 2000))
    s.rx(
        "state_periodic",
        "60秒ごとの STATE（同じ boot_id）：mode は直近の TELEMETRY の状態名、"
        "period_ms は STATE の値",
        State(1, 0x0102, 0x7F3A9C01, 500),
    )
    s.rx(
        "ack_done_limited",
        "ACK done、limited あり、applied 1000 → cmd/ack の applied {level: 1000}",
        Ack(0, 0x1234, 1, 0, 1, 1000),
    )
    s.rx(
        "ack_rejected_invalid_arg",
        "ACK rejected invalid_arg（ttl_s 範囲外の SET_FAN_OVERRIDE、v0.2.2 #40）、limited なし。"
        "rejected なので cmd/ack に applied を付けない（v0.2.2 #44）",
        Ack(1, 0xFFFF, 2, 2, 0, 0),
    )
    s.rx(
        "telemetry_fault_min_temp",
        "境界値：temp_c100 -32768（-327.68℃）、fault、SENSOR_FAULT、fan 1000、"
        "seq 65535（欠番あり）",
        Telemetry(65535, 3000, -32768, 2, 4, 1000),
    )
    s.rx(
        "telemetry_seq_wrapped",
        "seq の一周：UART の seq 0 → エンベロープの seq 65536。temp_c100 32767、stale",
        Telemetry(0, 4000, 32767, 1, 2, 500),
    )
    s.rx(
        "event_sensor_fault",
        "EVENT：SENSOR_FAULT critical、WARNING → SENSOR_FAULT（EVENT の連番は TELEMETRY と別で 1）",
        Event(1, 4500, 5, 2, 2, 4, -32768),
    )
    s.rx(
        "event_upstream_restored",
        "EVENT：UPSTREAM_RESTORED（code 8、v0.2.1 #34）info",
        Event(2, 4800, 8, 0, 2, 2, 2500),
    )
    s.rx(
        "heartbeat_rx_max_uptime",
        "境界値：uptime_ms 0xFFFFFFFF",
        Heartbeat(1, 0xFFFFFFFF),
    )
    s.rx(
        "telemetry_crc_mismatch",
        "壊れたフレーム：CRC 不一致 → 破棄",
        Telemetry(1, 0xFFFFFFFF, 2600, 0, 1, 0),
        corrupt=True,
    )
    s.rx(
        "telemetry_after_reboot",
        "uptime_ms の減少 → 再起動と判定。STATE を求め、TELEMETRY は変換せず破棄",
        Telemetry(0, 50, 2400, 0, 0, 0),
    )
    s.rx(
        "telemetry_awaiting_state",
        "STATE 待ちの TELEMETRY も破棄（破棄件数を数える）",
        Telemetry(1, 1050, 2410, 0, 1, 0),
    )
    s.rx(
        "state_after_reboot",
        "再起動後の STATE：boot_id '00000001'、fw_version 0x0103 → '1.3'、種別ごとの連番は振り直し",
        State(0, 0x0103, 0x00000001, 2000),
    )
    s.rx(
        "telemetry_new_boot",
        "新しい boot_id での TELEMETRY",
        Telemetry(2, 2050, 2420, 0, 1, 250),
    )

    # v0.2.2：28番以降（同じセッションの続き）
    s.tx_cmd(
        "cmd_set_fan_override_ttl_max",
        "境界値：lease_s 600 → ttl_s 600（上限。601 は作品01が rejected invalid_arg。v0.2.2 #40）",
        {
            "cmd_id": "01JD3R0F5K7M2N4P6R8T0V2X4Y",
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 300},
            "priority": "safety",
            "lease_s": 600,
        },
        0x0006,
    )
    s.tx_internal(
        "cmd_get_diag_internal",
        "内部の操作として GET_DIAG（cmd_ref 0x0007、ACK は cmd/ack にしない。v0.2.2 #37）",
        UART_OP_GET_DIAG,
        0x0007,
    )
    s.rx(
        "diag",
        "DIAG（v0.2.2 #37）：変換せず not_converted。expected.diag に項目・受信時刻・boot_id",
        Diag(0, 2600, 3, 12, 4, 0xFFFF, 850, 0),
    )
    s.rx(
        "ack_get_diag_done",
        "GET_DIAG の ACK done（DIAG の後）：内部の操作なので cmd/ack にせず not_converted",
        Ack(2, 0x0007, 1, 0, 0, 0),
    )
    s.rx(
        "state_reboot_watchdog",
        "ウォッチドッグによる再起動後の STATE：boot_id '00000002'、種別ごとの連番は振り直し",
        State(0, 0x0103, 0x00000002, 2000),
    )
    s.rx(
        "event_boot_watchdog",
        "STATE の直後の BOOT：from = reset_reason 3（watchdog）、to = 0 → "
        "detail は {reset_reason: watchdog} だけ（v0.2.2 #38）",
        Event(0, 120, 1, 0, 3, 0, 2400),
    )
    s.rx(
        "event_interlock_active",
        "EVENT：INTERLOCK_ACTIVE（code 9、v0.2.2 #39）warning、from・to は同じ WARNING",
        Event(1, 1500, 9, 1, 2, 2, 4620),
    )
    s.rx(
        "event_interlock_released",
        "EVENT：INTERLOCK_RELEASED（code 10）info、from・to は同じ NORMAL",
        Event(2, 2500, 10, 0, 1, 1, 4180),
    )
    return s.goldens


def render(goldens: list[Golden]) -> dict[str, str]:
    """ファイル名 → 内容。"""
    files: dict[str, str] = {}
    for order, g in enumerate(goldens, start=1):
        stem = f"{order:02d}_{g.name}"
        body: dict[str, Any] = {
            "name": g.name,
            "order": order,
            "direction": g.direction,
            "description": g.description,
            "frame": _frame_fields(g.frame),
            "hex": _hex(g.data),
        }
        body.update(g.extra)
        files[f"{stem}.hex"] = _hex(g.data) + "\n"
        files[f"{stem}.json"] = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UART ゴールデンフレームを生成する")
    parser.add_argument("--check", action="store_true", help="書き出さずに差分を調べる")
    parser.add_argument("--out", type=Path, default=GOLDEN_DIR)
    args = parser.parse_args(argv)
    files = render(build_session())
    out: Path = args.out
    existing = {p.name for p in out.glob("*.hex")} | {p.name for p in out.glob("*.json")}
    if args.check:
        differ = sorted(
            name
            for name, text in files.items()
            if not (out / name).is_file() or (out / name).read_text(encoding="utf-8") != text
        )
        stale = sorted(existing - set(files))
        for name in differ:
            print(f"差分あり: {name}")
        for name in stale:
            print(f"不要なファイル: {name}")
        return 1 if differ or stale else 0
    out.mkdir(parents=True, exist_ok=True)
    for name in existing - set(files):
        (out / name).unlink()
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    print(f"{len(files) // 2} 件のゴールデンを {out} に書き出した")
    return 0


if __name__ == "__main__":
    sys.exit(main())
