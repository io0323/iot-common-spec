"""Phase 1：Schema とサンプル電文のテスト。

- samples/valid の全件が適合し、samples/invalid の全件が「期待した理由で」不適合になる
- 理由は (JSON Pointer, jsonschema のキーワード) で照合する
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from iotspec.constants import SCHEMA_NAMES
from iotspec.schema import SCHEMA_DIR, iter_errors, json_pointer, select_schema, validate

ROOT = Path(__file__).resolve().parents[2]
VALID_DIR = ROOT / "samples" / "valid"
INVALID_DIR = ROOT / "samples" / "invalid"
VALIDATE_CLI = ROOT / "tools" / "validate.py"

VALID = sorted(VALID_DIR.glob("*.json"))
INVALID = sorted(INVALID_DIR.glob("*.json"))

# 不適合の期待理由：ファイル名（拡張子なし）→ (JSON Pointer, キーワード)
EXPECTED: dict[str, tuple[str, str]] = {
    "envelope__bad_ulid": ("/msg_id", "pattern"),
    "envelope__missing_boot_id": ("", "required"),
    "envelope__bad_boot_id": ("/boot_id", "pattern"),
    "envelope__ts_not_utc": ("/ts", "pattern"),
    "envelope__ts_without_millis": ("/ts", "pattern"),
    "envelope__seq_over_uint32": ("/seq", "maximum"),
    "envelope__seq_is_string": ("/seq", "type"),
    "envelope__bad_ts_source": ("/ts_source", "enum"),
    "envelope__bad_device_id": ("/device_id", "pattern"),
    "envelope__unknown_schema": ("/schema", "unknown_schema"),
    "envelope__missing_payload": ("", "required"),
    "telemetry__unknown_unit": ("/payload/readings/0/unit", "enum"),
    "telemetry__bad_quality": ("/payload/readings/1/quality", "enum"),
    "telemetry__unknown_owner": ("/payload/actuators/0/owner", "enum"),
    "telemetry__value_is_string": ("/payload/readings/0/value", "type"),
    "telemetry__count_negative": ("/payload/readings/0/value", "minimum"),
    "telemetry__count_fractional": ("/payload/readings/0/value", "type"),
    "telemetry__missing_readings": ("/payload", "required"),
    "telemetry__actuator_level_out_of_range": ("/payload/actuators/1/level", "maximum"),
    "event__lowercase_code": ("/payload/code", "pattern"),
    "event__bad_severity": ("/payload/severity", "enum"),
    "event__missing_code": ("/payload", "required"),
    "event__detail_not_object": ("/payload/detail", "type"),
    "event__boot_unknown_reset_reason": ("/payload/detail/reset_reason", "enum"),
    "event__boot_reset_reason_is_number": ("/payload/detail/reset_reason", "enum"),
    "state__bad_status": ("/payload/status", "enum"),
    "state__bad_bridged_by": ("/payload/bridged_by", "pattern"),
    "state__missing_channels": ("/payload", "required"),
    "state__missing_actuators": ("/payload", "required"),
    "state__channel_unknown_unit": ("/payload/channels/0/unit", "enum"),
    "state__period_ms_is_string": ("/payload/channels/0/period_ms", "type"),
    "cmd__level_out_of_range": ("/payload/args/level", "maximum"),
    "cmd__level_negative": ("/payload/args/level", "minimum"),
    "cmd__lease_s_zero": ("/payload/lease_s", "minimum"),
    "cmd__lease_s_over_max": ("/payload/lease_s", "maximum"),
    "cmd__unknown_op": ("/payload/op", "enum"),
    "cmd__set_actuator_missing_id": ("/payload/args", "required"),
    "cmd__set_actuator_missing_args": ("/payload", "required"),
    "cmd__bad_priority": ("/payload/priority", "enum"),
    "cmd__release_missing_id": ("/payload/args", "required"),
    "cmd__set_mode_missing_mode": ("/payload/args", "required"),
    "cmd__bad_expires_at": ("/payload/expires_at", "pattern"),
    "cmd_ack__bad_status": ("/payload/status", "enum"),
    "cmd_ack__unknown_reason": ("/payload/reason", "anyOf"),
    "cmd_ack__rejected_without_reason": ("/payload/reason", "enum"),
    "cmd_ack__limited_not_boolean": ("/payload/limited", "type"),
    "cmd_ack__missing_cmd_id": ("/payload", "required"),
    "request__schedule_kind_without_schedule": ("/payload", "required"),
    "request__bad_kind": ("/payload/kind", "enum"),
    "request__missing_target": ("/payload", "required"),
    "request__level_out_of_range": ("/payload/args/level", "maximum"),
    "request__lease_s_over_max": ("/payload/lease_s", "maximum"),
    "request_ack__accepted_without_cmd_id": ("/payload", "required"),
    "request_ack__unknown_reason": ("/payload/reason", "anyOf"),
    "request_ack__rejected_without_reason": ("/payload/reason", "not"),
    "request_ack__bad_status": ("/payload/status", "enum"),
}

KIND_FILE_PREFIXES = [name.removeprefix("iot.v1.") for name in SCHEMA_NAMES.values()]


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _reasons(message: object) -> set[tuple[str, str]]:
    if select_schema(message) is None:
        return {("/schema", "unknown_schema")}
    return {(json_pointer(e), str(e.validator)) for e in iter_errors(message)}


@pytest.mark.parametrize("path", sorted(SCHEMA_DIR.glob("*.schema.json")), ids=lambda p: p.name)
def test_schema_files_are_valid_draft_2020_12(path: Path) -> None:
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_samples_conform(path: Path) -> None:
    assert validate(_load(path)) == []


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_samples_fail_for_expected_reason(path: Path) -> None:
    message = _load(path)
    assert validate(message) != []
    assert path.stem in EXPECTED, f"期待理由が未登録: {path.stem}"
    assert EXPECTED[path.stem] in _reasons(message)


def test_expected_table_matches_files() -> None:
    assert set(EXPECTED) == {p.stem for p in INVALID}


@pytest.mark.parametrize("kind", KIND_FILE_PREFIXES)
def test_at_least_three_samples_per_kind(kind: str) -> None:
    assert len([p for p in VALID if p.stem.split("__")[0] == kind]) >= 3
    assert len([p for p in INVALID if p.stem.split("__")[0] == kind]) >= 3


@pytest.mark.parametrize("kind", ["cmd", "cmd_ack", "request", "request_ack"])
def test_spec_examples_present(kind: str) -> None:
    """spec.md 第3章のコード例が samples/valid にあり、schema フィールドが kind と一致する。"""
    message = _load(VALID_DIR / f"{kind}__spec_example.json")
    assert isinstance(message, dict)
    assert message["schema"] == f"iot.v1.{kind}"


def test_every_valid_sample_matches_its_file_prefix() -> None:
    for path in VALID:
        message = _load(path)
        assert isinstance(message, dict)
        assert message["schema"] == "iot.v1." + path.stem.split("__")[0], path.name


def test_unknown_fields_are_allowed() -> None:
    message = _load(VALID_DIR / "telemetry__with_unknown_field.json")
    assert isinstance(message, dict)
    message["another_new_field"] = [1, 2, 3]
    assert validate(message) == []


def test_validate_reports_pointer_and_reason() -> None:
    message = _load(INVALID_DIR / "cmd__level_out_of_range.json")
    errors = validate(message)
    assert any(e.startswith("/payload/args/level: ") for e in errors)


def test_validate_non_object() -> None:
    assert validate([1, 2]) != []
    assert validate({"payload": {}})[0].startswith("/schema: ")


def _run_cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATE_CLI), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exit_codes() -> None:
    ok = _run_cli(str(VALID_DIR / "cmd__spec_example.json"))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    ng = _run_cli(str(INVALID_DIR / "cmd__level_out_of_range.json"))
    assert ng.returncode == 1
    assert "/payload/args/level" in ng.stdout
    unknown = _run_cli(str(INVALID_DIR / "envelope__unknown_schema.json"))
    assert unknown.returncode == 2


def test_cli_stdin() -> None:
    text = (VALID_DIR / "request_ack__spec_example.json").read_text(encoding="utf-8")
    assert _run_cli(stdin=text).returncode == 0
    assert _run_cli("-", stdin="{not json").returncode == 1
