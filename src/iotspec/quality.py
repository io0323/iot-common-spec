"""受信側が判定するデータ品質（第5章）。

境界の扱いは spec.md の語に合わせる：
「超えた」「超」は厳密に大きい場合、「以内」は等しい場合を含む。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from iotspec.clock import ensure_utc
from iotspec.constants import (
    CLOCK_SKEW_THRESHOLD_S,
    LATE_MAX_AGE_S,
    LATE_PERIOD_MULTIPLIER,
    STALE_PERIOD_MULTIPLIER,
)

__all__ = ["ClockSkew", "Lateness", "classify_clock_skew", "classify_late", "is_stale"]

ClockSkew = Literal["ok", "skew"]
Lateness = Literal["on_time", "late"]


def is_stale(last_update: datetime, period_ms: int, now: datetime) -> bool:
    """最終更新から period_ms の3倍を超えたら True（ちょうど3倍は False）。"""
    elapsed = ensure_utc(now) - ensure_utc(last_update)
    return elapsed > timedelta(milliseconds=period_ms * STALE_PERIOD_MULTIPLIER)


def classify_clock_skew(ts: datetime, received_at: datetime) -> ClockSkew:
    """ts と受信時刻の差が±5分を超えたら "skew"（ちょうど5分は "ok"）。

    この判定を使うのは ts_source: device の電文だけ（gateway の ts は受信時刻なので）。
    skew なら受信側は ts を受信時刻に置き換え、ts_source: gateway とする。
    """
    diff = abs(ensure_utc(received_at) - ensure_utc(ts))
    return "skew" if diff > timedelta(seconds=CLOCK_SKEW_THRESHOLD_S) else "ok"


def classify_late(ts: datetime, received_at: datetime, period_ms: int) -> Lateness:
    """遅延到着の判定。ts が受信時刻より過去で、差が周期の3倍を超え、かつ5分以内なら "late"。

    差が5分を超えるものは時刻ずれ（classify_clock_skew）の扱いなので "on_time" を返す。
    呼び出し側は先に classify_clock_skew を判定する。late なら最新値を更新しない。
    """
    age = ensure_utc(received_at) - ensure_utc(ts)
    threshold = timedelta(milliseconds=period_ms * LATE_PERIOD_MULTIPLIER)
    if threshold < age <= timedelta(seconds=LATE_MAX_AGE_S):
        return "late"
    return "on_time"
