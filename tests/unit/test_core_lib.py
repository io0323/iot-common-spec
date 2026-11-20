"""Phase 2：clock・ids・envelope・dedup・quality・lease のテスト。

数値は iotspec.constants を参照し、時刻は VirtualClock で進める（time.sleep は使わない）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from iotspec import constants as c
from iotspec.clock import SystemClock, VirtualClock, format_ts, parse_ts
from iotspec.dedup import SeqDedupWindow
from iotspec.envelope import Envelope, EnvelopeBuilder, InvalidMessageError, SeqCounter
from iotspec.ids import new_boot_id, new_ulid, schedule_request_id
from iotspec.lease import InvalidLeaseError, Lease, renew_due, resolve_lease_s
from iotspec.quality import classify_clock_skew, classify_late, is_stale
from iotspec.schema import validate, validator_for

T0 = datetime(2026, 11, 20, 6, 0, 0, tzinfo=UTC)


def _conforms(defn: str, value: object) -> bool:
    """common.schema.json の $defs/<defn> に value が適合するか。"""
    validator = validator_for("iot.v1.telemetry")
    sub = validator.evolve(
        schema={"$ref": f"common.schema.json#/$defs/{defn}"},
    )
    return sub.is_valid(value)


# ---------------------------------------------------------------- clock


def test_virtual_clock_advance_and_format() -> None:
    clock = VirtualClock(T0)
    assert clock.now() == T0
    clock.advance(1.2345)
    assert format_ts(clock.now()) == "2026-11-20T06:00:01.234Z"
    clock.advance(timedelta(minutes=1))
    assert clock.now() == T0 + timedelta(minutes=1, seconds=1.2345)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_clock_rejects_naive_and_normalises_to_utc() -> None:
    with pytest.raises(ValueError):
        VirtualClock(datetime(2026, 11, 20))
    jst = timezone(timedelta(hours=9))
    assert VirtualClock(datetime(2026, 11, 20, 15, tzinfo=jst)).now() == T0.replace(hour=6)
    assert SystemClock().now().tzinfo is not None


def test_parse_ts_round_trip_and_rejects_bad_format() -> None:
    text = "2026-11-20T06:00:12.345Z"
    assert format_ts(parse_ts(text)) == text
    for bad in ("2026-11-20T06:00:12Z", "2026-11-20T15:00:12.345+09:00"):
        with pytest.raises(ValueError):
            parse_ts(bad)


# ---------------------------------------------------------------- ids

# 独立に計算したベクトル：hashlib.sha256 と手書きの Crockford Base32 で求めた値
# （python-ulid は使っていない）
SCHEDULE_VECTORS = [
    (12, "2026-11-20T22:00:00.000Z", "01M7XFJER06EKF25TEAMTX19AG"),
    (0, "2026-11-21T06:00:00.000Z", "01M7YB1BR0YPX8AME9QETVC9V6"),
    (4294967296, "2030-01-01T00:00:00.123Z", "01Q3DCBD3VCYPG6MNBHNB34832"),
    (12, "2026-11-20T22:00:00.001Z", "01M7XFJER1Y9BMZGFF8Z30MV72"),
]


@pytest.mark.parametrize(("schedule_id", "planned_at", "expected"), SCHEDULE_VECTORS)
def test_schedule_request_id_vectors(schedule_id: int, planned_at: str, expected: str) -> None:
    value = schedule_request_id(schedule_id, parse_ts(planned_at))
    assert value == expected
    assert _conforms("ulid", value)


def test_schedule_request_id_is_deterministic_and_input_sensitive() -> None:
    planned = parse_ts("2026-11-20T22:00:00.000Z")
    assert schedule_request_id(12, planned) == schedule_request_id(12, planned)
    assert schedule_request_id(12, planned) != schedule_request_id(13, planned)
    later = planned + timedelta(milliseconds=1)
    assert schedule_request_id(12, planned) != schedule_request_id(12, later)
    # 同じ瞬間を別のタイムゾーンで渡しても同じ値
    jst = planned.astimezone(timezone(timedelta(hours=9)))
    assert schedule_request_id(12, jst) == schedule_request_id(12, planned)
    # ミリ秒未満は切り捨て
    assert schedule_request_id(12, planned + timedelta(microseconds=999)) == (
        schedule_request_id(12, planned)
    )


@given(
    st.integers(min_value=0, max_value=2**63),
    st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
    ),
)
def test_schedule_request_id_conforms_and_time_part(schedule_id: int, planned: datetime) -> None:
    value = schedule_request_id(schedule_id, planned)
    assert _conforms("ulid", value)
    # 時刻部（先頭10文字）は planned_at のミリ秒から決まり、ULID の時刻順に並ぶ
    assert value[:10] == schedule_request_id(0, planned)[:10]


def test_new_ulid_and_boot_id_conform() -> None:
    clock = VirtualClock(T0)
    for _ in range(100):
        assert _conforms("ulid", new_ulid(clock))
        boot_id = new_boot_id()
        assert len(boot_id) == c.BOOT_ID_BITS // 4
        assert _conforms("boot_id", boot_id)


# ---------------------------------------------------------------- envelope


def test_builder_output_conforms_and_round_trips() -> None:
    clock = VirtualClock(T0)
    builder = EnvelopeBuilder(c.DEVICE_MCU_CPP_01, clock)
    payload = {
        "readings": [{"channel": "temp_air", "value": 41.25, "unit": "degC", "quality": "good"}]
    }
    env = builder.build(c.KIND_TELEMETRY, payload, uptime_ms=1234)
    message = env.to_dict()
    assert validate(message) == []
    assert message["schema"] == "iot.v1.telemetry"
    assert message["ts"] == "2026-11-20T06:00:00.000Z"
    assert message["boot_id"] == builder.boot_id
    assert Envelope.from_dict(message) == env


def test_builder_seq_per_kind_and_shared_boot_id() -> None:
    builder = EnvelopeBuilder(c.DEVICE_GW_01, VirtualClock(T0), SeqCounter("0000abcd"))
    ack = {"request_id": "01JD3Q8Z5K7M2N4P6R8T0V0001", "status": "rejected", "reason": "busy"}
    seqs = [builder.build(c.KIND_REQUEST_ACK, ack).seq for _ in range(3)]
    event = builder.build(c.KIND_EVENT, {"code": "BOOT", "severity": "info"})
    assert seqs == [0, 1, 2]
    assert event.seq == 0
    assert event.boot_id == "0000abcd"
    assert event.kind == "event"
    with pytest.raises(ValueError):
        builder.build("metrics", {})


def test_from_dict_keeps_unknown_fields_and_rejects_invalid() -> None:
    builder = EnvelopeBuilder(c.DEVICE_MCU_CPP_01, VirtualClock(T0))
    message = builder.build(c.KIND_EVENT, {"code": "BOOT", "severity": "info"}).to_dict()
    message["trace"] = "x"
    env = Envelope.from_dict(message)
    assert env.extra == {"trace": "x"}
    assert env.to_dict() == message
    message["seq"] = c.UINT32_MAX + 1
    with pytest.raises(InvalidMessageError) as info:
        Envelope.from_dict(message)
    assert any(e.startswith("/seq: ") for e in info.value.errors)


def test_seq_counter_wraps_at_uint32() -> None:
    counter = SeqCounter("00000001")
    counter._next[c.KIND_TELEMETRY] = c.UINT32_MAX
    assert counter.next(c.KIND_TELEMETRY) == c.UINT32_MAX
    assert counter.next(c.KIND_TELEMETRY) == 0


@given(st.lists(st.sampled_from(c.KINDS), max_size=300))
def test_seq_counter_property_consecutive_per_kind(kinds: list[str]) -> None:
    """性質：どの順で kind を混ぜても、kind ごとに 0,1,2,... の連番になる。"""
    counter = SeqCounter()
    got: dict[str, list[int]] = {}
    for kind in kinds:
        got.setdefault(kind, []).append(counter.next(kind))
    for values in got.values():
        assert values == list(range(len(values)))
    assert _conforms("boot_id", counter.boot_id)


@given(st.integers(min_value=0, max_value=c.UINT32_MAX), st.integers(min_value=1, max_value=50))
def test_seq_counter_property_modulo(start: int, steps: int) -> None:
    """性質：連番は常に uint32 の範囲にあり、(前の値 + 1) mod 2^32 で進む。"""
    counter = SeqCounter("00000001")
    counter._next[c.KIND_STATE] = start
    previous = counter.next(c.KIND_STATE)
    for _ in range(steps):
        value = counter.next(c.KIND_STATE)
        assert 0 <= value <= c.UINT32_MAX
        assert value == (previous + 1) % (c.UINT32_MAX + 1)
        previous = value


# ---------------------------------------------------------------- dedup

DEV, BOOT_A, BOOT_B, KIND = c.DEVICE_MCU_CPP_01, "aaaaaaaa", "bbbbbbbb", c.KIND_TELEMETRY


def test_dedup_detects_duplicate() -> None:
    window = SeqDedupWindow()
    assert window.is_duplicate(DEV, BOOT_A, KIND, 5) is False
    assert window.is_duplicate(DEV, BOOT_A, KIND, 5) is True
    assert window.is_duplicate(DEV, BOOT_A, KIND, 6) is False
    # kind・発信元が違えば別の記憶
    assert window.is_duplicate(DEV, BOOT_A, c.KIND_EVENT, 5) is False
    assert window.is_duplicate(c.DEVICE_MCU_C_01, BOOT_A, KIND, 5) is False


def test_dedup_capacity_comes_from_constants_and_forgets_oldest() -> None:
    window = SeqDedupWindow()
    assert window.capacity == c.SEQ_DEDUP_CAPACITY
    for seq in range(c.SEQ_DEDUP_CAPACITY):
        assert window.is_duplicate(DEV, BOOT_A, KIND, seq) is False
    assert window.remembered(DEV, KIND) == c.SEQ_DEDUP_CAPACITY
    # 1,024 個の範囲内は重複
    assert window.is_duplicate(DEV, BOOT_A, KIND, 0) is True
    # 1,025 個目で最も古い seq（0）を忘れる
    assert window.is_duplicate(DEV, BOOT_A, KIND, c.SEQ_DEDUP_CAPACITY) is False
    assert window.remembered(DEV, KIND) == c.SEQ_DEDUP_CAPACITY
    assert window.is_duplicate(DEV, BOOT_A, KIND, 1) is True
    # 忘れた seq は新しいものとして受け入れる（再配送は直近32件の範囲に限られる前提）
    assert window.is_duplicate(DEV, BOOT_A, KIND, 0) is False


def test_dedup_resets_on_boot_id_change() -> None:
    window = SeqDedupWindow()
    for seq in range(10):
        window.is_duplicate(DEV, BOOT_A, KIND, seq)
    window.is_duplicate(DEV, BOOT_A, c.KIND_EVENT, 0)
    # 再起動：同じ seq でも新しい起動の電文として受け入れる
    assert window.is_duplicate(DEV, BOOT_B, KIND, 0) is False
    assert window.remembered(DEV, KIND) == 1
    assert window.is_duplicate(DEV, BOOT_B, KIND, 0) is True
    # 他の kind の記憶はその kind の電文が来るまでそのまま
    assert window.is_duplicate(DEV, BOOT_A, c.KIND_EVENT, 0) is True


@given(
    st.lists(st.integers(min_value=0, max_value=c.UINT32_MAX), max_size=200),
    st.integers(min_value=1, max_value=64),
)
def test_dedup_property_matches_reference_model(seqs: list[int], capacity: int) -> None:
    """性質：判定結果は「直近 capacity 個の受け入れ済み seq に含まれるか」と一致する。"""
    window = SeqDedupWindow(capacity)
    accepted: list[int] = []
    for seq in seqs:
        expected = seq in accepted[-capacity:]
        assert window.is_duplicate(DEV, BOOT_A, KIND, seq) is expected
        if not expected:
            accepted.append(seq)
        assert window.remembered(DEV, KIND) == min(len(accepted), capacity)


@given(st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=100))
def test_dedup_property_second_delivery_within_window_is_dropped(seqs: list[int]) -> None:
    """性質：MQTT の再配送（直近 MQTT_MAX_IN_FLIGHT 件以内の再送）は必ず重複と判定される。"""
    window = SeqDedupWindow()
    accepted: list[int] = []
    for seq in seqs:
        if not window.is_duplicate(DEV, BOOT_A, KIND, seq):
            accepted.append(seq)
    for seq in accepted[-c.MQTT_MAX_IN_FLIGHT :]:
        assert window.is_duplicate(DEV, BOOT_A, KIND, seq) is True


# ---------------------------------------------------------------- quality

PERIOD_MS = 1000


def test_is_stale_boundary() -> None:
    limit = timedelta(milliseconds=PERIOD_MS * c.STALE_PERIOD_MULTIPLIER)
    assert is_stale(T0, PERIOD_MS, T0 + limit) is False
    assert is_stale(T0, PERIOD_MS, T0 + limit + timedelta(milliseconds=1)) is True
    assert is_stale(T0, PERIOD_MS, T0) is False


def test_classify_clock_skew_boundary() -> None:
    limit = timedelta(seconds=c.CLOCK_SKEW_THRESHOLD_S)
    one_ms = timedelta(milliseconds=1)
    assert classify_clock_skew(T0, T0 + limit) == "ok"
    assert classify_clock_skew(T0 + limit, T0) == "ok"
    assert classify_clock_skew(T0, T0 + limit + one_ms) == "skew"
    assert classify_clock_skew(T0 + limit + one_ms, T0) == "skew"


def test_classify_late_boundaries() -> None:
    three_periods = timedelta(milliseconds=PERIOD_MS * c.LATE_PERIOD_MULTIPLIER)
    max_age = timedelta(seconds=c.LATE_MAX_AGE_S)
    one_ms = timedelta(milliseconds=1)
    received = T0 + timedelta(hours=1)
    assert classify_late(received - three_periods, received, PERIOD_MS) == "on_time"
    assert classify_late(received - three_periods - one_ms, received, PERIOD_MS) == "late"
    assert classify_late(received - max_age, received, PERIOD_MS) == "late"
    # 5分を超える差は時刻ずれの扱い
    assert classify_late(received - max_age - one_ms, received, PERIOD_MS) == "on_time"
    # 未来の ts は遅延ではない
    assert classify_late(received + three_periods * 2, received, PERIOD_MS) == "on_time"


def test_quality_uses_virtual_clock() -> None:
    clock = VirtualClock(T0)
    last_update = clock.now()
    clock.advance(timedelta(milliseconds=PERIOD_MS * c.STALE_PERIOD_MULTIPLIER))
    assert is_stale(last_update, PERIOD_MS, clock.now()) is False
    clock.advance(0.001)
    assert is_stale(last_update, PERIOD_MS, clock.now()) is True


# ---------------------------------------------------------------- lease


def test_resolve_lease_s_default_and_limits() -> None:
    assert resolve_lease_s({"op": "set_actuator"}) == c.LEASE_DEFAULT_S
    assert resolve_lease_s({"op": "set_actuator", "lease_s": c.LEASE_MAX_S}) == c.LEASE_MAX_S
    assert resolve_lease_s({"op": "set_actuator", "lease_s": c.LEASE_MIN_S}) == c.LEASE_MIN_S
    for bad in (c.LEASE_MAX_S + 1, c.LEASE_MIN_S - 1, "60", 60.0, True):
        with pytest.raises(InvalidLeaseError):
            resolve_lease_s({"op": "set_actuator", "lease_s": bad})


def test_resolve_lease_s_ignored_for_other_ops() -> None:
    for op in ("release", "set_mode", "set_param", "get_status"):
        assert resolve_lease_s({"op": op}) is None
        assert resolve_lease_s({"op": op, "lease_s": c.LEASE_MAX_S + 1}) is None


def test_lease_renew_interval_and_expiry() -> None:
    clock = VirtualClock(T0)
    lease = Lease(clock.now(), c.LEASE_DEFAULT_S)
    interval = timedelta(seconds=c.LEASE_DEFAULT_S / c.LEASE_RENEW_DIVISOR)
    assert lease.renew_interval == interval == timedelta(seconds=20)
    assert lease.expires_at == T0 + timedelta(seconds=c.LEASE_DEFAULT_S)

    clock.advance(interval - timedelta(milliseconds=1))
    assert renew_due(lease, clock.now()) is False
    clock.advance(timedelta(milliseconds=1))
    assert renew_due(lease, clock.now()) is True

    clock.set(lease.expires_at - timedelta(milliseconds=1))
    assert lease.is_expired(clock.now()) is False
    clock.advance(timedelta(milliseconds=1))
    assert lease.is_expired(clock.now()) is True


def test_lease_survives_two_missed_renewals_but_not_three() -> None:
    """第4章：更新が2回続けて届かなくても期限に間に合うが、3回途絶えると期限が切れる。"""
    lease = Lease(T0, c.LEASE_DEFAULT_S)
    two_missed = T0 + lease.renew_interval * 2
    three_missed = T0 + lease.renew_interval * 3
    assert lease.is_expired(two_missed) is False
    assert lease.is_expired(three_missed) is True


def test_lease_rejects_out_of_range() -> None:
    with pytest.raises(InvalidLeaseError):
        Lease(T0, c.LEASE_MAX_S + 1)
