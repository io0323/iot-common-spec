"""Phase 5 結合テスト：pty の UART、request の流れ、ACL、LWT。

前提は conftest.py を参照（ブローカーに接続できなければ skip）。シナリオは10倍速で流す。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime

import pytest

from iotspec import constants as c
from iotspec.clock import SystemClock, format_ts
from iotspec.dedup import SeqDedupWindow
from iotspec.envelope import EnvelopeBuilder, SeqCounter
from iotspec.ids import new_ulid
from iotspec.schema import validate
from iotspec.sim.devices.mcu_c_01 import McuC01
from iotspec.sim.mqtt import BrokerAddress, client_for, run_over_mqtt
from iotspec.sim.scenario import load_scenario, parse_scenario
from iotspec.sim.simulation import Simulation

from .conftest import observing

pytestmark = pytest.mark.integration

SITE = c.DEFAULT_SITE
WAIT_S = 1.5


def _topic(device_id: str, kind: str) -> str:
    return c.TOPIC_FORMAT.format(site=SITE, device_id=device_id, kind=kind)


# ---------------------------------------------------------------- UART（pty）


async def test_uart_link_over_pty(broker: BrokerAddress) -> None:
    """mcu-c-01 の代役（pty を作る）と gw-01 簡易版（そのパスを pyserial で開く）をつなぐ。"""
    sim = Simulation(load_scenario("uart_link"))
    async with observing(broker) as observer:
        await run_over_mqtt(sim, broker)
    mcu, gw = sim.mcu_c, sim.gw
    assert isinstance(mcu, McuC01) and gw is not None and gw.bridge is not None

    # CMD → ACK：すべての指令に ACK が返り、検証で食い違いがない
    assert gw.cmds and all(
        r.ack is not None and r.ack["status"] == "done" for r in gw.cmds.values()
    )
    assert gw.stats["ack_mismatch"] == 0
    # 重複 cmd_ref への前回 ACK
    assert mcu.stats["duplicate_cmd_ref"] >= 1
    assert gw.stats["duplicate_ack"] >= 1
    # HEARTBEAT の途絶で手動指定を解除し UPSTREAM_LOST
    events = [m["payload"]["code"] for m in observer.on(f"/{c.DEVICE_MCU_C_01}/event")]
    assert "UPSTREAM_LOST" in events
    assert events[0] == "BOOT"
    # 破損フレームの破棄と計数
    assert mcu.stats["corrupted_sent"] == 2
    assert gw.bridge.discards["crc_mismatch"] == 2
    # 変換した電文はすべて Schema に適合し、mcu-c-01 名義
    converted = [m for t, m in observer.messages if f"/{c.DEVICE_MCU_C_01}/" in t]
    assert converted and all(validate(m) == [] for m in converted)
    assert {m["device_id"] for m in converted} == {c.DEVICE_MCU_C_01}


# ---------------------------------------------------------------- request の流れ


async def test_request_flow_and_retransmission(broker: BrokerAddress) -> None:
    sim = Simulation(load_scenario("request_flow"))
    async with observing(broker) as observer:
        await run_over_mqtt(sim, broker)
    app, gw = sim.app, sim.gw
    assert app is not None and gw is not None
    by_op = {
        (r.payload["op"], (r.payload.get("args") or {}).get("id")): r for r in app.requests.values()
    }

    pump = by_op[("set_actuator", "pump")]
    assert (pump.status, [a["status"] for a in pump.acks]) == ("done", ["accepted", "done"])
    assert pump.acks[0]["cmd_id"] == pump.acks[1]["cmd_id"]

    buzzer = by_op[("set_actuator", "buzzer")]
    assert (buzzer.status, buzzer.reason) == ("rejected", "not_permitted")

    # 無応答：最初の送信＋3回の再送で止まり、gateway_timeout
    silent = by_op[("set_mode", None)]
    assert silent.attempts == 1 + c.REQUEST_MAX_RETRIES
    assert (silent.status, silent.reason) == ("failed", "gateway_timeout")
    sent = [
        m
        for m in observer.on(f"/{c.DEVICE_APP_01}/request")
        if m["payload"]["request_id"] == silent.request_id
    ]
    assert len(sent) == 1 + c.REQUEST_MAX_RETRIES

    schedule = by_op[("set_actuator", "led_grow")]
    assert schedule.status == "done"

    for _, m in observer.messages:
        assert validate(m) == [], m


async def test_duplicate_request_id_returns_current_state(broker: BrokerAddress) -> None:
    """同じ request_id の再送には、gw-01 簡易版がその時点の状態を返す（第4章）。"""
    scenario = parse_scenario(
        {
            "name": "dup_request",
            "start": format_ts(datetime.now(UTC)),
            "speed": 1,
            "duration_s": 10,
            "devices": [
                {
                    "device_id": c.DEVICE_GW_01,
                    "type": "gw_01_lite",
                    "request_rules": [{"result": "done", "done_after_s": 3}],
                }
            ],
        }
    )
    sim = Simulation(scenario)
    builder = EnvelopeBuilder(c.DEVICE_APP_01, SystemClock(), SeqCounter())
    request = {
        "request_id": new_ulid(SystemClock()),
        "kind": "manual",
        "target": c.DEVICE_MCU_CPP_01,
        "op": "set_actuator",
        "args": {"id": "fan_main", "level": 300},
    }
    ack_topic = _topic(c.DEVICE_APP_01, c.KIND_REQUEST_ACK)
    async with observing(broker, ack_topic) as observer:
        runner = asyncio.create_task(run_over_mqtt(sim, broker))
        await asyncio.sleep(1.0)
        async with client_for(broker, c.DEVICE_APP_01) as app:
            # 1回目の送信、受理中の送り直し、done のあとの送り直し
            for wait_s in (1.0, 4.0, 1.0):
                message = builder.build(c.KIND_REQUEST, request).to_dict()
                await app.publish(
                    _topic(c.DEVICE_APP_01, c.KIND_REQUEST), json.dumps(message), qos=1
                )
                await asyncio.sleep(wait_s)
        await runner
    gw = sim.gw
    assert gw is not None and gw.stats["duplicate_request"] == 2
    statuses = [
        m["payload"]["status"]
        for _, m in observer.messages
        if m["payload"]["request_id"] == request["request_id"]
    ]
    # 1回目：accepted、重複（受理中）：accepted、最終結果：done、重複（終了後）：done
    assert statuses == ["accepted", "accepted", "done", "done"]


# ---------------------------------------------------------------- ACL


async def _published(
    broker: BrokerAddress, user: str, topic: str, payload: dict[str, object]
) -> bool:
    """user の名義で topic に発行し、observer に届いたら True。"""
    async with observing(broker, topic) as observer:
        async with client_for(broker, user) as client:
            await client.publish(topic, json.dumps(payload), qos=1)
        await asyncio.sleep(WAIT_S)
    return bool(observer.messages)


def _cmd_message(sender: str) -> dict[str, object]:
    builder = EnvelopeBuilder(sender, SystemClock(), SeqCounter())
    payload = {"cmd_id": new_ulid(SystemClock()), "op": "get_status", "priority": "auto"}
    return builder.build(c.KIND_CMD, payload).to_dict()


async def test_acl_only_gw_can_publish_cmd(broker: BrokerAddress) -> None:
    topic = _topic(c.DEVICE_MCU_CPP_01, c.KIND_CMD)
    assert await _published(broker, c.DEVICE_GW_01, topic, _cmd_message(c.DEVICE_GW_01))
    assert not await _published(broker, c.DEVICE_APP_01, topic, _cmd_message(c.DEVICE_APP_01))
    assert not await _published(
        broker, c.DEVICE_MCU_CPP_01, topic, _cmd_message(c.DEVICE_MCU_CPP_01)
    )


async def test_acl_devices_publish_only_own_topics(broker: BrokerAddress) -> None:
    builder = EnvelopeBuilder(c.DEVICE_MCU_CPP_01, SystemClock(), SeqCounter())
    event = builder.build(c.KIND_EVENT, {"code": "BOOT", "severity": "info"}).to_dict()
    own = _topic(c.DEVICE_MCU_CPP_01, c.KIND_EVENT)
    other = _topic("mcu-cpp-02", c.KIND_EVENT)
    assert await _published(broker, c.DEVICE_MCU_CPP_01, own, event)
    assert not await _published(broker, c.DEVICE_MCU_CPP_01, other, event)
    assert not await _published(
        broker, c.DEVICE_MCU_CPP_01, _topic(c.DEVICE_GW_01, c.KIND_EVENT), event
    )
    # request は app-01 だけ、request/ack は gw-01 だけ
    req_topic = _topic(c.DEVICE_APP_01, c.KIND_REQUEST)
    assert not await _published(broker, c.DEVICE_GW_01, req_topic, event)
    ack_topic = _topic(c.DEVICE_APP_01, c.KIND_REQUEST_ACK)
    assert not await _published(broker, c.DEVICE_APP_01, ack_topic, event)
    assert await _published(broker, c.DEVICE_GW_01, ack_topic, event)


# ---------------------------------------------------------------- LWT


async def test_lwt_offline_state_is_accepted_despite_seq(broker: BrokerAddress) -> None:
    """接続が切れたときの LWT は、seq が online の state と同じでも破棄されない（#29）。"""
    sim = Simulation(load_scenario("normal_day"), speed=0)
    device = next(d for d in sim.mqtt_devices if d.device_id == c.DEVICE_MCU_CPP_01)
    state_topic = _topic(c.DEVICE_MCU_CPP_01, c.KIND_STATE)
    window = SeqDedupWindow()
    async with observing(broker, state_topic) as observer:
        client = client_for(broker, device.device_id, device.will())
        await client.__aenter__()
        device.start()
        for out in device.drain():
            await client.publish(out.topic, out.payload(), qos=out.qos, retain=out.retain)
        await asyncio.sleep(0.5)
        # DISCONNECT を送らずに切る（ネットワーク断と同じ）→ ブローカーが LWT を発行する
        sock = client._client.socket()
        assert sock is not None
        sock.close()
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)
        await asyncio.sleep(2.0)
    states = [m for m in observer.on("/state") if m["boot_id"] == device.boot_id]  # type: ignore[attr-defined]
    assert [m["payload"]["status"] for m in states] == ["online", "offline"]
    online, offline = states
    assert offline["seq"] == online["seq"] + 1 or offline["seq"] == online["seq"]
    assert validate(offline) == []
    assert window.accept(online, c.KIND_STATE) is True
    assert window.accept(online, c.KIND_STATE) is False
    assert window.accept(offline, c.KIND_STATE) is True
    # 後片付け：retain された offline を消す
    async with client_for(broker, c.DEVICE_MCU_CPP_01) as cleaner:
        await cleaner.publish(state_topic, b"", qos=1, retain=True)
