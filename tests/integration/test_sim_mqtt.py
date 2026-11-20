"""Phase 4 結合テスト：Mosquitto に接続して normal_day を流す。

前提：docker compose up -d mosquitto（接続先は IOTSPEC_MQTT_URL、既定 mqtt://localhost:1883）。
ブローカーに接続できないときは理由を表示して skip する。
normal_day を60倍速で1分間（仮想1時間）流す。実時間の長さは IOTSPEC_IT_SIM_SECONDS で変えられる。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections import defaultdict
from dataclasses import replace
from typing import Any

import aiomqtt
import pytest

from iotspec.constants import DEFAULT_SITE, DEVICE_MCU_CPP_01
from iotspec.schema import validate
from iotspec.sim.mqtt import BrokerAddress, broker_from_env, run_over_mqtt
from iotspec.sim.scenario import load_scenario
from iotspec.sim.simulation import Simulation

pytestmark = pytest.mark.integration

SPEED = 60.0
REAL_SECONDS = float(os.environ.get("IOTSPEC_IT_SIM_SECONDS", "60"))
CONNECT_TIMEOUT_S = 3.0
SETTLE_S = 1.0


async def _require_broker() -> BrokerAddress:
    broker = broker_from_env()
    try:
        async with aiomqtt.Client(
            broker.host,
            broker.port,
            username=broker.username,
            password=broker.password,
            timeout=CONNECT_TIMEOUT_S,
        ):
            pass
    except (aiomqtt.MqttError, OSError) as exc:
        pytest.skip(f"MQTT ブローカーに接続できない（{broker.host}:{broker.port}）: {exc}")
    return broker


async def test_normal_day_over_mqtt_conforms_and_seq_is_consecutive() -> None:
    broker = await _require_broker()
    scenario = load_scenario("normal_day")
    scenario = replace(scenario, duration_s=REAL_SECONDS * SPEED)
    simulation = Simulation(scenario, speed=SPEED)
    boot_id = simulation.devices[0].boot_id
    received: list[dict[str, Any]] = []

    async with aiomqtt.Client(
        broker.host, broker.port, username=broker.username, password=broker.password
    ) as observer:
        await observer.subscribe(f"iot/v1/{DEFAULT_SITE}/{DEVICE_MCU_CPP_01}/#", qos=1)

        async def collect() -> None:
            async for message in observer.messages:
                if isinstance(message.payload, bytes | bytearray):
                    received.append(json.loads(message.payload))

        task = asyncio.create_task(collect())
        await run_over_mqtt(simulation, broker)
        await asyncio.sleep(SETTLE_S)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # 前回の実行で retain された state は除く
    ours = [m for m in received if m.get("boot_id") == boot_id]
    assert len(ours) >= REAL_SECONDS * SPEED * 0.9
    invalid = [(m.get("schema"), validate(m)) for m in received if validate(m)]
    assert invalid == []
    seqs: dict[str, list[int]] = defaultdict(list)
    for m in ours:
        assert m["device_id"] == DEVICE_MCU_CPP_01
        seqs[m["schema"]].append(m["seq"])
    for schema, values in seqs.items():
        steps = {b - a for a, b in itertools.pairwise(values)}
        assert steps <= {1}, (schema, sorted(steps))
