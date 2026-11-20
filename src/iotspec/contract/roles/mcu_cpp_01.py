"""role=mcu-cpp-01（作品02）のチェック。相手役は gw-01（MQTT で cmd を送る）。

対象はブローカーに接続し、自分の state（online、retain）を発行している前提で始める。
アクチュエーターは対象の state の actuators[] から選ぶ。
"""

from __future__ import annotations

import time
from typing import Any

from iotspec.clock import SystemClock
from iotspec.constants import (
    CMD_ACK_TIMEOUT_S,
    DEVICE_GW_01,
    KIND_CMD,
    KIND_CMD_ACK,
    KIND_STATE,
    KIND_TELEMETRY,
    LEASE_MAX_S,
)
from iotspec.contract.context import ContractContext, Observed
from iotspec.contract.core import Checker, Skip, check
from iotspec.dedup import SeqDedupWindow, exempt_from_seq_checks
from iotspec.ids import new_ulid
from iotspec.schema import validate

ROLE = "mcu-cpp-01"
STATE_WAIT_S = 10.0
LEASE_S = 3
"""リース期限切れのチェックで使う短いリース。"""
LIMITED_EXTRA_S = 30
LWT_WAIT_S = 5.0
_DONE = ("done",)


async def target_state(ctx: ContractContext) -> dict[str, Any]:
    """対象の online の state（観測済みのもの、なければ retain を読む）。"""
    cached = ctx.options.get("state")
    if cached is not None:
        return dict(cached)
    found = await ctx.observer.wait_for(
        lambda o: (
            o.device_id == ctx.device_id
            and o.kind == KIND_STATE
            and o.message is not None
            and o.message.get("payload", {}).get("status") == "online"
        ),
        STATE_WAIT_S,
    )
    if found is None or found.message is None:
        raise Skip(
            f"{ctx.device_id} の online の state を {STATE_WAIT_S:g} 秒以内に観測できなかった"
        )
    ctx.options["state"] = found.message
    return dict(found.message)


async def actuator(ctx: ContractContext, *, with_max_on: bool) -> dict[str, Any] | None:
    state = await target_state(ctx)
    for a in state["payload"]["actuators"]:
        if (a.get("max_on_s") is not None) == with_max_on:
            return dict(a)
    return None


def period_s(state: dict[str, Any]) -> float:
    periods = [int(ch["period_ms"]) for ch in state["payload"]["channels"]] or [1000]
    return min(periods) / 1000.0


async def send_cmd(ctx: ContractContext, payload: dict[str, Any]) -> float:
    """gw-01 の名義で cmd を送り、送った時刻を返す。"""
    sent_at = time.monotonic()
    await ctx.publish_as(DEVICE_GW_01, KIND_CMD, payload, topic_device=ctx.device_id)
    return sent_at


def new_cmd(op: str, args: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"cmd_id": new_ulid(SystemClock()), "op": op, "priority": "auto"}
    if args is not None:
        payload["args"] = args
    payload.update(extra)
    return payload


async def wait_ack(
    ctx: ContractContext,
    cmd_id: str,
    since: float,
    *,
    final: bool = True,
    timeout: float = CMD_ACK_TIMEOUT_S,
) -> Observed | None:
    def matches(o: Observed) -> bool:
        if o.device_id != ctx.device_id or o.kind != KIND_CMD_ACK or o.message is None:
            return False
        p = o.message.get("payload", {})
        return bool(p.get("cmd_id") == cmd_id and (not final or p.get("status") != "accepted"))

    first = await ctx.observer.wait_for(matches, timeout, since=since)
    return first


async def telemetry_level(
    ctx: ContractContext, actuator_id: str, since: float, timeout: float
) -> tuple[int, str] | None:
    """since 以降の最初の telemetry の (level, owner)。"""
    found = await ctx.observer.wait_for(
        lambda o: (
            o.device_id == ctx.device_id
            and o.kind == KIND_TELEMETRY
            and any(
                a.get("id") == actuator_id
                for a in (o.message or {}).get("payload", {}).get("actuators", [])
            )
        ),
        timeout,
        since=since,
    )
    if found is None or found.message is None:
        return None
    for a in found.message["payload"]["actuators"]:
        if a["id"] == actuator_id:
            return int(a["level"]), str(a.get("owner"))
    return None


@check(ROLE, "ack", "set_actuator に同じ cmd_id の cmd/ack（done）が2秒以内に返る", order=10)
async def ack(ctx: ContractContext, c: Checker) -> None:
    target = await actuator(ctx, with_max_on=False)
    if target is None:
        raise Skip("max_on_s のないアクチュエーターが state にない")
    cmd = new_cmd("set_actuator", {"id": target["id"], "level": 500}, lease_s=10)
    since = await send_cmd(ctx, cmd)
    got = await wait_ack(ctx, cmd["cmd_id"], since, final=False)
    if not c.expect(
        got is not None,
        "cmd/ack が返らない",
        ref=f"cmd_id={cmd['cmd_id']}",
        expected=f"{CMD_ACK_TIMEOUT_S}秒以内",
    ):
        return
    assert got is not None and got.message is not None
    c.expect(validate(got.message) == [], "cmd/ack が Schema に適合しない", ref=got.ref())
    final = await wait_ack(ctx, cmd["cmd_id"], since, timeout=CMD_ACK_TIMEOUT_S * 3)
    if c.expect(
        final is not None, "最終結果（done など）が返らない", ref=f"cmd_id={cmd['cmd_id']}"
    ):
        assert final is not None and final.message is not None
        p = final.message["payload"]
        c.expect(
            p["status"] == "done",
            "set_actuator が done にならない",
            ref=final.ref(),
            expected="done",
            actual=p["status"],
        )
        c.expect("applied" in p, "done の ack に applied がない", ref=final.ref())
    await send_cmd(ctx, new_cmd("release", {"id": target["id"]}))


@check(
    ROLE,
    "lease_expiry",
    "リースが切れると、その出力の owner が上位の priority から local（または none）に戻る",
    order=20,
)
async def lease_expiry(ctx: ContractContext, c: Checker) -> None:
    target = await actuator(ctx, with_max_on=False)
    if target is None:
        raise Skip("max_on_s のないアクチュエーターが state にない")
    state = await target_state(ctx)
    period = period_s(state)
    cmd = new_cmd("set_actuator", {"id": target["id"], "level": 700}, lease_s=LEASE_S)
    since = await send_cmd(ctx, cmd)
    await wait_ack(ctx, cmd["cmd_id"], since)
    during = await telemetry_level(ctx, target["id"], since + 0.2, period * 3)
    c.expect(
        during is not None and during[1] == "auto",
        "リース中の telemetry の owner が cmd の priority（auto）でない",
        ref=f"{target['id']}",
        expected="auto",
        actual=during,
    )
    after = await telemetry_level(ctx, target["id"], since + LEASE_S + period, LEASE_S + period * 4)
    c.expect(
        after is not None and after[1] in ("local", "none"),
        "リースが切れたあとの owner が local（または none）に戻らない",
        ref=f"{target['id']}",
        expected="local または none",
        actual=after,
    )


@check(
    ROLE,
    "duplicate_cmd_id",
    "重複した cmd_id には前回の ack を返し、指令を実行し直さない",
    order=30,
)
async def duplicate_cmd_id(ctx: ContractContext, c: Checker) -> None:
    target = await actuator(ctx, with_max_on=False)
    if target is None:
        raise Skip("max_on_s のないアクチュエーターが state にない")
    period = period_s(await target_state(ctx))
    first = new_cmd("set_actuator", {"id": target["id"], "level": 600}, lease_s=30)
    since = await send_cmd(ctx, first)
    ack1 = await wait_ack(ctx, first["cmd_id"], since)
    second = new_cmd("set_actuator", {"id": target["id"], "level": 300}, lease_s=30)
    since2 = await send_cmd(ctx, second)
    await wait_ack(ctx, second["cmd_id"], since2)
    since3 = await send_cmd(ctx, first)  # 同じ cmd_id で送り直す
    ack2 = await wait_ack(ctx, first["cmd_id"], since3)
    if not c.expect(
        ack1 is not None and ack2 is not None, "ack が返らない", ref=f"cmd_id={first['cmd_id']}"
    ):
        return
    assert ack1 is not None and ack2 is not None and ack1.message and ack2.message
    c.expect(
        ack1.message["payload"] == ack2.message["payload"],
        "重複した cmd_id への ack が前回と違う",
        ref=ack2.ref(),
        expected=ack1.message["payload"],
        actual=ack2.message["payload"],
    )
    level = await telemetry_level(ctx, target["id"], since3 + period, period * 4)
    c.expect(
        level is not None and level[0] == 300,
        "重複した cmd_id の指令を実行し直した（出力が後の指令の値から変わった）",
        ref=f"{target['id']}",
        expected=300,
        actual=None if level is None else level[0],
    )
    await send_cmd(ctx, new_cmd("release", {"id": target["id"]}))


@check(
    ROLE,
    "limited_applied",
    "安全制限で丸める指令の ack に limited: true と applied が付く",
    order=40,
)
async def limited_applied(ctx: ContractContext, c: Checker) -> None:
    target = await actuator(ctx, with_max_on=True)
    if target is None:
        raise Skip("max_on_s のあるアクチュエーターが state にない（安全制限を確かめられない）")
    lease_s = min(LEASE_MAX_S, int(target["max_on_s"]) + LIMITED_EXTRA_S)
    cmd = new_cmd("set_actuator", {"id": target["id"], "level": 1000}, lease_s=lease_s)
    since = await send_cmd(ctx, cmd)
    got = await wait_ack(ctx, cmd["cmd_id"], since)
    if c.expect(got is not None, "ack が返らない", ref=f"cmd_id={cmd['cmd_id']}"):
        assert got is not None and got.message is not None
        p = got.message["payload"]
        c.expect(
            p.get("limited") is True,
            f"連続運転の上限（max_on_s {target['max_on_s']}）を超える lease_s にlimited が付かない",
            ref=got.ref(),
            expected=True,
            actual=p.get("limited"),
        )
        c.expect(
            isinstance(p.get("applied"), dict) and "level" in p["applied"],
            "applied に実際の level がない",
            ref=got.ref(),
            actual=p.get("applied"),
        )
    await send_cmd(ctx, new_cmd("release", {"id": target["id"]}))


@check(ROLE, "set_param_unknown_op", "set_param には rejected（unknown_op）を返す", order=50)
async def set_param(ctx: ContractContext, c: Checker) -> None:
    await target_state(ctx)
    cmd = new_cmd("set_param", {"name": "contract_test", "value": 1})
    since = await send_cmd(ctx, cmd)
    got = await wait_ack(ctx, cmd["cmd_id"], since)
    if c.expect(got is not None, "ack が返らない", ref=f"cmd_id={cmd['cmd_id']}"):
        assert got is not None and got.message is not None
        p = got.message["payload"]
        c.expect(
            (p["status"], p.get("reason")) == ("rejected", "unknown_op"),
            "set_param への応答が rejected／unknown_op でない",
            ref=got.ref(),
            expected=("rejected", "unknown_op"),
            actual=(p["status"], p.get("reason")),
        )


@check(
    ROLE,
    "lwt",
    "接続が切れると LWT の offline の state（retain）が発行され、seq の判定で破棄されない",
    order=99,
    needs_target_control=True,
)
async def lwt(ctx: ContractContext, c: Checker) -> None:
    state = await target_state(ctx)
    killed_at = time.monotonic()
    await ctx.target.kill()
    found = await ctx.observer.wait_for(
        lambda o: (
            o.device_id == ctx.device_id
            and o.kind == KIND_STATE
            and (o.message or {}).get("payload", {}).get("status") == "offline"
        ),
        LWT_WAIT_S,
        since=killed_at,
    )
    if not c.expect(
        found is not None,
        "接続断のあとに offline の state が届かない",
        expected=f"{LWT_WAIT_S:g}秒以内",
    ):
        return
    assert found is not None and found.message is not None
    c.expect(
        validate(found.message) == [], "offline の state が Schema に適合しない", ref=found.ref()
    )
    c.expect(
        found.message.get("boot_id") == state.get("boot_id"),
        "LWT の boot_id が online の state と違う",
        ref=found.ref(),
        expected=state.get("boot_id"),
        actual=found.message.get("boot_id"),
    )
    c.expect(
        exempt_from_seq_checks(found.message),
        "offline の state として seq の判定から外せない",
        ref=found.ref(),
    )
    window = SeqDedupWindow()
    window.accept(state, KIND_STATE)
    c.expect(
        window.accept(found.message, KIND_STATE),
        "受信側の重複排除で LWT が破棄される",
        ref=found.ref(),
    )
    retained = await ctx.retained(ctx.topic(ctx.device_id, KIND_STATE))
    c.expect(
        retained is not None and retained.get("payload", {}).get("status") == "offline",
        "offline の state が retain されていない",
        expected="offline",
        actual=None if retained is None else retained.get("payload", {}).get("status"),
    )
