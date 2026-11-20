"""送信量の上限（第4章「送信量の上限」）。

発信元・kind ごとに、平均で毎秒 PUBLISH_RATE_AVG_PER_S 件、
瞬間的には毎秒 PUBLISH_RATE_BURST_PER_S 件まで。
- 平均：トークンバケット（補充 avg 件/秒、容量 burst 件）
- 瞬間：直近1秒（スライディングウィンドウ）の送信件数が burst 未満のときだけ送る
トークンバケットだけだと、満杯から burst 件送った直後の補充分で
1秒あたり burst を超えうるので両方で守る。
時刻は Clock から取る。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from iotspec.clock import Clock
from iotspec.constants import PUBLISH_RATE_AVG_PER_S, PUBLISH_RATE_BURST_PER_S

__all__ = ["RateLimiter"]

_WINDOW = timedelta(seconds=1)


class RateLimiter:
    def __init__(
        self,
        clock: Clock,
        rate_per_s: float = PUBLISH_RATE_AVG_PER_S,
        burst: int = PUBLISH_RATE_BURST_PER_S,
    ) -> None:
        self.clock = clock
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._tokens = float(burst)
        self._last = clock.now()
        self._sent: deque[datetime] = deque()

    def try_acquire(self) -> bool:
        """1件送ってよければ True（記録してから返す）。"""
        now = self.clock.now()
        elapsed = (now - self._last).total_seconds()
        if elapsed > 0:
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_s)
            self._last = now
        while self._sent and now - self._sent[0] >= _WINDOW:
            self._sent.popleft()
        if self._tokens < 1.0 or len(self._sent) >= self.burst:
            return False
        self._tokens -= 1.0
        self._sent.append(now)
        return True
