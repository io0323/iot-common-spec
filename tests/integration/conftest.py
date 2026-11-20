"""結合テストの共通の準備。

前提：deploy/mosquitto/gen_passwd.sh でパスワードファイルを作り、docker compose up -d mosquitto。
接続先は IOTSPEC_MQTT_URL（既定 mqtt://localhost:1883）、パスワードは IOTSPEC_MQTT_PASSWORD
（未設定なら gen_passwd.sh の既定値 iotspec-dev）。
ブローカーに接続できなければ理由を表示して skip する。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiomqtt
import pytest

from iotspec.sim.mqtt import MQTT_PASSWORD_ENV, BrokerAddress, broker_from_env, client_for

DEV_PASSWORD = "iotspec-dev"
CONNECT_TIMEOUT_S = 3.0
OBSERVER = "observer"


@pytest.fixture
async def broker(monkeypatch: pytest.MonkeyPatch) -> BrokerAddress:
    if MQTT_PASSWORD_ENV not in os.environ:
        monkeypatch.setenv(MQTT_PASSWORD_ENV, DEV_PASSWORD)
    address = broker_from_env()
    username, password = address.credentials_for(OBSERVER)
    try:
        async with aiomqtt.Client(
            address.host,
            address.port,
            username=username,
            password=password,
            timeout=CONNECT_TIMEOUT_S,
        ):
            pass
    except (aiomqtt.MqttError, OSError) as exc:
        pytest.skip(f"MQTT ブローカーに接続できない（{address.host}:{address.port}）: {exc}")
    return address


class Observer:
    """iot/# を購読して受けた電文を貯める（observer ユーザー：読むだけ）。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def on(self, topic_suffix: str) -> list[dict[str, Any]]:
        return [m for t, m in self.messages if t.endswith(topic_suffix)]


@asynccontextmanager
async def observing(broker: BrokerAddress, topic: str = "iot/#") -> AsyncIterator[Observer]:
    observer = Observer()
    async with client_for(broker, OBSERVER, unique=True) as client:
        await client.subscribe(topic, qos=1)

        async def collect() -> None:
            async for message in client.messages:
                if isinstance(message.payload, bytes | bytearray):
                    observer.messages.append((str(message.topic), json.loads(message.payload)))

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)
        try:
            yield observer
        finally:
            await asyncio.sleep(0.5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
