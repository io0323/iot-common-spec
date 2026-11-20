"""リース（第4章「リース」）。

set_actuator はリース付きの上位指令。lease_s は省略時60秒、上限600秒（v0.2.1 で確定）。
lease_s が意味を持つのは set_actuator だけで、他の op では無視する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from iotspec.clock import ensure_utc
from iotspec.constants import LEASE_DEFAULT_S, LEASE_MAX_S, LEASE_MIN_S, LEASE_RENEW_DIVISOR

__all__ = ["InvalidLeaseError", "Lease", "renew_due", "resolve_lease_s"]

_LEASED_OP = "set_actuator"


class InvalidLeaseError(ValueError):
    """lease_s が範囲外・型違い（デバイスは invalid_arg で拒否する）。"""


def resolve_lease_s(cmd: Mapping[str, Any]) -> int | None:
    """cmd の payload から有効な lease_s を求める。

    - set_actuator 以外の op は None（lease_s は無視する）
    - set_actuator で省略されていれば LEASE_DEFAULT_S
    - 整数でない・LEASE_MIN_S 未満・LEASE_MAX_S 超なら InvalidLeaseError
    """
    if cmd.get("op") != _LEASED_OP:
        return None
    value = cmd.get("lease_s", LEASE_DEFAULT_S)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLeaseError(f"lease_s が整数ではない: {value!r}")
    if not LEASE_MIN_S <= value <= LEASE_MAX_S:
        raise InvalidLeaseError(f"lease_s が範囲外（{LEASE_MIN_S}–{LEASE_MAX_S}）: {value}")
    return value


@dataclass(frozen=True)
class Lease:
    """開始時刻と lease_s から期限と更新時期を求める。"""

    started_at: datetime
    lease_s: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        if not LEASE_MIN_S <= self.lease_s <= LEASE_MAX_S:
            raise InvalidLeaseError(f"lease_s が範囲外: {self.lease_s}")

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.lease_s)

    @property
    def renew_interval(self) -> timedelta:
        """更新の間隔（lease_s の3分の1。60秒なら20秒）。"""
        return timedelta(seconds=self.lease_s / LEASE_RENEW_DIVISOR)

    @property
    def renew_at(self) -> datetime:
        """03 が新しい cmd_id で送り直す時刻。"""
        return self.started_at + self.renew_interval

    def is_expired(self, now: datetime) -> bool:
        """期限に達したら True（ちょうど期限の時刻で切れる。open-questions #15）。"""
        return ensure_utc(now) >= self.expires_at


def renew_due(lease: Lease, now: datetime) -> bool:
    """更新時期（開始から lease_s の3分の1）に達していれば True。"""
    return ensure_utc(now) >= lease.renew_at
