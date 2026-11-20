"""role=gw-01（作品03）のチェック。

相手役は mcu-c-01（UART、契約テスト自身）と app-01（MQTT の request）。

対象（作品03）は、契約テストが作った pty（または --uart のポート）を
作品01のつながる UART として開き、
ブローカーに gw-01 として接続している前提。ゴールデンの照合は、作品01との UART のやり取りを
まだしていない（起動したての）対象に対して最初に行う。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from pathlib import Path
from typing import Any

from iotspec.clock import SystemClock
from iotspec.constants import (
    DEVICE_APP_01,
    DEVICE_MCU_C_01,
    KIND_REQUEST,
    KIND_REQUEST_ACK,
    KIND_STATE,
    UART_ACK_TIMEOUT_S,
    UART_CMD_MAX_IN_FLIGHT,
    UART_CMD_MAX_RETRIES,
    UART_CMD_RATE_PER_S,
    UART_HEARTBEAT_LOSS_S,
)
from iotspec.contract.context import ContractContext, Observed, UartPeer
from iotspec.contract.core import Checker, Skip, check
from iotspec.ids import new_ulid
from iotspec.uart.frames import Ack, Cmd, Frame, State

ROLE = "gw-01"
GOLDEN_DIR = Path(__file__).resolve().parents[4] / "uart" / "golden"
GOLDEN_GAP_S = 0.3
SETTLE_S = 1.5
HOLD_ACKS_S = 6.0
REQUESTS = 6
FINISH_WAIT_S = 20.0
OFFLINE_MARGIN_S = 3.0
_TIMING_TOLERANCE_S = 0.25
"""pty とイベントループの揺らぎを見込んだ時間の許容。"""


def _golden_dir() -> Path:
    bundled = Path(__file__).resolve().parents[2] / "_golden"
    return bundled if bundled.is_dir() else GOLDEN_DIR


def goldens() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(_golden_dir().glob("*.json"))]


class Device:
    """作品01の役：CMD に ACK を返す（保留もできる）。直近の cmd_ref には前回の ACK を返す。"""

    def __init__(self, peer: UartPeer) -> None:
        self.peer = peer
        self.hold = False
        self.cmds: list[tuple[float, Cmd]] = []
        self._acks: dict[int, tuple[int, int, int, int]] = {}
        peer.on_frame = self._on_frame

    def _on_frame(self, frame: Frame) -> None:
        if not isinstance(frame, Cmd):
            return
        self.cmds.append((time.monotonic(), frame))
        if not self.hold:
            self.ack(frame)

    def ack(self, frame: Cmd) -> None:
        fields = self._acks.get(frame.cmd_ref)
        if fields is None:
            applied = frame.args[0] if frame.args else 0
            fields = (1, 0, 0, applied)
            self._acks[frame.cmd_ref] = fields
        self.peer.send(Ack(self.peer.next_seq(Ack.TYPE), frame.cmd_ref, *fields))

    def release(self) -> None:
        self.hold = False
        latest: dict[int, Cmd] = {}
        for _, cmd in self.cmds:
            latest[cmd.cmd_ref] = cmd
        for cmd in latest.values():
            self.ack(cmd)


def device(ctx: ContractContext) -> Device:
    if ctx.uart is None:
        raise Skip("仮想シリアルがない")
    existing = ctx.options.get("device")
    if existing is None:
        existing = ctx.options["device"] = Device(ctx.uart)
    assert isinstance(existing, Device)
    return existing


def comparable(message: dict[str, Any]) -> dict[str, Any]:
    """照合に使う項目（msg_id と ts は対象の採番・受信時刻なので除く）。"""
    keep = ("schema", "device_id", "seq", "boot_id", "ts_source", "uptime_ms", "payload")
    return {k: message.get(k) for k in keep}


@check(
    ROLE,
    "golden_conversion",
    "UART のゴールデンフレームを順に送ると、期待どおりのエンベロープに変換される",
    order=5,
)
async def golden_conversion(ctx: ContractContext, c: Checker) -> None:
    dev = device(ctx)
    rx = [g for g in goldens() if g["direction"] == "01->03" and g["frame"]["type"] != "ACK"]
    if not rx:
        raise Skip("ゴールデンが見つからない")
    started = time.monotonic()
    for g in rx:
        dev.peer.send_raw(bytes.fromhex(g["hex"]))
        await asyncio.sleep(GOLDEN_GAP_S)
    await asyncio.sleep(SETTLE_S)
    expected = [g for g in rx if g["expected"]["envelope"] is not None]
    observed = [
        o
        for o in ctx.observer.select(device_id=DEVICE_MCU_C_01, since=started)
        if o.kind != "cmd/ack"
        and o.message is not None
        and not o.retain
        and o.message.get("payload", {}).get("status") != "offline"
    ]
    c.expect(
        len(observed) == len(expected),
        "変換された電文の数が違う",
        expected=f"{len(expected)}件（{', '.join(g['name'] for g in expected)}）",
        actual=f"{len(observed)}件",
    )
    for g, o in zip(expected, observed, strict=False):
        assert o.message is not None
        want, got = comparable(g["expected"]["envelope"]), comparable(o.message)
        for key in want:
            c.expect(
                want[key] == got[key],
                f"ゴールデン {g['name']} の {key} が違う",
                ref=o.ref(),
                expected=want[key],
                actual=got[key],
            )
        c.expect(
            o.message.get("ts_source") == "gateway", "ts_source が gateway でない", ref=o.ref()
        )
    # STATE より前・再起動の検知のあとは GET_STATUS で STATE を求める（第7章）
    asked = [cmd for at, cmd in dev.cmds if at >= started and cmd.op == 0x10]
    c.expect(
        bool(asked), "STATE 待ちになっても GET_STATUS を送ってこない", expected="GET_STATUS の CMD"
    )
    # 以後の相手役のフレームは、ゴールデンの最後の起動の続きとして送る
    dev.peer.reset_uptime(3000)
    dev.peer.set_next_seq(State.TYPE, 1)
    ctx.options["golden_done"] = True


async def _requests(ctx: ContractContext, count: int, base_level: int) -> list[dict[str, Any]]:
    sent = []
    for i in range(count):
        payload = {
            "request_id": new_ulid(SystemClock()),
            "kind": "manual",
            "target": DEVICE_MCU_C_01,
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": base_level + i * 10},
            "lease_s": 30,
        }
        await ctx.publish_as(DEVICE_APP_01, KIND_REQUEST, payload, topic_device=DEVICE_APP_01)
        sent.append(payload)
    return sent


def _acks_for(ctx: ContractContext, request_id: str, since: float) -> list[Observed]:
    return [
        o
        for o in ctx.observer.select(kind=KIND_REQUEST_ACK, since=since)
        if o.message is not None and o.message.get("payload", {}).get("request_id") == request_id
    ]


@check(
    ROLE,
    "uart_cmd_limits",
    f"作品01への CMD は毎秒{UART_CMD_RATE_PER_S}件（再送を含む）・"
    f"未完了{UART_CMD_MAX_IN_FLIGHT}件まで、"
    f"ACK が{UART_ACK_TIMEOUT_S}秒来なければ同じ cmd_ref で最大{UART_CMD_MAX_RETRIES}回再送",
    order=20,
)
async def uart_cmd_limits(ctx: ContractContext, c: Checker) -> None:
    dev = device(ctx)
    dev.peer.start_heartbeat()
    dev.peer.send(State(dev.peer.next_seq(State.TYPE), 0x0103, 0x00000001, 2000))
    await asyncio.sleep(SETTLE_S)
    dev.hold = True
    started = time.monotonic()
    sent = await _requests(ctx, REQUESTS, 100)
    await asyncio.sleep(HOLD_ACKS_S)
    held = [(at, cmd) for at, cmd in dev.cmds if at >= started]
    dev.release()
    c.expect(bool(held), "request を送っても CMD が来ない", expected="SET_FAN_OVERRIDE の CMD")
    # 毎秒1件（再送を含む）
    for (a, _), (b, cmd) in itertools.pairwise(held):
        c.expect(
            b - a >= 1 / UART_CMD_RATE_PER_S - _TIMING_TOLERANCE_S,
            "CMD の間隔が毎秒1件の制限より短い",
            ref=f"cmd_ref={cmd.cmd_ref}",
            expected=f"{1 / UART_CMD_RATE_PER_S:g}秒以上",
            actual=f"{b - a:.2f}秒",
        )
    # 未完了は4件まで（ACK を保留している間に出てきた cmd_ref の数）
    refs = {cmd.cmd_ref for _, cmd in held}
    c.expect(
        len(refs) <= UART_CMD_MAX_IN_FLIGHT,
        "未完了の指令が上限を超えた",
        expected=f"{UART_CMD_MAX_IN_FLIGHT}件以下",
        actual=len(refs),
    )
    # 再送は同じ cmd_ref（同じ指令が別の cmd_ref で送られていないか）
    by_content: dict[tuple[int, tuple[int, ...]], set[int]] = {}
    for _, cmd in held:
        by_content.setdefault((cmd.op, cmd.args), set()).add(cmd.cmd_ref)
    for (op, args), content_refs in by_content.items():
        c.expect(
            len(content_refs) == 1,
            "同じ指令の再送で cmd_ref が変わった",
            ref=f"op=0x{op:02x} args={args}",
            expected="1つの cmd_ref",
            actual=sorted(content_refs),
        )
    resent = [cmd for _, cmd in held if sum(1 for _, x in held if x.cmd_ref == cmd.cmd_ref) > 1]
    c.expect(
        bool(resent), "ACK を返さなくても再送しない", expected=f"{UART_ACK_TIMEOUT_S}秒後に再送"
    )
    for ref in {cmd.cmd_ref for cmd in resent}:
        times = [at for at, cmd in held if cmd.cmd_ref == ref]
        c.expect(
            len(times) <= 1 + UART_CMD_MAX_RETRIES,
            "再送の回数が多すぎる",
            ref=f"cmd_ref={ref}",
            expected=f"{1 + UART_CMD_MAX_RETRIES}回以下",
            actual=len(times),
        )
        for a, b in itertools.pairwise(times):
            c.expect(
                b - a >= UART_ACK_TIMEOUT_S - _TIMING_TOLERANCE_S,
                "ACK を待たずに再送した",
                ref=f"cmd_ref={ref}",
                expected=f"{UART_ACK_TIMEOUT_S}秒以上",
                actual=f"{b - a:.2f}秒",
            )
    # ACK を返したあとは、すべての request が done になる
    deadline = time.monotonic() + FINISH_WAIT_S
    while time.monotonic() < deadline:
        if all(
            any(
                o.message["payload"]["status"] == "done"
                for o in _acks_for(ctx, r["request_id"], started)
                if o.message
            )
            for r in sent
        ):
            break
        await asyncio.sleep(0.2)
    for r in sent:
        statuses = [
            o.message["payload"]["status"]
            for o in _acks_for(ctx, r["request_id"], started)
            if o.message
        ]
        c.expect(
            "done" in statuses,
            "ACK を返したのに request が done にならない",
            ref=f"request_id={r['request_id']}",
            expected="done",
            actual=statuses,
        )


@check(
    ROLE,
    "request_ack",
    "request に accepted（cmd_id 付き）→ done を返し、同じ request_id の再送には現在の状態を返す",
    order=30,
)
async def request_ack(ctx: ContractContext, c: Checker) -> None:
    dev = device(ctx)
    dev.hold = False
    dev.peer.start_heartbeat()
    started = time.monotonic()
    (request,) = await _requests(ctx, 1, 250)
    deadline = time.monotonic() + FINISH_WAIT_S / 2
    while time.monotonic() < deadline:
        if any(
            o.message and o.message["payload"]["status"] == "done"
            for o in _acks_for(ctx, request["request_id"], started)
        ):
            break
        await asyncio.sleep(0.2)
    acks = [
        o.message["payload"] for o in _acks_for(ctx, request["request_id"], started) if o.message
    ]
    statuses = [a["status"] for a in acks]
    c.expect(
        statuses[:1] == ["accepted"],
        "最初の応答が accepted でない",
        ref=f"request_id={request['request_id']}",
        expected="accepted",
        actual=statuses,
    )
    c.expect(
        "done" in statuses,
        "最終結果 done が返らない",
        ref=f"request_id={request['request_id']}",
        expected="done",
        actual=statuses,
    )
    cmd_ids = {a.get("cmd_id") for a in acks}
    c.expect(
        len(cmd_ids) == 1 and None not in cmd_ids,
        "accepted と done の cmd_id が同じでない",
        ref=f"request_id={request['request_id']}",
        actual=cmd_ids,
    )
    again = time.monotonic()
    await ctx.publish_as(DEVICE_APP_01, KIND_REQUEST, request, topic_device=DEVICE_APP_01)
    found = await ctx.observer.wait_for(
        lambda o: (
            o.kind == KIND_REQUEST_ACK
            and (o.message or {}).get("payload", {}).get("request_id") == request["request_id"]
        ),
        5.0,
        since=again,
    )
    if c.expect(
        found is not None,
        "同じ request_id の再送に応答がない",
        ref=f"request_id={request['request_id']}",
    ):
        assert found is not None and found.message is not None
        p = found.message["payload"]
        c.expect(
            p["status"] == "done",
            "再送への応答が現在の状態（done）でない",
            ref=found.ref(),
            expected="done",
            actual=p["status"],
        )
        c.expect(
            p.get("cmd_id") in cmd_ids,
            "再送への応答の cmd_id が前回と違う",
            ref=found.ref(),
            expected=cmd_ids,
            actual=p.get("cmd_id"),
        )


@check(
    ROLE,
    "bridged_offline_state",
    f"UART デバイスが{UART_HEARTBEAT_LOSS_S}秒途絶えると、そのデバイス名義の offline の state を"
    "bridged_by 付き・retain で発行する",
    order=40,
)
async def bridged_offline_state(ctx: ContractContext, c: Checker) -> None:
    dev = device(ctx)
    online = [
        o
        for o in ctx.observer.select(device_id=DEVICE_MCU_C_01, kind=KIND_STATE)
        if o.message and o.message["payload"]["status"] == "online" and not o.retain
    ]
    c.expect(bool(online), "UART デバイスの online の state を観測していない")
    for o in online:
        assert o.message is not None
        c.expect(
            o.message["payload"].get("bridged_by") == ctx.device_id,
            "UART デバイスの state に bridged_by がない",
            ref=o.ref(),
            expected=ctx.device_id,
            actual=o.message["payload"].get("bridged_by"),
        )
    await dev.peer.stop_heartbeat()
    dev.hold = True
    stopped = time.monotonic()
    found = await ctx.observer.wait_for(
        lambda o: (
            o.device_id == DEVICE_MCU_C_01
            and o.kind == KIND_STATE
            and (o.message or {}).get("payload", {}).get("status") == "offline"
        ),
        UART_HEARTBEAT_LOSS_S + OFFLINE_MARGIN_S,
        since=stopped,
    )
    if not c.expect(
        found is not None,
        "UART デバイスの途絶で offline の state が発行されない",
        expected=f"{UART_HEARTBEAT_LOSS_S}秒後",
    ):
        return
    assert found is not None and found.message is not None
    c.expect(
        found.message["payload"].get("bridged_by") == ctx.device_id,
        "offline の state に bridged_by がない",
        ref=found.ref(),
        expected=ctx.device_id,
    )
    retained = await ctx.retained(ctx.topic(DEVICE_MCU_C_01, KIND_STATE))
    c.expect(
        retained is not None and retained["payload"]["status"] == "offline",
        "offline の state が retain されていない",
        actual=None if retained is None else retained["payload"]["status"],
    )
