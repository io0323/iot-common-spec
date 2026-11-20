"""時計と時刻の表記（CLAUDE.md「規約」）。

時刻を読む処理はすべて Clock を注入して使い、テストでは VirtualClock に差し替える。
時刻は UTC の aware datetime で扱い、電文では ISO 8601 ミリ秒・末尾 Z で表す（第3章）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "VirtualClock", "ensure_utc", "format_ts", "parse_ts"]

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@runtime_checkable
class Clock(Protocol):
    """現在時刻（UTC の aware datetime）を返すもの。"""

    def now(self) -> datetime: ...


def ensure_utc(dt: datetime) -> datetime:
    """aware datetime を UTC に直す。naive なら ValueError。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime は扱わない: {dt!r}")
    return dt.astimezone(UTC)


def format_ts(dt: datetime) -> str:
    """'2026-11-20T06:00:12.345Z' の形にする。ミリ秒未満は切り捨てる。"""
    dt = ensure_utc(dt)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def parse_ts(text: str) -> datetime:
    """ISO 8601 ミリ秒・末尾 Z の文字列を UTC の aware datetime にする。形式違いは ValueError。"""
    if not _TS_RE.match(text):
        raise ValueError(f"ts の形式ではない: {text!r}")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


class SystemClock:
    """OS の時計。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class VirtualClock:
    """手で進める時計（テスト・シミュレータ用）。"""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | float) -> datetime:
        """delta（timedelta または秒）だけ進め、進めたあとの時刻を返す。負は不可。"""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        if step < timedelta(0):
            raise ValueError("時計を戻すことはできない")
        self._now += step
        return self._now

    def set(self, dt: datetime) -> None:
        """時刻を指定の値にする（シナリオの開始時刻の設定など）。"""
        self._now = ensure_utc(dt)
