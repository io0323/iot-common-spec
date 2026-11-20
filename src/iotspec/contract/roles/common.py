"""全 role（MQTT につながるもの）に共通のチェック：Schema 適合、seq と boot_id、送信量の上限。

対象が発行した電文は、エンベロープの device_id で見分ける（契約テストの相手役の電文は含めない）。
購読した時点で届く retain の電文（以前の実行の残り）は含めない。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from iotspec.constants import (
    DEVICE_MCU_C_01,
    MQTT_MAX_IN_FLIGHT,
    PUBLISH_RATE_AVG_PER_S,
    PUBLISH_RATE_BURST_PER_S,
)
from iotspec.contract.context import ContractContext, Observed
from iotspec.contract.core import Checker, check
from iotspec.dedup import exempt_from_seq_checks
from iotspec.schema import validate

COMMON_ORDER = 90
MIN_OBSERVE_S = 5.0
"""送信量を判定するのに最低限観測する時間（--only でほかのチェックを省いたときのため）。"""
_WINDOW_S = 1.0
_RATE_TOLERANCE = 1.1
"""受信時刻の揺らぎ（ブローカー経由で届くまでの時間差）を見込んだ許容の倍率。"""


def target_senders(ctx: ContractContext) -> set[str]:
    """対象が発行する電文のエンベロープの device_id。作品03は UART デバイス名義でも発行する。"""
    senders = {ctx.device_id}
    if ctx.role == "gw-01":
        senders.add(DEVICE_MCU_C_01)
    return senders


def from_target(ctx: ContractContext) -> list[Observed]:
    senders = target_senders(ctx)
    return [
        o
        for o in ctx.observer.messages
        if not o.retain and o.message is not None and o.message.get("device_id") in senders
    ]


@check("common", "schema", "対象が発行した全電文が JSON Schema に適合する", order=COMMON_ORDER)
async def schema_all(ctx: ContractContext, c: Checker) -> None:
    messages = from_target(ctx)
    c.expect(bool(messages), "対象の電文を1件も観測できなかった", expected="1件以上", actual=0)
    for o in messages:
        assert o.message is not None
        errors = validate(o.message)
        c.expect(
            not errors,
            "Schema に適合しない",
            ref=o.ref(),
            expected="適合",
            actual="; ".join(errors),
        )
        topic_device = o.device_id
        sender = o.message.get("device_id")
        if o.kind not in ("request", "request/ack", "cmd"):
            c.expect(
                topic_device == sender,
                "トピックの device_id とエンベロープの device_id が違う",
                ref=o.ref(),
                expected=sender,
                actual=topic_device,
            )


@check(
    "common",
    "seq_and_boot_id",
    "seq が発信元・boot_id・kind ごとに連番（UART 由来は単調増加）で、boot_id が付いている",
    order=COMMON_ORDER,
)
async def seq_and_boot(ctx: ContractContext, c: Checker) -> None:
    last: dict[tuple[str, str, str], int] = {}
    for o in from_target(ctx):
        m = o.message
        assert m is not None
        if not isinstance(m.get("boot_id"), str) or not m.get("boot_id"):
            c.fail("boot_id がない", ref=o.ref())
            continue
        if exempt_from_seq_checks(m):  # offline の state は seq の判定から外す（#29）
            continue
        key = (str(m["device_id"]), str(m["boot_id"]), str(m.get("schema")))
        seq = int(m["seq"])
        previous = last.get(key)
        last[key] = seq
        if previous is None:
            continue
        if m["device_id"] == DEVICE_MCU_C_01 and ctx.role == "gw-01":
            # UART の欠番は seq の飛びとして残す仕様（第7章）なので、増えていればよい
            c.expect(
                seq > previous,
                "seq が増えていない",
                ref=o.ref(),
                expected=f"> {previous}",
                actual=seq,
            )
        else:
            c.expect(
                seq == previous + 1,
                "seq が連番でない",
                ref=o.ref(),
                expected=previous + 1,
                actual=seq,
            )


@check(
    "common",
    "publish_rate",
    f"送信量が kind ごとに平均毎秒{PUBLISH_RATE_AVG_PER_S}件・"
    f"瞬間毎秒{PUBLISH_RATE_BURST_PER_S}件以内",
    order=COMMON_ORDER,
)
async def publish_rate(ctx: ContractContext, c: Checker) -> None:
    remaining = ctx.started_at + MIN_OBSERVE_S - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)
    by_kind: dict[tuple[str, str], list[Observed]] = defaultdict(list)
    for o in from_target(ctx):
        assert o.message is not None
        by_kind[(str(o.message["device_id"]), str(o.message.get("schema")))].append(o)
    burst_limit = int(PUBLISH_RATE_BURST_PER_S * _RATE_TOLERANCE)
    for (sender, schema), items in by_kind.items():
        window: deque[float] = deque()
        tokens, last_at = float(burst_limit), items[0].at
        worst_burst, starved = 0, None
        for o in items:
            while window and o.at - window[0] >= _WINDOW_S:
                window.popleft()
            window.append(o.at)
            worst_burst = max(worst_burst, len(window))
            tokens = min(
                float(burst_limit),
                tokens + (o.at - last_at) * PUBLISH_RATE_AVG_PER_S * _RATE_TOLERANCE,
            )
            last_at = o.at
            tokens -= 1.0
            if tokens < -1.0 and starved is None:
                starved = o
        c.expect(
            worst_burst <= burst_limit,
            f"{sender} の {schema} が1秒間に上限を超えて届いた",
            expected=f"{PUBLISH_RATE_BURST_PER_S}件以下",
            actual=f"{worst_burst}件",
        )
        if starved is not None:
            c.fail(
                f"{sender} の {schema} が平均の上限を超えて送られた",
                ref=starved.ref(),
                expected=f"平均毎秒{PUBLISH_RATE_AVG_PER_S}件（瞬間{PUBLISH_RATE_BURST_PER_S}件）以内",
                actual=f"観測した {len(items)} 件",
            )
    _ = MQTT_MAX_IN_FLIGHT  # 確認待ちの件数は外から観測できない（docs/contract-tests.md）
