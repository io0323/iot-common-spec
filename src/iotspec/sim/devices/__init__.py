"""仮想デバイス（作品01〜04の代役）。"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["Outgoing"]


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
