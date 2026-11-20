"""契約テストの実行環境：MQTT の観測、相手役の接続、UART の相手役、対象の起動・強制終了。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import aiomqtt

from iotspec.clock import SystemClock, parse_ts
from iotspec.constants import (
    DEFAULT_SITE,
    TOPIC_FORMAT,
    UART_HEARTBEAT_PERIOD_S,
    UART_SEQ_MODULO,
)
from iotspec.contract.core import Skip
from iotspec.envelope import EnvelopeBuilder, SeqCounter
from iotspec.sim.mqtt import BrokerAddress, client_for, run_over_mqtt
from iotspec.sim.serial import DevicePort, OwnPty, SerialLink, add_reader
from iotspec.sim.simulation import Simulation
from iotspec.uart.frames import Discarded, Frame, Heartbeat, decode_frame, encode_frame

__all__ = [
    "ContractContext",
    "ExternalTarget",
    "Observed",
    "SimulationTarget",
    "SubprocessTarget",
    "Target",
    "UartPeer",
]

OBSERVER_USER = "observer"
_POLL_S = 0.05


# ---------------------------------------------------------------- MQTT の観測


@dataclass(frozen=True)
class Observed:
    """受信した MQTT メッセージ1件。at は受信した時刻（time.monotonic）。"""

    at: float
    topic: str
    message: dict[str, Any] | None
    raw: bytes
    retain: bool

    @property
    def device_id(self) -> str:
        return self.topic.split("/", 4)[3]

    @property
    def kind(self) -> str:
        return self.topic.split("/", 4)[4]

    def ref(self) -> str:
        m = self.message or {}
        return f"{self.topic} seq={m.get('seq')} msg_id={m.get('msg_id')}"

    def ts(self) -> datetime | None:
        try:
            return parse_ts(str((self.message or {}).get("ts")))
        except ValueError:
            return None


class Observer:
    """iot/v1/{site}/# を observer ユーザーで購読して貯める。"""

    def __init__(self) -> None:
        self.messages: list[Observed] = []

    def select(
        self, *, device_id: str | None = None, kind: str | None = None, since: float = 0.0
    ) -> list[Observed]:
        return [
            o
            for o in self.messages
            if o.at >= since
            and (device_id is None or o.device_id == device_id)
            and (kind is None or o.kind == kind)
        ]

    async def wait_for(
        self, predicate: Callable[[Observed], bool], timeout: float, *, since: float = 0.0
    ) -> Observed | None:
        deadline = time.monotonic() + timeout
        while True:
            for o in self.messages:
                if o.at >= since and predicate(o):
                    return o
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_S)


# ---------------------------------------------------------------- UART の相手役


@dataclass
class ReceivedFrame:
    at: float
    raw: bytes
    frame: Frame | Discarded


class UartPeer:
    """UART の相手役。受けたフレームを貯め、種別ごとの連番でフレームを送る。"""

    def __init__(self, link: SerialLink) -> None:
        self.link = link
        self.received: list[ReceivedFrame] = []
        self._buffer = bytearray()
        self._seq: dict[int, int] = {}
        self._remove: Callable[[], None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._uptime_start = time.monotonic()
        self.on_frame: Callable[[Frame], None] | None = None

    def start(self) -> None:
        self._remove = add_reader(self.link, self._on_data)

    def _on_data(self, data: bytes) -> None:
        self._buffer += data
        while (index := self._buffer.find(0)) >= 0:
            chunk = bytes(self._buffer[: index + 1])
            del self._buffer[: index + 1]
            if len(chunk) > 1:
                frame = decode_frame(chunk)
                self.received.append(ReceivedFrame(time.monotonic(), chunk, frame))
                if self.on_frame is not None and not isinstance(frame, Discarded):
                    self.on_frame(frame)

    def next_seq(self, frame_type: int) -> int:
        seq = self._seq.get(frame_type, 0)
        self._seq[frame_type] = (seq + 1) % UART_SEQ_MODULO
        return seq

    def set_next_seq(self, frame_type: int, seq: int) -> None:
        self._seq[frame_type] = seq % UART_SEQ_MODULO

    def uptime_ms(self) -> int:
        return int((time.monotonic() - self._uptime_start) * 1000) & 0xFFFFFFFF

    def reset_uptime(self, uptime_ms: int) -> None:
        self._uptime_start = time.monotonic() - uptime_ms / 1000

    def send(self, frame: Frame) -> None:
        self.link.write(encode_frame(frame))

    def send_raw(self, data: bytes) -> None:
        self.link.write(data)

    def frames(self, cls: type[Any], *, since: float = 0.0) -> list[Any]:
        return [r.frame for r in self.received if r.at >= since and isinstance(r.frame, cls)]

    async def wait_frame(
        self, predicate: Callable[[Frame], bool], timeout: float, *, since: float = 0.0
    ) -> Frame | None:
        deadline = time.monotonic() + timeout
        while True:
            for r in self.received:
                if r.at >= since and not isinstance(r.frame, Discarded) and predicate(r.frame):
                    return r.frame
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_S)

    def start_heartbeat(self) -> None:
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._beat())

    async def stop_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None

    async def _beat(self) -> None:
        while True:
            self.send(Heartbeat(self.next_seq(Heartbeat.TYPE), self.uptime_ms()))
            await asyncio.sleep(UART_HEARTBEAT_PERIOD_S)

    async def close(self) -> None:
        await self.stop_heartbeat()
        if self._remove is not None:
            self._remove()
        self.link.close()


# ---------------------------------------------------------------- 対象の起動・強制終了


class Target(Protocol):
    controllable: bool

    async def start(self, ctx: ContractContext) -> None: ...

    async def kill(self) -> None: ...

    async def stop(self) -> None: ...


class ExternalTarget:
    """すでに動いている対象（契約テストからは起動も停止もしない）。"""

    controllable = False

    async def start(self, ctx: ContractContext) -> None:
        return None

    async def kill(self) -> None:
        raise Skip("対象を強制終了できない")

    async def stop(self) -> None:
        return None


class SubprocessTarget:
    """--target-cmd で起動するプロセス。

    コマンドの {uart} は相手役の仮想シリアルのパスに置き換える。
    """

    controllable = True

    def __init__(self, command: str) -> None:
        self.command = command
        self.process: subprocess.Popen[bytes] | None = None

    async def start(self, ctx: ContractContext) -> None:
        command = self.command.replace("{uart}", ctx.uart_peer_path or "")
        env = dict(os.environ)
        if ctx.uart_peer_path:
            env["IOTSPEC_UART_PATH"] = ctx.uart_peer_path
        self.process = subprocess.Popen(shlex.split(command), env=env, start_new_session=True)

    async def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            await asyncio.to_thread(self.process.wait)

    async def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 5)
            except TimeoutError:
                await self.kill()


class SimulationTarget:
    """このリポジトリの代役を同じプロセスで動かす（自己テスト用）。"""

    controllable = True

    def __init__(self, make: Callable[[ContractContext], Simulation]) -> None:
        self._make = make
        self.simulation: Simulation | None = None
        self._task: asyncio.Task[None] | None = None
        self._kill = asyncio.Event()

    async def start(self, ctx: ContractContext) -> None:
        self.simulation = self._make(ctx)
        self._task = asyncio.create_task(
            run_over_mqtt(self.simulation, ctx.broker, kill=self._kill)
        )
        await asyncio.sleep(0.3)

    async def kill(self) -> None:
        self._kill.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            await self.kill()


# ---------------------------------------------------------------- 実行環境


@dataclass
class ContractContext:
    role: str
    broker: BrokerAddress
    target: Target
    device_id: str
    site: str = DEFAULT_SITE
    uart_path: str | None = None
    """--uart：対象が作ったシリアルポートを開く。None なら契約テストが pty を作る。"""
    options: dict[str, Any] = field(default_factory=dict)
    observer: Observer = field(default_factory=Observer)
    uart: UartPeer | None = None
    uart_peer_path: str | None = None
    _stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)
    _clients: dict[str, aiomqtt.Client] = field(default_factory=dict)
    _builders: dict[str, EnvelopeBuilder] = field(default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    """観測を始めた時刻（time.monotonic）。"""

    def topic(self, device_id: str, kind: str) -> str:
        return TOPIC_FORMAT.format(site=self.site, device_id=device_id, kind=kind)

    async def open(self, *, uart: bool) -> None:
        """観測と UART の相手役を用意する（対象を起動する前に呼ぶ）。"""
        self.started_at = time.monotonic()
        observer = await self._stack.enter_async_context(
            client_for(self.broker, OBSERVER_USER, unique=True)
        )
        await observer.subscribe(f"iot/v1/{self.site}/#", qos=1)
        self._tasks.append(asyncio.create_task(self._collect(observer)))
        if uart:
            if self.uart_path:
                link: SerialLink = DevicePort(self.uart_path)
            else:
                own = OwnPty()
                self.uart_peer_path = own.peer_path
                link = own
                print(
                    f"契約テストの仮想シリアル: {own.peer_path}（対象はこのパスを開く）", flush=True
                )
            self.uart = UartPeer(link)
            self.uart.start()
        await asyncio.sleep(0.3)

    async def _collect(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
            try:
                parsed = json.loads(raw) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = None
            self.observer.messages.append(
                Observed(
                    time.monotonic(), str(message.topic), parsed, bytes(raw), bool(message.retain)
                )
            )

    async def client(self, user: str) -> aiomqtt.Client:
        """user（device_id）の名義で接続した相手役のクライアント。"""
        if user not in self._clients:
            self._clients[user] = await self._stack.enter_async_context(
                client_for(self.broker, user)
            )
        return self._clients[user]

    def builder(self, user: str) -> EnvelopeBuilder:
        if user not in self._builders:
            self._builders[user] = EnvelopeBuilder(user, SystemClock(), SeqCounter())
        return self._builders[user]

    async def publish_as(
        self, user: str, kind: str, payload: dict[str, Any], *, topic_device: str
    ) -> dict[str, Any]:
        message = self.builder(user).build(kind, payload).to_dict()
        client = await self.client(user)
        await client.publish(self.topic(topic_device, kind), json.dumps(message), qos=1)
        return message

    async def retained(self, topic: str, timeout: float = 2.0) -> dict[str, Any] | None:
        """新しく購読して retain されている電文を読む。"""
        async with client_for(self.broker, OBSERVER_USER, unique=True) as client:
            await client.subscribe(topic, qos=1)
            try:
                async with asyncio.timeout(timeout):
                    async for message in client.messages:
                        if message.retain and isinstance(message.payload, bytes | bytearray):
                            return dict(json.loads(message.payload))
            except TimeoutError:
                return None
        return None

    def spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.create_task(coro))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.uart is not None:
            await self.uart.close()
        with contextlib.suppress(Exception):
            await self._stack.aclose()


def first(items: Iterable[Any]) -> Any:
    return next(iter(items), None)
