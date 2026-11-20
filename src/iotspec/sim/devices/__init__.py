"""仮想デバイス（作品01〜04の代役）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

__all__ = ["MqttDevice", "Outgoing"]


@dataclass(frozen=True)
class Outgoing:
    """デバイスが発行する MQTT メッセージ。"""

    topic: str
    message: dict[str, object]
    qos: int
    retain: bool

    @property
    def kind(self) -> str:
        return self.topic.split("/", 4)[4]

    def payload(self) -> bytes:
        return json.dumps(self.message, ensure_ascii=False, separators=(",", ":")).encode()


class MqttDevice(Protocol):
    """MQTT につながる代役（mcu-cpp-01、gw-01 簡易版、app-01）の共通の形。"""

    device_id: str

    def subscriptions(self) -> list[str]: ...

    def will(self) -> Outgoing: ...

    def shutdown(self) -> Outgoing: ...

    def start(self) -> None: ...

    def tick(self) -> None: ...

    def drain(self) -> list[Outgoing]: ...

    def handle_message(self, topic: str, payload: bytes) -> None: ...
