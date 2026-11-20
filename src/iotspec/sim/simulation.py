"""シナリオからデバイスを組み立て、Runtime に処理を予定する。"""

from __future__ import annotations

from datetime import timedelta

from iotspec.clock import VirtualClock
from iotspec.sim.devices import Outgoing
from iotspec.sim.devices.mcu_cpp_01 import McuCpp01
from iotspec.sim.runtime import Runtime
from iotspec.sim.scenario import DeviceConfig, Scenario
from iotspec.sim.signals import SignalSet, build_signal

__all__ = ["Simulation", "build_device"]

_MS_PER_S = 1000.0


def build_device(scenario: Scenario, config: DeviceConfig, clock: VirtualClock) -> McuCpp01:
    if config.type != "mcu_cpp_01":
        raise ValueError(f"unknown device type: {config.type!r}")
    signals = SignalSet(
        {ch: build_signal(spec) for ch, spec in config.signals.items()},
        scenario.seed,
        config.device_id,
    )
    return McuCpp01(
        config.profile,
        clock,
        signals,
        site=scenario.site,
        faults=scenario.faults_for(config.device_id),
        local_rules=config.local_rules,
        initial_mode=config.initial_mode,
    )


class Simulation:
    """シナリオ1本分のデバイスと Runtime。speed を渡すとシナリオの speed より優先する。"""

    def __init__(self, scenario: Scenario, speed: float | None = None) -> None:
        self.scenario = scenario
        self.clock = VirtualClock(scenario.start)
        self.runtime = Runtime(self.clock, scenario.speed if speed is None else speed)
        self.devices = [build_device(scenario, c, self.clock) for c in scenario.devices]
        self._scheduled = False

    def schedule(self) -> None:
        """各デバイスの起動と計測周期の処理を予定する（1回だけ）。"""
        if self._scheduled:
            return
        self._scheduled = True
        for device in self.devices:
            period_s = min(c.period_ms for c in device.profile.channels) / _MS_PER_S
            self.runtime.at(self.scenario.start, device.start)
            first = self.scenario.start + timedelta(seconds=period_s)
            self.runtime.every(period_s, device.tick, first=first)

    def drain(self) -> list[Outgoing]:
        out: list[Outgoing] = []
        for device in self.devices:
            out.extend(device.drain())
        return out

    def run_batch(self) -> list[Outgoing]:
        """待たずに最後まで流し、発行したメッセージを順に返す（過去データの一括生成）。"""
        self.schedule()
        out: list[Outgoing] = []
        self.runtime.run_until(self.scenario.end, after_step=lambda: out.extend(self.drain()))
        return out
