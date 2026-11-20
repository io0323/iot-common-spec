"""Phase 6：契約テストキットの骨組み（ブローカー不要の部分）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from iotspec.contract.core import (
    ROLES,
    Checker,
    CheckResult,
    Failure,
    checks_for,
    format_results,
    run_checks,
    write_junit,
)
from iotspec.contract.roles.gw_01 import goldens


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_checks(role: str) -> None:
    names = [item.name for item in checks_for(role)]
    assert len(names) == len(set(names))
    assert len(names) >= 4


def test_required_checks_exist() -> None:
    required = {
        "mcu-c-01": {
            "ack",
            "duplicate_cmd_ref",
            "upstream_lost_restored",
            "boot_order",
            "seq_per_type",
            "frame_encoding",
        },
        "mcu-cpp-01": {
            "schema",
            "ack",
            "lease_expiry",
            "duplicate_cmd_id",
            "limited_applied",
            "lwt",
            "set_param_unknown_op",
            "publish_rate",
        },
        "gw-01": {
            "golden_conversion",
            "uart_cmd_limits",
            "request_ack",
            "bridged_offline_state",
            "schema",
            "publish_rate",
        },
        "app-01": {
            "request_schema",
            "retransmission",
            "schedule_request_id",
            "ack_tracking",
            "schema",
            "publish_rate",
        },
    }
    for role, names in required.items():
        assert names <= {item.name for item in checks_for(role)}, role


def test_mcu_c_01_has_no_mqtt_common_checks() -> None:
    assert "publish_rate" not in {item.name for item in checks_for("mcu-c-01")}


def test_failure_render_and_checker() -> None:
    checker = Checker()
    assert checker.expect(True, "ok")
    assert not checker.expect(
        False, "違う", ref="iot/v1/home/x/cmd/ack seq=3", expected=1, actual=2
    )
    (failure,) = checker.failures
    assert failure.render() == "違う / 電文: iot/v1/home/x/cmd/ack seq=3 / 期待: 1 / 実際: 2"


def test_format_and_junit(tmp_path: Path) -> None:
    results = [
        CheckResult("gw-01", "a", "説明a", "passed", duration_s=0.5),
        CheckResult("gw-01", "b", "説明b", "failed", [Failure("食い違い", "ref", "x", "y")]),
        CheckResult("gw-01", "c", "説明c", "skipped", reason="対象を起動できない"),
    ]
    text = format_results(results)
    assert "[不合格] gw-01 / b" in text and "食い違い / 電文: ref / 期待: x / 実際: y" in text
    path = tmp_path / "out.xml"
    write_junit(results, path, "gw-01")
    suite = ET.parse(path).getroot()
    assert (
        suite.get("tests") == "3" and suite.get("failures") == "1" and suite.get("skipped") == "1"
    )
    cases = {case.get("name"): case for case in suite}
    assert cases["b"].find("failure") is not None
    assert cases["c"].find("skipped") is not None


class _Target:
    controllable = False


class _Ctx:
    target = _Target()


async def test_checks_needing_target_control_are_skipped() -> None:
    results = await run_checks(_Ctx(), "mcu-cpp-01", only=["lwt"])  # type: ignore[arg-type]
    (lwt,) = results
    assert lwt.status == "skipped" and "--target-cmd" in lwt.reason


def test_goldens_are_available_for_gw_contract() -> None:
    names = {g["name"] for g in goldens()}
    assert {"state_boot", "event_upstream_restored", "telemetry_seq_wrapped"} <= names
