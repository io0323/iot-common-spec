"""共通仕様 v0.2 の定数（spec.md から集めたもの）。

数値・コード値・上限値はこのモジュールに集め、ほかの場所に直接書かない。
Phase 0 ではトピック体系（第1章・第2章）と予約済み device_id だけを定義する。
"""

from __future__ import annotations

from typing import Final

# --- 版（第1章「版の方針」） ---
TOPIC_ROOT: Final = "iot"
TOPIC_MAJOR_VERSION: Final = "v1"
"""トピックに含めるメジャー版。本書の版（v0.2）とは別物。"""

# --- トピック（第2章） ---
TOPIC_FORMAT: Final = "iot/v1/{site}/{device_id}/{kind}"
"""MQTT トピックの形式。{device_id} は発信元または宛先のデバイス。"""

KIND_TELEMETRY: Final = "telemetry"
KIND_EVENT: Final = "event"
KIND_STATE: Final = "state"
KIND_CMD: Final = "cmd"
KIND_CMD_ACK: Final = "cmd/ack"
KIND_REQUEST: Final = "request"
KIND_REQUEST_ACK: Final = "request/ack"

KINDS: Final[tuple[str, ...]] = (
    KIND_TELEMETRY,
    KIND_EVENT,
    KIND_STATE,
    KIND_CMD,
    KIND_CMD_ACK,
    KIND_REQUEST,
    KIND_REQUEST_ACK,
)
"""トピックの {kind} に入る値（第2章のトピック表の順）。"""

SCHEMA_NAME_FORMAT: Final = "iot.v1.{kind}"
"""エンベロープの schema フィールドの形式（第3章）。kind の "/" は "_" に置き換える。"""

SCHEMA_NAMES: Final[dict[str, str]] = {
    kind: SCHEMA_NAME_FORMAT.format(kind=kind.replace("/", "_")) for kind in KINDS
}
"""トピックの kind → schema フィールドの値（例：cmd/ack → iot.v1.cmd_ack）。"""

DEFAULT_SITE: Final = "home"
"""site 名の既定値（第9章「確定前に決めること」2026-11-20 決定）。"""

# --- 予約済み device_id（第2章「命名規則」） ---
DEVICE_MCU_C_01: Final = "mcu-c-01"
"""作品01（C × STM32）。"""
DEVICE_MCU_CPP_01: Final = "mcu-cpp-01"
"""作品02（C++ × ESP32）。"""
DEVICE_GW_01: Final = "gw-01"
"""作品03（C++ × Raspberry Pi、ゲートウェイ）。"""
DEVICE_APP_01: Final = "app-01"
"""作品04（Python × Raspberry Pi、アプリ）。"""

RESERVED_DEVICE_IDS: Final[tuple[str, ...]] = (
    DEVICE_MCU_C_01,
    DEVICE_MCU_CPP_01,
    DEVICE_GW_01,
    DEVICE_APP_01,
)
