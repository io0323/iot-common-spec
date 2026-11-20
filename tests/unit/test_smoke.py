"""Phase 0 のスモークテスト：パッケージが読み込め、定数が spec.md 第2章と一致する。"""

from iotspec import SPEC_VERSION, constants


def test_spec_version() -> None:
    assert SPEC_VERSION == "0.2"


def test_kinds_and_schema_names() -> None:
    assert constants.KINDS == (
        "telemetry",
        "event",
        "state",
        "cmd",
        "cmd/ack",
        "request",
        "request/ack",
    )
    assert constants.SCHEMA_NAMES["cmd/ack"] == "iot.v1.cmd_ack"
    assert constants.SCHEMA_NAMES["telemetry"] == "iot.v1.telemetry"


def test_topic() -> None:
    topic = constants.TOPIC_FORMAT.format(
        site=constants.DEFAULT_SITE, device_id=constants.DEVICE_MCU_CPP_01, kind="cmd/ack"
    )
    assert topic == "iot/v1/home/mcu-cpp-01/cmd/ack"
    assert constants.RESERVED_DEVICE_IDS == ("mcu-c-01", "mcu-cpp-01", "gw-01", "app-01")
