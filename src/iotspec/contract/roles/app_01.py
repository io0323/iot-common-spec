"""role=app-01（作品04）のチェック。

相手役は gw-01（request に request/ack を返す。契約テスト自身）。

対象（作品04）は、観測の時間（--duration、既定 30 秒）のうちに、手動操作の request を2件以上と
スケジュール実行の request を1件以上送るように設定しておく（docs/contract-tests.md）。
相手役は最初に届いた request_id にだけわざと応答せず（再送の確認用）、
ほかには accepted → done を返す。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any

import aiomqtt

from iotspec.clock import SystemClock, parse_ts
from iotspec.constants import (
    DEVICE_GW_01,
    KIND_REQUEST,
    KIND_REQUEST_ACK,
    REQUEST_MAX_RETRIES,
    REQUEST_RESPONSE_TIMEOUT_S,
)
from iotspec.contract.context import ContractContext, Observed
from iotspec.contract.core import Checker, Skip, check
from iotspec.ids import new_ulid, schedule_request_id
from iotspec.schema import validate

ROLE = "app-01"
DEFAULT_DURATION_S = 30.0
DONE_AFTER_S = 1.0
ACK_GRACE_S = 0.5
_TIMING_TOLERANCE_S = 0.5


class Gateway:
    """gw-01 の役。

    最初の request_id は無視し、ほかには accepted → done。重複には現在の状態を返す。
    """

    def __init__(self, ctx: ContractContext) -> None:
        self.ctx = ctx
        self.ignored: str | None = None
        self.state: dict[str, dict[str, Any]] = {}
        self.acked_at: dict[str, float] = {}

    async def run(self) -> None:
        client = await self.ctx.client(DEVICE_GW_01)
        await client.subscribe(self.ctx.topic(self.ctx.device_id, KIND_REQUEST), qos=1)
        async for message in client.messages:
            if isinstance(message.payload, bytes | bytearray):
                await self._on_request(client, json.loads(message.payload))

    async def _respond(self, client: aiomqtt.Client, payload: dict[str, Any]) -> None:
        await self.ctx.publish_as(
            DEVICE_GW_01, KIND_REQUEST_ACK, payload, topic_device=self.ctx.device_id
        )

    async def _on_request(self, client: aiomqtt.Client, message: dict[str, Any]) -> None:
        if validate(message):
            return
        request_id = str(message["payload"]["request_id"])
        if self.ignored is None:
            self.ignored = request_id
        if request_id == self.ignored:
            return
        current = self.state.get(request_id)
        if current is not None:
            await self._respond(client, dict(current))
            return
        cmd_id = new_ulid(SystemClock())
        accepted = {
            "request_id": request_id,
            "status": "accepted",
            "cmd_id": cmd_id,
            "reason": None,
        }
        self.state[request_id] = accepted
        self.acked_at[request_id] = time.monotonic()
        await self._respond(client, accepted)
        await asyncio.sleep(DONE_AFTER_S)
        done = {"request_id": request_id, "status": "done", "cmd_id": cmd_id, "reason": None}
        self.state[request_id] = done
        await self._respond(client, done)


async def observe(ctx: ContractContext) -> Gateway:
    """相手役を動かし、観測の時間が過ぎるまで待つ（最初のチェックで1回だけ）。"""
    gateway = ctx.options.get("gateway")
    if gateway is None:
        gateway = ctx.options["gateway"] = Gateway(ctx)
        ctx.spawn(gateway.run())
        duration = float(ctx.options.get("duration_s", DEFAULT_DURATION_S))
        await asyncio.sleep(duration)
    assert isinstance(gateway, Gateway)
    return gateway


def requests(ctx: ContractContext) -> list[Observed]:
    return [
        o
        for o in ctx.observer.select(device_id=ctx.device_id, kind=KIND_REQUEST)
        if o.message is not None and not o.retain
    ]


def by_id(ctx: ContractContext) -> dict[str, list[Observed]]:
    grouped: dict[str, list[Observed]] = {}
    for o in requests(ctx):
        assert o.message is not None
        grouped.setdefault(str(o.message["payload"]["request_id"]), []).append(o)
    return grouped


@check(
    ROLE,
    "request_schema",
    "request が JSON Schema に適合し、手動とスケジュールの両方が観測できる",
    order=10,
)
async def request_schema(ctx: ContractContext, c: Checker) -> None:
    await observe(ctx)
    seen = requests(ctx)
    c.expect(
        bool(seen),
        "request を1件も観測できなかった",
        expected="観測の時間内に request を送るよう対象を設定する",
    )
    kinds = set()
    for o in seen:
        assert o.message is not None
        errors = validate(o.message)
        c.expect(
            not errors, "request が Schema に適合しない", ref=o.ref(), actual="; ".join(errors)
        )
        kinds.add(o.message["payload"].get("kind"))
    c.expect(
        {"manual", "schedule"} <= kinds,
        "手動とスケジュール実行の両方の request が観測できない",
        expected={"manual", "schedule"},
        actual=kinds,
    )


@check(
    ROLE,
    "retransmission",
    f"応答がなければ同じ request_id で{REQUEST_RESPONSE_TIMEOUT_S}秒ごとに"
    f"最大{REQUEST_MAX_RETRIES}回再送して止まる",
    order=20,
)
async def retransmission(ctx: ContractContext, c: Checker) -> None:
    gateway = await observe(ctx)
    if gateway.ignored is None:
        raise Skip("request を観測できなかった")
    sends = by_id(ctx).get(gateway.ignored, [])
    c.expect(
        len(sends) == 1 + REQUEST_MAX_RETRIES,
        "応答がない request の送信回数が違う（最初の送信＋3回の再送で止まる）",
        ref=f"request_id={gateway.ignored}",
        expected=1 + REQUEST_MAX_RETRIES,
        actual=len(sends),
    )
    for a, b in itertools.pairwise(sends):
        c.expect(
            b.at - a.at >= REQUEST_RESPONSE_TIMEOUT_S - _TIMING_TOLERANCE_S,
            "応答を待たずに再送した",
            ref=b.ref(),
            expected=f"{REQUEST_RESPONSE_TIMEOUT_S}秒以上",
            actual=f"{b.at - a.at:.2f}秒",
        )
    payloads = {json.dumps((o.message or {}).get("payload"), sort_keys=True) for o in sends}
    c.expect(
        len(payloads) == 1,
        "再送で request の中身が変わった",
        ref=f"request_id={gateway.ignored}",
        actual=len(payloads),
    )


@check(
    ROLE,
    "schedule_request_id",
    "スケジュール実行の request_id が schedule_request_id(schedule.id, planned_at) と一致する",
    order=30,
)
async def schedule_id(ctx: ContractContext, c: Checker) -> None:
    await observe(ctx)
    scheduled = [
        o for o in requests(ctx) if (o.message or {}).get("payload", {}).get("kind") == "schedule"
    ]
    if not c.expect(bool(scheduled), "スケジュール実行の request を観測できなかった"):
        return
    for o in scheduled:
        assert o.message is not None
        p = o.message["payload"]
        expected = schedule_request_id(
            int(p["schedule"]["id"]), parse_ts(p["schedule"]["planned_at"])
        )
        c.expect(
            p["request_id"] == expected,
            "決定的な request_id になっていない",
            ref=o.ref(),
            expected=expected,
            actual=p["request_id"],
        )


@check(ROLE, "ack_tracking", "request/ack を受けたあとは、その request_id を再送しない", order=40)
async def ack_tracking(ctx: ContractContext, c: Checker) -> None:
    gateway = await observe(ctx)
    answered = 0
    for request_id, sends in by_id(ctx).items():
        acked = gateway.acked_at.get(request_id)
        if acked is None:
            continue
        answered += 1
        late = [o for o in sends if o.at > acked + ACK_GRACE_S]
        c.expect(
            not late,
            "accepted を受けたあとも再送している",
            ref=late[0].ref() if late else None,
            expected="再送しない",
            actual=f"{len(late)}件",
        )
    c.expect(
        answered > 0,
        "応答した request がない（手動操作の request を2件以上送るよう対象を設定する）",
    )
