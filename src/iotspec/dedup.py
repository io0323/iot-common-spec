"""seq による重複排除（第5章「重複」、第4章「送信量の上限」）。

重複の判定は（device_id, boot_id, kind, seq）で行い、msg_id は使わない。
発信元・kind ごとに直近 SEQ_DEDUP_CAPACITY 個の seq を記憶する。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iotspec.constants import KIND_STATE, SCHEMA_NAMES, SEQ_DEDUP_CAPACITY

__all__ = ["SeqDedupWindow", "exempt_from_seq_checks"]


def exempt_from_seq_checks(message: Mapping[str, Any]) -> bool:
    """seq による重複・欠損・遅延の判定から外す電文なら True（v0.2.1 #29）。

    status: offline の state（LWT と正常終了時の offline）は、接続時に作られ seq を進めないので、
    受信側は seq の判定に使わず必ず受け入れる。
    """
    payload = message.get("payload")
    return (
        message.get("schema") == SCHEMA_NAMES[KIND_STATE]
        and isinstance(payload, Mapping)
        and payload.get("status") == "offline"
    )


@dataclass
class _Window:
    boot_id: str
    order: deque[int] = field(default_factory=deque)
    seen: set[int] = field(default_factory=set)


class SeqDedupWindow:
    """発信元・kind ごとに、直近 capacity 個の seq を記憶する重複判定器。

    - 記憶にある seq が再び来たら重複（True）
    - boot_id が変わったら、その発信元・kind の記憶を捨てて新しい起動として数える
    - 記憶は受け取った順に古いものから捨てる。捨てた seq が再び来ても重複とは判定しない
      （再配送は確認待ち 32 件の範囲に限られるので、1,024 個で足りる。第4章）
    """

    def __init__(self, capacity: int = SEQ_DEDUP_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity は1以上")
        self.capacity = capacity
        self._windows: dict[tuple[str, str], _Window] = {}

    def is_duplicate(self, device_id: str, boot_id: str, kind: str, seq: int) -> bool:
        """重複なら True。初めての seq なら記憶して False。"""
        key = (device_id, kind)
        window = self._windows.get(key)
        if window is None or window.boot_id != boot_id:
            window = _Window(boot_id=boot_id)
            self._windows[key] = window
        if seq in window.seen:
            return True
        window.order.append(seq)
        window.seen.add(seq)
        if len(window.order) > self.capacity:
            window.seen.discard(window.order.popleft())
        return False

    def accept(self, message: Mapping[str, Any], kind: str) -> bool:
        """電文を受け入れるなら True（重複なら False）。offline の state は記憶せずに受け入れる。"""
        if exempt_from_seq_checks(message):
            return True
        return not self.is_duplicate(
            str(message["device_id"]), str(message["boot_id"]), kind, int(message["seq"])
        )

    def remembered(self, device_id: str, kind: str) -> int:
        """その発信元・kind で記憶している seq の数。"""
        window = self._windows.get((device_id, kind))
        return 0 if window is None else len(window.order)
