"""シナリオからデバイスを組み立て、Runtime に処理を予定する。

- MQTT につながる代役（mcu-cpp-01、gw-01 簡易版、app-01）と、UART だけの mcu-c-01 を扱う
- mcu-c-01 と gw-01 簡易版が両方あれば UART でつなぐ（一括生成ではメモリ上で、MQTT 実行では pty で）
- 一括生成（run_batch）では、MQTT の電文をデバイス間でメモリ上で配送する（購読トピックの完全一致）
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from iotspec.clock import VirtualClock
from iotspec.constants import DEVICE_MCU_C_01, UART_HEARTBEAT_PERIOD_S
from iotspec.profile import load_profile
from iotspec.sim.devices import MqttDevice, Outgoing
from iotspec.sim.devices.app_01 import App01
from iotspec.sim.devices.gw_01_lite import Gw01Lite
from iotspec.sim.devices.mcu_c_01 import McuC01, Thresholds
from iotspec.sim.devices.mcu_cpp_01 import McuCpp01
from iotspec.sim.runtime import Runtime
from iotspec.sim.scenario import DeviceConfig, Scenario
from iotspec.sim.signals import SignalSet, build_signal

__all__ = ["Simulation", "build_device"]

_MS_PER_S = 1000.0
_MAX_ROUTING_ROUNDS = 100


def _signals(scenario: Scenario, config: DeviceConfig) -> SignalSet:
    return SignalSet(
        {ch: build_signal(spec) for ch, spec in config.signals.items()},
        scenario.seed,
        config.device_id,
    )


def build_device(
    scenario: Scenario, config: DeviceConfig, clock: VirtualClock
) -> McuCpp01 | McuC01 | Gw01Lite | App01:
    faults = scenario.faults_for(config.device_id)
    if config.type == "mcu_cpp_01":
        return McuCpp01(
            config.profile,
            clock,
            _signals(scenario, config),
            site=scenario.site,
            faults=faults,
            local_rules=config.local_rules,
            initial_mode=config.initial_mode,
        )
    if config.type == "mcu_c_01":
        return McuC01(
            config.profile,
            clock,
            _signals(scenario, config),
            faults=faults,
            local_rules=config.local_rules,
            thresholds=Thresholds(**config.options.get("thresholds", {})),
            reset_reason=config.options.get("reset_reason", "power_on"),
            acks_at=[
                scenario.start + timedelta(seconds=s) for s in config.options.get("acks_at_s", [])
            ],
        )
    if config.type == "gw_01_lite":
        uart_profile = load_profile(DEVICE_MCU_C_01)
        return Gw01Lite(
            config.profile,
            clock,
            site=scenario.site,
            uart_profile=uart_profile,
            request_rules=config.options.get("request_rules", []),
            faults=faults,
            initial_mode=config.initial_mode,
            emergency_condition_until=(
                None
                if "emergency_condition_until_s" not in config.options
                else scenario.start
                + timedelta(seconds=float(config.options["emergency_condition_until_s"]))
            ),
        )
    if config.type == "app_01":
        return App01(config.profile, clock, site=scenario.site)
    raise ValueError(f"unknown device type: {config.type!r}")


class Simulation:
    """シナリオ1本分のデバイスと Runtime。speed を渡すとシナリオの speed より優先する。"""

    def __init__(self, scenario: Scenario, speed: float | None = None) -> None:
        self.scenario = scenario
        self.clock = VirtualClock(scenario.start)
        self.runtime = Runtime(self.clock, scenario.speed if speed is None else speed)
        self.configs = {c.device_id: c for c in scenario.devices}
        self.devices = {
            c.device_id: build_device(scenario, c, self.clock) for c in scenario.devices
        }
        self._scheduled = False

    # ------------------------------------------------------------ 取り出し

    @property
    def mqtt_devices(self) -> list[MqttDevice]:
        return [d for d in self.devices.values() if not isinstance(d, McuC01)]

    @property
    def mcu_c(self) -> McuC01 | None:
        return next((d for d in self.devices.values() if isinstance(d, McuC01)), None)

    @property
    def gw(self) -> Gw01Lite | None:
        return next((d for d in self.devices.values() if isinstance(d, Gw01Lite)), None)

    @property
    def app(self) -> App01 | None:
        return next((d for d in self.devices.values() if isinstance(d, App01)), None)

    @property
    def uart_linked(self) -> bool:
        """mcu-c-01 と gw-01 簡易版が同じシナリオにいる（UART でつなぐ）。"""
        return self.mcu_c is not None and self.gw is not None

    # ------------------------------------------------------------ 予定

    def schedule(self) -> None:
        """各デバイスの起動・周期処理・シナリオの操作を予定する（1回だけ）。"""
        if self._scheduled:
            return
        self._scheduled = True
        start = self.scenario.start
        for device_id, device in self.devices.items():
            if isinstance(device, McuCpp01):
                period_s = min(c.period_ms for c in device.profile.channels) / _MS_PER_S
            else:
                period_s = float(UART_HEARTBEAT_PERIOD_S)
            self.runtime.at(start, device.start)
            self.runtime.every(period_s, device.tick, first=start + timedelta(seconds=period_s))
            options = self.configs[device_id].options
            if isinstance(device, Gw01Lite):
                for item in options.get("uart_commands", []):
                    self.runtime.at(start + timedelta(seconds=item["at_s"]), _submit(device, item))
            if isinstance(device, App01):
                for item in options.get("requests", []):
                    self.runtime.at(start + timedelta(seconds=item["at_s"]), _request(device, item))

    # ------------------------------------------------------------ 一括生成

    def pump_uart(self) -> None:
        """メモリ上で UART のバイト列を受け渡す（一括生成用）。"""
        mcu, gw = self.mcu_c, self.gw
        if mcu is None or gw is None:
            return
        for _ in range(_MAX_ROUTING_ROUNDS):
            up, down = mcu.drain_tx(), gw.drain_uart()
            if not up and not down:
                return
            if up:
                gw.feed_uart(up)
            if down:
                mcu.feed(down)

    def route(self, published: list[Outgoing]) -> None:
        """メモリ上で MQTT の電文を購読者に配送し、配送した電文を published に足す。"""
        for _ in range(_MAX_ROUTING_ROUNDS):
            batch = [o for d in self.mqtt_devices for o in d.drain()]
            if not batch:
                return
            published.extend(batch)
            for out in batch:
                for device in self.mqtt_devices:
                    if out.topic in device.subscriptions():
                        device.handle_message(out.topic, out.payload())
            self.pump_uart()

    def run_batch(self) -> list[Outgoing]:
        """待たずに最後まで流し、発行したメッセージを順に返す（過去データの一括生成）。"""
        self.schedule()
        out: list[Outgoing] = []

        def step() -> None:
            self.pump_uart()
            self.route(out)

        self.runtime.run_until(self.scenario.end, after_step=step)
        return out


def _submit(gw: Gw01Lite, item: dict[str, Any]) -> Any:
    payload = {k: v for k, v in item.items() if k in ("op", "args", "lease_s", "priority")}
    return lambda: gw.submit_uart(payload, repeat=int(item.get("repeat", 0)))


def _request(app: App01, item: dict[str, Any]) -> Any:
    return lambda: app.request(item)
