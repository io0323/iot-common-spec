"""role=mcu-c-01（作品01）のチェック。相手役は gw-01 簡易版と同じ UART の上位役（契約テスト自身）。

契約テストが pty を作り（--uart がなければ）、対象はそのパスを開く。
契約テストは1秒ごとに HEARTBEAT を送る。
ゴールデンフレームとの一致は2段で確かめる：作品01の符号化そのものは uart/golden/ を使った作品01側の
単体テスト（docs/contract-tests.md）、ここでは受けた全フレームが参照実装と同じバイト列に符号化されることを確かめる。
"""

from __future__ import annotations

import time

from iotspec.clock import SystemClock
from iotspec.constants import (
    DEVICE_GW_01,
    UART_ACK_TIMEOUT_S,
    UART_HEARTBEAT_PERIOD_S,
    UART_OP_CLEAR_OVERRIDE,
    UART_OP_GET_STATUS,
    UART_OP_SET_FAN_OVERRIDE,
    UART_SEQ_MODULO,
    UART_UPSTREAM_LOST_S,
)
from iotspec.contract.context import ContractContext, UartPeer
from iotspec.contract.core import Checker, Skip, check
from iotspec.profile import load_profile
from iotspec.uart.bridge import UartBridge
from iotspec.uart.frames import Ack, Cmd, Discarded, Event, State, Telemetry, encode_frame
from iotspec.uart.mapping import EVENT_CODE

ROLE = "mcu-c-01"
STATE_WAIT_S = 10.0
LOST_MARGIN_S = 4.0
RESTORED_WAIT_S = 4.0
CODE_BOOT = EVENT_CODE.to_uart("BOOT")
CODE_LOST = EVENT_CODE.to_uart("UPSTREAM_LOST")
CODE_RESTORED = EVENT_CODE.to_uart("UPSTREAM_RESTORED")
_cmd_ref = [0x4000]


def uart(ctx: ContractContext) -> UartPeer:
    if ctx.uart is None:
        raise Skip("仮想シリアルがない")
    return ctx.uart


def next_ref() -> int:
    _cmd_ref[0] = (_cmd_ref[0] + 1) % UART_SEQ_MODULO
    return _cmd_ref[0]


async def ready(ctx: ContractContext) -> UartPeer:
    """対象から STATE を受けるまで待つ（HEARTBEAT は送り続ける）。"""
    peer = uart(ctx)
    peer.start_heartbeat()
    if not ctx.options.get("ready"):
        got = await peer.wait_frame(lambda f: isinstance(f, State), STATE_WAIT_S)
        if got is None:
            peer.send(Cmd(peer.next_seq(Cmd.TYPE), next_ref(), UART_OP_GET_STATUS, 0))
            got = await peer.wait_frame(lambda f: isinstance(f, State), STATE_WAIT_S)
        if got is None:
            raise Skip(f"対象から STATE を {STATE_WAIT_S:g} 秒以内に受けられなかった")
        ctx.options["ready"] = True
    return peer


async def send_cmd(
    peer: UartPeer, op: int, ttl_s: int = 0, args: tuple[int, ...] = (), ref: int | None = None
) -> tuple[Cmd, float]:
    cmd = Cmd(peer.next_seq(Cmd.TYPE), next_ref() if ref is None else ref, op, ttl_s, args)
    sent_at = time.monotonic()
    peer.send(cmd)
    return cmd, sent_at


async def wait_ack(peer: UartPeer, cmd_ref: int, since: float) -> Ack | None:
    got = await peer.wait_frame(
        lambda f: isinstance(f, Ack) and f.cmd_ref == cmd_ref, UART_ACK_TIMEOUT_S, since=since
    )
    return got if isinstance(got, Ack) else None


async def fan_after(peer: UartPeer, since: float, timeout: float = 3.0) -> int | None:
    got = await peer.wait_frame(lambda f: isinstance(f, Telemetry), timeout, since=since)
    return got.fan_pm if isinstance(got, Telemetry) else None


@check(
    ROLE,
    "boot_order",
    "起動後の最初のフレームが STATE で、その次の EVENT が BOOT",
    order=5,
    needs_target_control=True,
)
async def boot_order(ctx: ContractContext, c: Checker) -> None:
    peer = await ready(ctx)
    frames = [r.frame for r in peer.received if not isinstance(r.frame, Discarded)]
    c.expect(
        bool(frames) and isinstance(frames[0], State),
        "最初のフレームが STATE でない",
        expected="STATE",
        actual=type(frames[0]).__name__ if frames else None,
    )
    events = [f for f in frames if isinstance(f, Event)]
    c.expect(
        bool(events) and events[0].code == CODE_BOOT,
        "最初の EVENT が BOOT でない",
        expected=f"code {CODE_BOOT}",
        actual=events[0].code if events else None,
    )
    if events and frames:
        boot_at = frames.index(events[0])
        state_at = next((i for i, f in enumerate(frames) if isinstance(f, State)), len(frames))
        c.expect(state_at < boot_at, "BOOT が STATE より先に来た", expected="STATE → BOOT")


@check(ROLE, "ack", "CMD に同じ cmd_ref の ACK（done）が2秒以内に返る", order=10)
async def ack(ctx: ContractContext, c: Checker) -> None:
    peer = await ready(ctx)
    cmd, since = await send_cmd(peer, UART_OP_GET_STATUS)
    got = await wait_ack(peer, cmd.cmd_ref, since)
    if c.expect(
        got is not None,
        "ACK が返らない",
        ref=f"GET_STATUS cmd_ref={cmd.cmd_ref}",
        expected=f"{UART_ACK_TIMEOUT_S}秒以内",
    ):
        assert got is not None
        c.expect(
            got.status == 1,
            "GET_STATUS の ACK が done でない",
            ref=f"cmd_ref={cmd.cmd_ref}",
            expected=1,
            actual=got.status,
        )
    state = await peer.wait_frame(lambda f: isinstance(f, State), UART_ACK_TIMEOUT_S, since=since)
    c.expect(state is not None, "GET_STATUS に STATE が返らない", ref=f"cmd_ref={cmd.cmd_ref}")
    cmd, since = await send_cmd(peer, UART_OP_SET_FAN_OVERRIDE, 30, (400,))
    got = await wait_ack(peer, cmd.cmd_ref, since)
    if c.expect(got is not None, "ACK が返らない", ref=f"SET_FAN_OVERRIDE cmd_ref={cmd.cmd_ref}"):
        assert got is not None
        c.expect(
            (got.status, got.applied) == (1, 400),
            "SET_FAN_OVERRIDE の ACK が done・applied 400 でない",
            ref=f"cmd_ref={cmd.cmd_ref}",
            expected=(1, 400),
            actual=(got.status, got.applied),
        )
    await send_cmd(peer, UART_OP_CLEAR_OVERRIDE)


@check(
    ROLE,
    "duplicate_cmd_ref",
    "重複した cmd_ref には前回の ACK を返し、指令を実行し直さない",
    order=20,
)
async def duplicate_cmd_ref(ctx: ContractContext, c: Checker) -> None:
    peer = await ready(ctx)
    first, since = await send_cmd(peer, UART_OP_SET_FAN_OVERRIDE, 60, (700,))
    ack1 = await wait_ack(peer, first.cmd_ref, since)
    second, since2 = await send_cmd(peer, UART_OP_SET_FAN_OVERRIDE, 60, (200,))
    await wait_ack(peer, second.cmd_ref, since2)
    _, since3 = await send_cmd(peer, UART_OP_SET_FAN_OVERRIDE, 60, (700,), ref=first.cmd_ref)
    ack2 = await wait_ack(peer, first.cmd_ref, since3)
    if not c.expect(
        ack1 is not None and ack2 is not None, "ACK が返らない", ref=f"cmd_ref={first.cmd_ref}"
    ):
        return
    assert ack1 is not None and ack2 is not None
    fields1 = (ack1.status, ack1.reason, ack1.limited, ack1.applied)
    fields2 = (ack2.status, ack2.reason, ack2.limited, ack2.applied)
    c.expect(
        fields1 == fields2,
        "重複した cmd_ref への ACK が前回と違う",
        ref=f"cmd_ref={first.cmd_ref}",
        expected=fields1,
        actual=fields2,
    )
    fan = await fan_after(peer, since3 + UART_HEARTBEAT_PERIOD_S)
    c.expect(
        fan == 200,
        "重複した cmd_ref の指令を実行し直した（fan_pm が後の指令の値から変わった）",
        ref=f"cmd_ref={first.cmd_ref}",
        expected=200,
        actual=fan,
    )
    await send_cmd(peer, UART_OP_CLEAR_OVERRIDE)


@check(
    ROLE,
    "upstream_lost_restored",
    "上位の HEARTBEAT が10秒途絶えると手動指定を解除して UPSTREAM_LOST、戻ると UPSTREAM_RESTORED",
    order=30,
)
async def upstream(ctx: ContractContext, c: Checker) -> None:
    peer = await ready(ctx)
    cmd, since = await send_cmd(peer, UART_OP_SET_FAN_OVERRIDE, 600, (900,))
    await wait_ack(peer, cmd.cmd_ref, since)
    await peer.stop_heartbeat()
    stopped = time.monotonic()
    lost = await peer.wait_frame(
        lambda f: isinstance(f, Event) and f.code == CODE_LOST,
        UART_UPSTREAM_LOST_S + LOST_MARGIN_S,
        since=stopped,
    )
    if c.expect(
        lost is not None,
        "HEARTBEAT の途絶で UPSTREAM_LOST が来ない",
        expected=f"{UART_UPSTREAM_LOST_S}秒後",
    ):
        fan = await fan_after(peer, time.monotonic())
        c.expect(
            fan is not None and fan != 900,
            "UPSTREAM_LOST のあとも手動指定（fan_pm 900）が残っている",
            expected="900 以外",
            actual=fan,
        )
    resumed = time.monotonic()
    peer.start_heartbeat()
    restored = await peer.wait_frame(
        lambda f: isinstance(f, Event) and f.code == CODE_RESTORED, RESTORED_WAIT_S, since=resumed
    )
    c.expect(
        restored is not None,
        "HEARTBEAT が戻っても UPSTREAM_RESTORED（EVENT code 8）が来ない",
        expected=f"{RESTORED_WAIT_S:g}秒以内",
    )


@check(
    ROLE,
    "frame_encoding",
    "受けた全フレームが CRC・COBS・長さとも正しく、参照実装と同じバイト列に符号化される",
    order=90,
)
async def frame_encoding(ctx: ContractContext, c: Checker) -> None:
    peer = uart(ctx)
    c.expect(bool(peer.received), "フレームを1件も受けていない")
    for r in peer.received:
        if isinstance(r.frame, Discarded):
            c.fail(
                "壊れたフレームを受けた",
                ref=r.raw.hex(" "),
                expected="正しいフレーム",
                actual=f"{r.frame.reason}: {r.frame.detail}",
            )
            continue
        c.expect(
            encode_frame(r.frame) == r.raw,
            "参照実装の符号化と違うバイト列",
            ref=repr(r.frame),
            expected=encode_frame(r.frame).hex(" "),
            actual=r.raw.hex(" "),
        )


@check(ROLE, "seq_per_type", "UART の seq がフレーム種別ごとの連番（#25）", order=90)
async def seq_per_type(ctx: ContractContext, c: Checker) -> None:
    peer = uart(ctx)
    last: dict[str, int] = {}
    boot: int | None = None
    for r in peer.received:
        f = r.frame
        if isinstance(f, Discarded):
            continue
        if isinstance(f, State) and boot is not None and f.boot_id != boot:
            last.clear()  # 再起動
        if isinstance(f, State):
            boot = f.boot_id
        name = type(f).__name__.upper()
        previous = last.get(name)
        last[name] = f.seq
        if previous is not None:
            c.expect(
                f.seq == (previous + 1) % UART_SEQ_MODULO,
                f"{name} の seq が連番でない",
                ref=repr(f),
                expected=(previous + 1) % UART_SEQ_MODULO,
                actual=f.seq,
            )


@check(
    ROLE,
    "converted_schema",
    "受けたフレームを参照実装で変換した電文が、すべて JSON Schema に適合する",
    order=90,
)
async def converted_schema(ctx: ContractContext, c: Checker) -> None:
    peer = uart(ctx)
    bridge = UartBridge(load_profile(ctx.device_id), SystemClock(), bridged_by=DEVICE_GW_01)
    for r in peer.received:
        if isinstance(r.frame, Discarded):
            continue
        result = bridge.receive(r.frame)
        c.expect(
            result.discarded in (None, "awaiting_state", "unknown_cmd_ref"),
            "変換できないフレーム",
            ref=repr(r.frame),
            expected="変換できる値",
            actual=f"{result.discarded}: {result.detail}",
        )
