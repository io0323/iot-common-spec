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

# --- 数値の範囲（第3章） ---
UINT32_MAX: Final = 0xFFFF_FFFF
"""seq・uptime_ms の上限（uint32）。seq はこれを超えたら0に戻る。"""

BOOT_ID_BITS: Final = 32
"""boot_id のビット数。16進小文字8桁で表す（第3章、v0.2.1 で確定）。"""

# --- 区間ごとの約束：待ち時間・再送・重複排除（第4章「区間ごとの約束」） ---
# 04 → 03（request）
REQUEST_RESPONSE_TIMEOUT_S: Final = 5
"""最初の応答（request/ack）までの待ち時間。"""
REQUEST_MAX_RETRIES: Final = 3
"""同じ request_id での再送の最大回数（初回を含まない）。"""
REQUEST_DEDUP_TTL_S: Final = 3600
"""03 が request_id を記憶する期間（1時間）。"""
REQUEST_FINAL_RESULT_TIMEOUT_S: Final = 60
"""04 は受理から60秒以内に最終結果が来なければ failed（gateway_timeout）とする。"""

# 03 → 02（cmd）
CMD_ACK_TIMEOUT_S: Final = 2
"""cmd/ack までの待ち時間。"""
CMD_MAX_RETRIES: Final = 3
"""同じ cmd_id での再送の最大回数（初回を含まない）。"""
CMD_DEDUP_CAPACITY: Final = 64
"""02 が記憶する cmd_id と結果の件数。"""
CMD_DEDUP_TTL_S: Final = 600
"""02 が cmd_id と結果を記憶する期間（10分）。"""

# 03 → 01（UART CMD）
UART_ACK_TIMEOUT_S: Final = 2
"""UART の ACK までの待ち時間。"""
UART_CMD_MAX_RETRIES: Final = 3
"""同じ cmd_ref での再送の最大回数（初回を含まない）。"""
UART_CMD_REF_DEDUP_CAPACITY: Final = 16
"""01 が記憶する cmd_ref と結果の件数。"""
UART_CMD_RATE_PER_S: Final = 1
"""03 から 01 への指令は毎秒1件まで。"""
UART_CMD_MAX_IN_FLIGHT: Final = 4
"""03 から 01 への未完了の指令は同時4件まで。"""
CMD_REF_MODULO: Final = 65_536
"""cmd_ref（u16）は65,536で一周する。"""

# --- 重複排除と送信量の上限（第4章「送信量の上限」、第5章「重複」） ---
SEQ_DEDUP_CAPACITY: Final = 1024
"""受信側が発信元・kind ごとに記憶する直近の seq の数。"""
PUBLISH_RATE_AVG_PER_S: Final = 10
"""発信元・kind ごとの MQTT 発行の平均上限（毎秒）。"""
PUBLISH_RATE_BURST_PER_S: Final = 50
"""発信元・kind ごとの MQTT 発行の瞬間的な上限（毎秒）。"""
PUBLISH_RATE_TOTAL_PER_S: Final = 200
"""作品03・04 の非機能要件にある全発信元の合計（毎秒）。"""
MQTT_MAX_IN_FLIGHT: Final = 32
"""MQTT クライアントの確認待ち（QoS 1 の未確認送信）の上限。"""

# --- リース（第4章「リース」） ---
LEASE_DEFAULT_S: Final = 60
"""lease_s の既定値。cmd で省略されたらこの値とみなす（v0.2.1 で確定）。"""
LEASE_MIN_S: Final = 1
"""lease_s の下限（Schema の minimum）。"""
LEASE_MAX_S: Final = 600
"""lease_s の上限。"""
LEASE_RENEW_DIVISOR: Final = 3
"""03 は lease_s の3分の1ごとに新しい cmd_id で送り直す（60秒なら20秒ごと）。"""

# --- データ品質（第5章） ---
STALE_PERIOD_MULTIPLIER: Final = 3
"""最終更新から period_ms の3倍を超えたら stale。"""
CLOCK_SKEW_THRESHOLD_S: Final = 5 * 60
"""ts_source: device で受信時刻との差が±5分を超えたら時刻ずれ。"""
CLOCK_SKEW_EVENT_INTERVAL_S: Final = 60 * 60
"""CLOCK_SKEW イベントは同一デバイス1時間に1回まで。"""
LATE_PERIOD_MULTIPLIER: Final = 3
"""ts が受信時刻より過去で、差が周期の3倍を超えたら遅延到着。"""
LATE_MAX_AGE_S: Final = 5 * 60
"""遅延到着とみなす差の上限（5分以内）。これを超える差は時刻ずれの扱い。"""

# --- UART フレーム（第7章） ---
UART_BAUD_RATE: Final = 115_200
"""物理層：115200 bps、8N1、フロー制御なし。"""
UART_FRAME_DELIMITER: Final = 0x00
"""COBS で符号化したフレームの区切り。"""
UART_HEADER_SIZE: Final = 4
"""[種別 u8][seq u16][長さ u8]。"""
UART_CRC_SIZE: Final = 2
"""CRC-16/CCITT-FALSE（u16、リトルエンディアン）。"""
UART_PAYLOAD_MAX: Final = 200
"""ペイロード長の上限（バイト）。"""
UART_SEQ_MODULO: Final = 65_536
"""UART の seq（u16）は65,536で一周する。共通エンベロープでは一周ごとにこれを加える。"""

CRC16_POLY: Final = 0x1021
CRC16_INIT: Final = 0xFFFF
CRC16_XOROUT: Final = 0x0000

# 種別（第7章「種別と対応」）
UART_TYPE_TELEMETRY: Final = 0x01
UART_TYPE_EVENT: Final = 0x02
UART_TYPE_HEARTBEAT: Final = 0x03
UART_TYPE_STATE: Final = 0x04
UART_TYPE_CMD: Final = 0x10
UART_TYPE_ACK: Final = 0x11

# CMD の op（第7章）
UART_OP_SET_FAN_OVERRIDE: Final = 0x01
UART_OP_CLEAR_OVERRIDE: Final = 0x02
UART_OP_SET_THRESHOLD: Final = 0x03
UART_OP_GET_STATUS: Final = 0x10
UART_OP_GET_DIAG: Final = 0x11
UART_OP_NAMES: Final[dict[int, str]] = {
    UART_OP_SET_FAN_OVERRIDE: "SET_FAN_OVERRIDE",
    UART_OP_CLEAR_OVERRIDE: "CLEAR_OVERRIDE",
    UART_OP_SET_THRESHOLD: "SET_THRESHOLD",
    UART_OP_GET_STATUS: "GET_STATUS",
    UART_OP_GET_DIAG: "GET_DIAG",
}

# 生存監視（第7章）
UART_HEARTBEAT_PERIOD_S: Final = 1
"""HEARTBEAT は双方向1秒周期。"""
UART_HEARTBEAT_LOSS_S: Final = 3
"""3秒途絶えたら相手を切断とみなす。"""
UART_UPSTREAM_LOST_S: Final = 10
"""作品01は上位の HEARTBEAT が10秒途絶えたら手動指定を解除し UPSTREAM_LOST を送る。"""
UART_STATE_PERIOD_S: Final = 60
"""作品01は起動後の最初のフレームとして STATE を送り、以後60秒ごとにも送る。"""
UART_INITIAL_DEVICE_STATE: Final = "INIT"
"""TELEMETRY を受ける前の state の mode（v0.2.1、#16）。"""

# --- MQTT の QoS と retain（第2章のトピック表） ---
QOS_BY_KIND: Final[dict[str, int]] = {
    KIND_TELEMETRY: 0,
    KIND_EVENT: 1,
    KIND_STATE: 1,
    KIND_CMD: 1,
    KIND_CMD_ACK: 1,
    KIND_REQUEST: 1,
    KIND_REQUEST_ACK: 1,
}
RETAIN_KINDS: Final[frozenset[str]] = frozenset({KIND_STATE})
"""retain で発行する kind（state だけ。LWT で offline）。"""
