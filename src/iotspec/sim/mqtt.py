"""シミュレータのデバイスを MQTT ブローカーと仮想シリアルにつなぐ（aiomqtt）。

- 接続先は環境変数 IOTSPEC_MQTT_URL（既定 mqtt://localhost:1883）
- 各デバイスの MQTT のユーザー名は device_id（v0.2.1 ACL）。パスワードは環境変数
  IOTSPEC_MQTT_PASSWORD（未設定なら認証なしで接続する）
- client id は device_id、LWT は offline の state（retain、seq を進めない。#29）
- 確認待ち（QoS 1 の未確認送信）は MQTT_MAX_IN_FLIGHT 件まで（第4章）
- mcu-c-01 は MQTT につながず UART だけを持つ。gw-01 簡易版が同じシナリオにいれば pty でつなぎ
  （mcu-c-01 が pty を作り、gw-01 がそのパスを開く）、いなければ pty のパスを表示して相手を待つ
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import aiomqtt

from iotspec.constants import MQTT_MAX_IN_FLIGHT
from iotspec.sim.devices import MqttDevice, Outgoing
from iotspec.sim.serial import DevicePort, OwnPty, SerialLink, add_reader
from iotspec.sim.simulation import Simulation

__all__ = [
    "DEFAULT_MQTT_URL",
    "MQTT_PASSWORD_ENV",
    "MQTT_URL_ENV",
    "BrokerAddress",
    "broker_from_env",
    "run_over_mqtt",
]

MQTT_URL_ENV = "IOTSPEC_MQTT_URL"
MQTT_PASSWORD_ENV = "IOTSPEC_MQTT_PASSWORD"
DEFAULT_MQTT_URL = "mqtt://localhost:1883"
_DEFAULT_PORT = 1883
_SUBSCRIBE_QOS = 1


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

    def credentials_for(self, device_id: str) -> tuple[str | None, str | None]:
        """デバイスが使うユーザー名とパスワード。

        パスワードが設定されていればユーザー名は device_id にする。
        """
        password = self.password or os.environ.get(MQTT_PASSWORD_ENV)
        if password is None:
            return self.username, None
        return device_id, password


def broker_from_env() -> BrokerAddress:
    return BrokerAddress.parse(os.environ.get(MQTT_URL_ENV, DEFAULT_MQTT_URL))


def client_for(
    broker: BrokerAddress, device_id: str, will: Outgoing | None = None
) -> aiomqtt.Client:
    """device_id の名義で接続する aiomqtt.Client（テストの観測用にも使う）。"""
    username, password = broker.credentials_for(device_id)
    return aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        username=username,
        password=password,
        identifier=device_id,
        will=(
            None
            if will is None
            else aiomqtt.Will(will.topic, will.payload(), qos=will.qos, retain=will.retain)
        ),
        max_inflight_messages=MQTT_MAX_IN_FLIGHT,
    )


async def _publish(client: aiomqtt.Client, items: list[Outgoing]) -> None:
    for item in items:
        await client.publish(item.topic, item.payload(), qos=item.qos, retain=item.retain)


def _open_uart(simulation: Simulation, stack: contextlib.ExitStack) -> list[tuple[str, SerialLink]]:
    """UART のつながりを作り、(役割, 片側) の並びを返す。役割は "mcu" か "gw"。"""
    mcu, gw = simulation.mcu_c, simulation.gw
    links: list[tuple[str, SerialLink]] = []
    if mcu is not None:
        path = simulation.configs[mcu.device_id].options.get("uart_path")
        mcu_link: SerialLink = DevicePort(path) if path else OwnPty()
        stack.callback(mcu_link.close)
        links.append(("mcu", mcu_link))
        if gw is None and isinstance(mcu_link, OwnPty):
            print(
                f"{mcu.device_id}: 仮想シリアルを作った。相手は {mcu_link.peer_path} を開く",
                flush=True,
            )
        if gw is not None and isinstance(mcu_link, OwnPty):
            gw_link = DevicePort(mcu_link.peer_path)
            stack.callback(gw_link.close)
            links.append(("gw", gw_link))
    elif gw is not None:
        path = simulation.configs[gw.device_id].options.get("uart_path")
        if path:
            gw_link = DevicePort(path)
            stack.callback(gw_link.close)
            links.append(("gw", gw_link))
    return links


async def run_over_mqtt(simulation: Simulation, broker: BrokerAddress) -> None:
    """シミュレーションを最後まで流し、MQTT と仮想シリアルでやり取りする。"""
    simulation.schedule()
    mqtt_devices: list[MqttDevice] = simulation.mqtt_devices
    with contextlib.ExitStack() as sync_stack:
        links = dict(_open_uart(simulation, sync_stack))
        async with contextlib.AsyncExitStack() as stack:
            clients: dict[str, aiomqtt.Client] = {}
            for device in mqtt_devices:
                client = await stack.enter_async_context(
                    client_for(broker, device.device_id, device.will())
                )
                for topic in device.subscriptions():
                    await client.subscribe(topic, qos=_SUBSCRIBE_QOS)
                clients[device.device_id] = client

            mcu, gw = simulation.mcu_c, simulation.gw
            removers = []
            if mcu is not None and "mcu" in links:
                removers.append(add_reader(links["mcu"], mcu.feed))
            if gw is not None and "gw" in links:
                removers.append(add_reader(links["gw"], gw.feed_uart))
            for remove in removers:
                stack.callback(remove)

            async def flush() -> None:
                if mcu is not None and "mcu" in links:
                    data = mcu.drain_tx()
                    if data:
                        links["mcu"].write(data)
                if gw is not None and "gw" in links:
                    data = gw.drain_uart()
                    if data:
                        links["gw"].write(data)
                for device in mqtt_devices:
                    await _publish(clients[device.device_id], device.drain())

            async def receive(device: MqttDevice, client: aiomqtt.Client) -> None:
                async for message in client.messages:
                    payload = message.payload
                    if isinstance(payload, bytes | bytearray):
                        device.handle_message(str(message.topic), bytes(payload))
                        await flush()

            receivers = [
                asyncio.create_task(receive(d, clients[d.device_id]))
                for d in mqtt_devices
                if d.subscriptions()
            ]
            try:
                end = simulation.scenario.end
                await simulation.runtime.run_until_async(end, after_step=flush)
                await flush()
                for device in mqtt_devices:
                    await _publish(clients[device.device_id], [device.shutdown()])
            finally:
                for task in receivers:
                    task.cancel()
                await asyncio.gather(*receivers, return_exceptions=True)
