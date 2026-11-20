"""VirtualClock で動くイベントループ（第8章：倍速実行と過去データの一括生成）。

予定した処理を時刻順に実行し、実行する直前に VirtualClock をその時刻に進める。
- speed > 0：仮想時間の経過 ÷ speed だけ実時間で待つ（倍速実行。60 なら1分を1秒で）
- speed == 0：待たずに次々に実行する（過去データの一括生成）
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from iotspec.clock import VirtualClock, ensure_utc

__all__ = ["Runtime"]

Callback = Callable[[], None]
AfterStep = Callable[[], Awaitable[None]]


@dataclass(order=True)
class _Entry:
    at: datetime
    order: int
    callback: Callback = field(compare=False)
    period: timedelta | None = field(compare=False, default=None)


class Runtime:
    def __init__(self, clock: VirtualClock, speed: float = 0.0) -> None:
        if speed < 0:
            raise ValueError("speed は0以上")
        self.clock = clock
        self.speed = speed
        self._queue: list[_Entry] = []
        self._order = itertools.count()

    def at(self, when: datetime, callback: Callback) -> None:
        """when に一度だけ実行する。"""
        heapq.heappush(self._queue, _Entry(ensure_utc(when), next(self._order), callback))

    def every(self, period_s: float, callback: Callback, *, first: datetime | None = None) -> None:
        """first（省略時は今）から period_s ごとに実行する。"""
        if period_s <= 0:
            raise ValueError("period_s は正")
        start = ensure_utc(first) if first is not None else self.clock.now()
        period = timedelta(seconds=period_s)
        heapq.heappush(self._queue, _Entry(start, next(self._order), callback, period))

    def _pop_due(self, until: datetime) -> _Entry | None:
        if not self._queue or self._queue[0].at > until:
            return None
        entry = heapq.heappop(self._queue)
        if entry.period is not None:
            heapq.heappush(
                self._queue,
                _Entry(entry.at + entry.period, next(self._order), entry.callback, entry.period),
            )
        return entry

    def run_until(self, until: datetime, after_step: Callback | None = None) -> int:
        """until までの予定を待たずに実行し、実行した件数を返す（一括生成・単体テスト用）。"""
        until = ensure_utc(until)
        count = 0
        while (entry := self._pop_due(until)) is not None:
            self.clock.set(max(entry.at, self.clock.now()))
            entry.callback()
            count += 1
            if after_step is not None:
                after_step()
        if self.clock.now() < until:
            self.clock.set(until)
        return count

    async def run_until_async(self, until: datetime, after_step: AfterStep | None = None) -> int:
        """until までの予定を speed に合わせて実時間で待ちながら実行する。

        after_step は各処理のあとに呼ぶ（送信待ちの電文を MQTT に流すなど）。
        """
        until = ensure_utc(until)
        count = 0
        while (entry := self._pop_due(until)) is not None:
            wait = (entry.at - self.clock.now()).total_seconds()
            if self.speed > 0 and wait > 0:
                await asyncio.sleep(wait / self.speed)
            self.clock.set(max(entry.at, self.clock.now()))
            entry.callback()
            count += 1
            if after_step is not None:
                await after_step()
            else:
                await asyncio.sleep(0)
        if self.clock.now() < until:
            self.clock.set(until)
        return count
