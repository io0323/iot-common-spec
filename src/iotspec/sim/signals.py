"""計測値の信号生成（第8章：温度の日周変動、照度の昼夜、給水後の土壌水分上昇）。

乱数は seed とチャネル名から作るので、同じ seed・同じ呼び出し順なら同じ値になる。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from iotspec.clock import ensure_utc

__all__ = [
    "Constant",
    "Daylight",
    "Diurnal",
    "Signal",
    "SignalSet",
    "SoilMoisture",
    "build_signal",
]

_SECONDS_PER_HOUR = 3600.0
_HOURS_PER_DAY = 24.0
_PERMILLE = 1000.0
_SOIL_MIN, _SOIL_MAX = 0.0, 100.0


def _hour_of_day(t: datetime) -> float:
    t = ensure_utc(t)
    return t.hour + t.minute / 60 + (t.second + t.microsecond / 1e6) / _SECONDS_PER_HOUR


class Signal(Protocol):
    def sample(
        self, t: datetime, dt_s: float, inputs: Mapping[str, int], rng: random.Random
    ) -> float:
        """時刻 t の値。dt_s は前回からの経過秒、inputs はアクチュエーターの出力（‰）。"""
        ...


@dataclass
class Constant:
    value: float
    noise: float = 0.0

    def sample(
        self, t: datetime, dt_s: float, inputs: Mapping[str, int], rng: random.Random
    ) -> float:
        return self.value + rng.gauss(0.0, self.noise)


@dataclass
class Diurnal:
    """日周変動：mean + amplitude・cos(2π(時刻 − peak_hour)/24)。

    amplitude が負なら peak_hour が谷になる。
    """

    mean: float
    amplitude: float
    peak_hour: float
    noise: float = 0.0

    def sample(
        self, t: datetime, dt_s: float, inputs: Mapping[str, int], rng: random.Random
    ) -> float:
        phase = 2 * math.pi * (_hour_of_day(t) - self.peak_hour) / _HOURS_PER_DAY
        return self.mean + self.amplitude * math.cos(phase) + rng.gauss(0.0, self.noise)


@dataclass
class Daylight:
    """照度の昼夜：日の出から日の入りまで半周期の正弦、夜は0。"""

    max: float
    sunrise_hour: float
    sunset_hour: float
    noise: float = 0.0

    def sample(
        self, t: datetime, dt_s: float, inputs: Mapping[str, int], rng: random.Random
    ) -> float:
        hour = _hour_of_day(t)
        if not self.sunrise_hour <= hour < self.sunset_hour:
            return 0.0
        span = self.sunset_hour - self.sunrise_hour
        value = self.max * math.sin(math.pi * (hour - self.sunrise_hour) / span)
        return max(0.0, value + rng.gauss(0.0, self.noise))


@dataclass
class SoilMoisture:
    """土壌水分（%）：時間とともに下がり、ポンプの出力に比例して上がる。"""

    start: float
    decay_per_hour: float
    refill_per_pump_s: float
    noise: float = 0.0
    pump: str = "pump"

    def __post_init__(self) -> None:
        self._level = self.start

    def sample(
        self, t: datetime, dt_s: float, inputs: Mapping[str, int], rng: random.Random
    ) -> float:
        duty = inputs.get(self.pump, 0) / _PERMILLE
        self._level -= self.decay_per_hour * dt_s / _SECONDS_PER_HOUR
        self._level += self.refill_per_pump_s * dt_s * duty
        self._level = min(_SOIL_MAX, max(_SOIL_MIN, self._level))
        return min(_SOIL_MAX, max(_SOIL_MIN, self._level + rng.gauss(0.0, self.noise)))


def build_signal(spec: Mapping[str, Any]) -> Signal:
    """シナリオの信号定義（scenario.schema.json の signal）から作る。"""
    kind = spec["kind"]
    noise = float(spec.get("noise", 0.0))
    if kind == "constant":
        return Constant(float(spec["value"]), noise)
    if kind == "diurnal":
        return Diurnal(
            float(spec["mean"]), float(spec["amplitude"]), float(spec["peak_hour"]), noise
        )
    if kind == "daylight":
        return Daylight(
            float(spec["max"]), float(spec["sunrise_hour"]), float(spec["sunset_hour"]), noise
        )
    if kind == "soil":
        return SoilMoisture(
            float(spec["start"]),
            float(spec["decay_per_hour"]),
            float(spec["refill_per_pump_s"]),
            noise,
        )
    raise ValueError(f"unknown signal kind: {kind!r}")


class SignalSet:
    """1台のデバイスのチャネル → 信号。チャネルごとに seed から独立した乱数列を持つ。"""

    def __init__(self, signals: Mapping[str, Signal], seed: int, device_id: str) -> None:
        self.signals = dict(signals)
        self._rngs = {ch: random.Random(f"{seed}:{device_id}:{ch}") for ch in self.signals}

    def sample(self, t: datetime, dt_s: float, inputs: Mapping[str, int]) -> dict[str, float]:
        return {
            ch: round(sig.sample(t, dt_s, inputs, self._rngs[ch]), 3)
            for ch, sig in self.signals.items()
        }
