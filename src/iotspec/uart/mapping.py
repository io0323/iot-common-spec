"""UART 上の値 ⇔ 共通仕様上の値（第7章「値の対応表」）。

対応表にない値は UnknownValueError（KeyError）。
変換する側は「値の形式不正」として破棄する（第5章）。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "EVENT_CODE",
    "QUALITY",
    "REASON",
    "SEVERITY",
    "STATUS",
    "UnknownValueError",
    "ValueTable",
]


class UnknownValueError(KeyError):
    """対応表にない値。"""


class ValueTable:
    """UART の整数値と共通仕様の値の双方向の対応表。"""

    def __init__(self, name: str, pairs: dict[int, str | None]) -> None:
        self.name = name
        self._to_common: dict[int, str | None] = dict(pairs)
        self._to_uart: dict[str | None, int] = {v: k for k, v in pairs.items()}
        if len(self._to_uart) != len(self._to_common):
            raise ValueError(f"{name}: 共通仕様側の値が重複している")

    def to_common(self, value: int) -> str | None:
        """UART の値 → 共通仕様の値。"""
        try:
            return self._to_common[value]
        except KeyError:
            raise UnknownValueError(f"{self.name}: 対応表にない UART の値 {value}") from None

    def to_uart(self, value: str | None) -> int:
        """共通仕様の値 → UART の値。"""
        try:
            return self._to_uart[value]
        except KeyError:
            raise UnknownValueError(f"{self.name}: 対応表にない値 {value!r}") from None

    def uart_values(self) -> list[int]:
        return list(self._to_common)

    def common_values(self) -> list[str | None]:
        return list(self._to_uart)


QUALITY: Final = ValueTable("quality", {0: "good", 1: "stale", 2: "fault"})
STATUS: Final = ValueTable("status", {0: "accepted", 1: "done", 2: "rejected", 3: "failed"})
REASON: Final = ValueTable(
    "reason",
    {0: None, 1: "unsafe", 2: "invalid_arg", 3: "unknown_op", 4: "expired", 5: "internal"},
)
EVENT_CODE: Final = ValueTable(
    "event code",
    {
        1: "BOOT",
        2: "STATE_CHANGED",
        3: "THRESHOLD_EXCEEDED",
        4: "THRESHOLD_CLEARED",
        5: "SENSOR_FAULT",
        6: "SENSOR_RECOVERED",
        7: "UPSTREAM_LOST",
    },
)
SEVERITY: Final = ValueTable("severity", {0: "info", 1: "warning", 2: "critical"})
