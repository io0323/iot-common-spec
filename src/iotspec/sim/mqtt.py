"""シミュレータのデバイスを MQTT ブローカーにつなぐ（aiomqtt）。

接続先は環境変数 IOTSPEC_MQTT_URL（既定 mqtt://localhost:1883）。URL に user:pass を含めてよい。
デバイスごとに1つの接続を持ち、client id は device_id、LWT は offline の state（retain）。
確認待ち（QoS 1 の未確認送信）は MQTT_MAX_IN_FLIGHT 件まで（第4章）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import aiomqtt

from iotspec.constants import KIND_CMD, MQTT_MAX_IN_FLIGHT, QOS_BY_KIND
from iotspec.sim.devices import Outgoing
from iotspec.sim.devices.mcu_cpp_01 import McuCpp01
from iotspec.sim.simulation import Simulation

__all__ = ["DEFAULT_MQTT_URL", "MQTT_URL_ENV", "BrokerAddress", "broker_from_env", "run_over_mqtt"]

MQTT_URL_ENV = "IOTSPEC_MQTT_URL"
DEFAULT_MQTT_URL = "mqtt://localhost:1883"
_DEFAULT_PORT = 1883


@dataclass(frozen=True)
class BrokerAddress:
    host: str
    port: int = _DEFAULT_PORT
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, text: str) -> BrokerAddress:
        """'mqtt://user:pass@host:port' または 'host:port' を読む。"""
        if "://" not in text:
            text = f"mqtt://{text}"
        url = urlparse(text)
        if url.scheme not in ("mqtt", "tcp"):
            raise ValueError(f"対応していない URL の形式: {text!r}")
        if not url.hostname:
            raise ValueError(f"ホスト名がない: {text!r}")
        return cls(url.hostname, url.port or _DEFAULT_PORT, url.username, url.password)


def broker_from_env() -> BrokerAddress:
    return BrokerAddress.parse(os.environ.get(MQTT_URL_ENV, DEFAULT_MQTT_URL))


def _client(device: McuCpp01, broker: BrokerAddress) -> aiomqtt.Client:
    will = device.will()
    return aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        username=broker.username,
        password=broker.password,
        identifier=device.device_id,
        will=aiomqtt.Will(will.topic, will.payload(), qos=will.qos, retain=will.retain),
        max_inflight_messages=MQTT_MAX_IN_FLIGHT,
    )


async def _publish(client: aiomqtt.Client, items: list[Outgoing]) -> None:
    for item in items:
        await client.publish(item.topic, item.payload(), qos=item.qos, retain=item.retain)


async def run_over_mqtt(simulation: Simulation, broker: BrokerAddress) -> None:
    """シミュレーションを最後まで流し、各デバイスの電文を MQTT に発行する。"""
    simulation.schedule()
    async with contextlib.AsyncExitStack() as stack:
        clients: dict[str, aiomqtt.Client] = {}
        for device in simulation.devices:
            client = await stack.enter_async_context(_client(device, broker))
            await client.subscribe(device.cmd_topic, qos=QOS_BY_KIND[KIND_CMD])
            clients[device.device_id] = client

        async def flush() -> None:
            for device in simulation.devices:
                await _publish(clients[device.device_id], device.drain())

        async def receive(device: McuCpp01, client: aiomqtt.Client) -> None:
            async for message in client.messages:
                payload = message.payload
                if isinstance(payload, bytes | bytearray):
                    device.handle_message(str(message.topic), bytes(payload))
                    await _publish(client, device.drain())

        receivers = [
            asyncio.create_task(receive(d, clients[d.device_id])) for d in simulation.devices
        ]
        try:
            await simulation.runtime.run_until_async(simulation.scenario.end, after_step=flush)
            await flush()
            for device in simulation.devices:
                await _publish(clients[device.device_id], [device.shutdown()])
        finally:
            for task in receivers:
                task.cancel()
            await asyncio.gather(*receivers, return_exceptions=True)
