"""契約テストの実行：環境を用意し、対象を起動し、role のチェックを行う。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from iotspec.clock import format_ts
from iotspec.constants import DEFAULT_SITE
from iotspec.contract.context import ContractContext, SimulationTarget, Target
from iotspec.contract.core import ROLES, CheckResult, run_checks
from iotspec.sim.mqtt import BrokerAddress
from iotspec.sim.scenario import parse_scenario
from iotspec.sim.simulation import Simulation

__all__ = ["UART_ROLES", "run_contract", "standin_target"]

UART_ROLES = frozenset({"mcu-c-01", "gw-01"})
STANDIN_DURATION_S = 3600


async def run_contract(
    role: str,
    broker: BrokerAddress,
    target: Target,
    *,
    device_id: str | None = None,
    uart_path: str | None = None,
    only: Iterable[str] | None = None,
    site: str = DEFAULT_SITE,
    options: dict[str, Any] | None = None,
) -> list[CheckResult]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}（{', '.join(ROLES)}）")
    ctx = ContractContext(
        role=role,
        broker=broker,
        target=target,
        device_id=device_id or role,
        site=site,
        uart_path=uart_path,
        options=dict(options or {}),
    )
    try:
        await ctx.open(uart=role in UART_ROLES)
        await target.start(ctx)
        return await run_checks(ctx, role, only)
    finally:
        await target.stop()
        await ctx.close()


# ---------------------------------------------------------------- 代役を対象にする（自己テスト用）

_SIGNALS_CPP = {
    "temp_air": {"kind": "constant", "value": 25.0, "noise": 0.1},
    "humidity": {"kind": "constant", "value": 50.0},
    "pressure": {"kind": "constant", "value": 1012.0},
    "lux": {"kind": "constant", "value": 8000.0},
    "soil": {"kind": "soil", "start": 40.0, "decay_per_hour": 1.0, "refill_per_pump_s": 0.2},
}


def _device(role: str, ctx: ContractContext) -> dict[str, Any]:
    if role == "mcu-cpp-01":
        return {
            "device_id": role,
            "type": "mcu_cpp_01",
            "local_rules": {"fan_main": 200},
            "signals": _SIGNALS_CPP,
        }
    if role == "mcu-c-01":
        return {
            "device_id": role,
            "type": "mcu_c_01",
            "uart_path": ctx.uart_peer_path,
            "signals": {"temp_board": {"kind": "constant", "value": 25.0}},
        }
    if role == "gw-01":
        return {"device_id": role, "type": "gw_01_lite", "uart_path": ctx.uart_peer_path}
    return {
        "device_id": role,
        "type": "app_01",
        "requests": [
            {
                "at_s": 1,
                "kind": "manual",
                "target": "mcu-cpp-01",
                "op": "set_actuator",
                "args": {"id": "fan_main", "level": 100},
            },
            {
                "at_s": 3,
                "kind": "schedule",
                "schedule_id": 7,
                "target": "mcu-cpp-01",
                "op": "set_actuator",
                "args": {"id": "led_grow", "level": 300},
                "lease_s": 600,
            },
            {"at_s": 5, "kind": "manual", "target": "mcu-cpp-01", "op": "get_status"},
        ],
    }


def standin_target(
    role: str, tweak: Callable[[Simulation], None] | None = None
) -> SimulationTarget:
    """role の代役（このリポジトリのシミュレータ）を対象にする。

    tweak で代役を壊せる（自己テスト用）。
    """

    def make(ctx: ContractContext) -> Simulation:
        scenario = parse_scenario(
            {
                "name": "contract_standin",
                "start": format_ts(datetime.now(UTC)),
                "speed": 1,
                "duration_s": STANDIN_DURATION_S,
                "site": ctx.site,
                "devices": [_device(role, ctx)],
            }
        )
        simulation = Simulation(scenario)
        if tweak is not None:
            tweak(simulation)
        return simulation

    return SimulationTarget(make)
