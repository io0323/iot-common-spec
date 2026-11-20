"""Phase 6：契約テストキットの自己テスト。

- 代役同士：各 role の代役を対象にすると、全チェックが合格する
- 壊した代役：わざと仕様に反する代役では、該当するチェックが不合格になる
前提は conftest.py を参照（ブローカーに接続できなければ skip）。
"""

from __future__ import annotations

from typing import Any

import pytest

from iotspec.clock import SystemClock
from iotspec.contract.core import CheckResult, format_results
from iotspec.contract.runner import run_contract, standin_target
from iotspec.ids import new_ulid
from iotspec.sim.devices.app_01 import App01
from iotspec.sim.devices.gw_01_lite import CmdRecord, Gw01Lite
from iotspec.sim.devices.mcu_cpp_01 import McuCpp01
from iotspec.sim.mqtt import BrokerAddress
from iotspec.sim.simulation import Simulation

pytestmark = pytest.mark.integration


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


@pytest.mark.parametrize("role", ["mcu-cpp-01", "mcu-c-01", "gw-01", "app-01"])
async def test_self_test_all_checks_pass(broker: BrokerAddress, role: str) -> None:
    results = await run_contract(role, broker, standin_target(role))
    print(format_results(results))
    assert results
    assert [r.name for r in results if r.status != "passed"] == []


# ---------------------------------------------------------------- 壊した代役


def _device(sim: Simulation, device_id: str) -> Any:
    return sim.devices[device_id]


async def test_broken_mcu_cpp_reexecutes_duplicate_cmd_id(broker: BrokerAddress) -> None:
    def tweak(sim: Simulation) -> None:
        device: McuCpp01 = _device(sim, "mcu-cpp-01")
        device._dedup.get = lambda cmd_id, now: None  # type: ignore[method-assign]  # 重複を覚えない

    results = _by_name(
        await run_contract(
            "mcu-cpp-01", broker, standin_target("mcu-cpp-01", tweak), only=["duplicate_cmd_id"]
        )
    )
    print(format_results(list(results.values())))
    assert results["duplicate_cmd_id"].status == "failed"
    assert any("実行し直した" in f.message for f in results["duplicate_cmd_id"].failures)


class _AlwaysAllow:
    def try_acquire(self) -> bool:
        return True


class _NoLimits(dict[str, Any]):
    def get(self, key: str, default: Any = None) -> Any:
        return _AlwaysAllow()


async def test_broken_mcu_cpp_exceeds_publish_rate(broker: BrokerAddress) -> None:
    def tweak(sim: Simulation) -> None:
        device: McuCpp01 = _device(sim, "mcu-cpp-01")
        device._limiters = _NoLimits()  # type: ignore[assignment]  # 送信上限を守らない
        original = device.tick

        def flood() -> None:
            original()
            for _ in range(80):
                device._event("THRESHOLD_EXCEEDED", "warning", channel="temp_air")

        device.tick = flood  # type: ignore[method-assign]

    results = _by_name(
        await run_contract(
            "mcu-cpp-01", broker, standin_target("mcu-cpp-01", tweak), only=["ack", "publish_rate"]
        )
    )
    print(format_results(list(results.values())))
    assert results["publish_rate"].status == "failed"
    assert any("上限を超えて" in f.message for f in results["publish_rate"].failures)


async def test_broken_gw_changes_cmd_ref_on_retransmission(broker: BrokerAddress) -> None:
    def tweak(sim: Simulation) -> None:
        gw: Gw01Lite = _device(sim, "gw-01")
        original = gw._transmit

        def transmit(record: CmdRecord, now: Any, *, count_attempt: bool = True) -> bool:
            if count_attempt and record.attempts >= 1 and record.cmd_ref is not None:
                gw._in_flight.pop(record.cmd_ref, None)  # 再送のたびに新しい cmd_ref を振ってしまう
                record.cmd_ref = gw._next_cmd_ref
                gw._next_cmd_ref += 1
                gw._in_flight[record.cmd_ref] = record
            return original(record, now, count_attempt=count_attempt)

        gw._transmit = transmit  # type: ignore[method-assign]

    results = _by_name(
        await run_contract(
            "gw-01", broker, standin_target("gw-01", tweak), only=["uart_cmd_limits"]
        )
    )
    print(format_results(list(results.values())))
    assert results["uart_cmd_limits"].status == "failed"
    assert any("cmd_ref が変わった" in f.message for f in results["uart_cmd_limits"].failures)


async def test_broken_app_makes_random_schedule_request_id(
    broker: BrokerAddress, monkeypatch: pytest.MonkeyPatch
) -> None:
    import iotspec.sim.devices.app_01 as app_module

    # スケジュール実行のたびに違う request_id を作ってしまう
    monkeypatch.setattr(
        app_module, "schedule_request_id", lambda schedule_id, planned_at: new_ulid(SystemClock())
    )

    def tweak(sim: Simulation) -> None:
        assert isinstance(_device(sim, "app-01"), App01)

    results = _by_name(
        await run_contract(
            "app-01",
            broker,
            standin_target("app-01", tweak),
            only=["schedule_request_id"],
            options={"duration_s": 8},
        )
    )
    print(format_results(list(results.values())))
    assert results["schedule_request_id"].status == "failed"
    assert any("決定的な request_id" in f.message for f in results["schedule_request_id"].failures)
