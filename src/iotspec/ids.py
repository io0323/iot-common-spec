"""ID の生成（第4章「IDの種類と生成する作品」）。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from ulid import ULID

from iotspec.clock import Clock, ensure_utc, format_ts
from iotspec.constants import BOOT_ID_BITS

__all__ = ["new_boot_id", "new_ulid", "schedule_request_id"]

_ULID_RANDOM_BYTES = 10  # ULID の乱数部 80 ビット
_ULID_TIME_BYTES = 6  # ULID の時刻部 48 ビット（Unix ミリ秒）
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def new_ulid(clock: Clock) -> str:
    """時刻部を clock.now() にした ULID（msg_id・cmd_id・手動操作の request_id 用）。"""
    return str(ULID.from_datetime(clock.now()))


def new_boot_id() -> str:
    """起動ごとの boot_id。32 ビットの乱数を16進小文字8桁で返す（v0.2.1 で確定）。"""
    return f"{secrets.randbits(BOOT_ID_BITS):0{BOOT_ID_BITS // 4}x}"


def schedule_request_id(schedule_id: int, planned_at: datetime) -> str:
    """スケジュール実行の request_id を決定的に作る（第4章）。

    時刻部 = planned_at（Unix ミリ秒）、
    乱数部 = SHA-256(f"{schedule_id}|{planned_at の ISO 8601 ミリ秒・末尾Z}") の先頭 80 ビット。
    作品04 が再起動して同じ予定を送り直しても同じ値になり、03 が重複として弾ける。
    """
    planned_at = ensure_utc(planned_at)
    ts_text = format_ts(planned_at)
    # ミリ秒未満は切り捨て（ISO 文字列側と同じ扱い）。浮動小数を経由せず整数で求める
    millis = (planned_at - _EPOCH) // timedelta(milliseconds=1)
    digest = hashlib.sha256(f"{schedule_id}|{ts_text}".encode()).digest()
    raw = millis.to_bytes(_ULID_TIME_BYTES, "big") + digest[:_ULID_RANDOM_BYTES]
    return str(ULID.from_bytes(raw))
