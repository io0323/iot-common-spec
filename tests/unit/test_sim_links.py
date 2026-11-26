"""Phase 5：mcu-c-01・gw-01 簡易版・app-01 の代役と仮想シリアルの単体テスト。

時刻は VirtualClock で進め、time.sleep は使わない。UART・MQTT はメモリ上でつなぐ。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from iotspec import constants as c
from iotspec.clock import VirtualClock, format_ts
from iotspec.ids import schedule_request_id
from iotspec.profile import load_profile
from iotspec.schema import validate
from iotspec.sim.devices.app_01 import App01
from iotspec.sim.devices.gw_01_lite import Gw01Lite
from iotspec.sim.devices.mcu_c_01 import (
    CRITICAL,
    INIT,
    NORMAL,
    SENSOR_FAULT,
    TRANSITION_EVENTS,
    WARNING,
    McuC01,
    Thresholds,
)
from iotspec.sim.scenario import ScenarioError, load_scenario, parse_scenario
from iotspec.sim.serial import DevicePort, OwnPty
from iotspec.sim.signals import SignalSet
from iotspec.sim.simulation import Simulation
from iotspec.uart import cobs
from iotspec.uart.frames import (
    Ack,
    Cmd,
    Diag,
    Event,
    FrameReader,
    Heartbeat,
    State,
    Telemetry,
    encode_frame,
)
from iotspec.uart.mapping import EVENT_CODE, REASON, RESET_REASON, SEVERITY

T0 = datetime(2026, 11, 20, 6, 0, 0, tzinfo=UTC)
MCU_PROFILE = load_profile(c.DEVICE_MCU_C_01)


class _Temp:
    """値を差し替えられる温度の信号。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def sample(self, t: datetime, dt_s: float, inputs: Any, rng: Any) -> float:
        return self.value


# ---------------------------------------------------------------- mcu-c-01


def _mcu(temp: float = 25.0, **kwargs: Any) -> tuple[McuC01, VirtualClock, _Temp]:
    clock = VirtualClock(T0)
    signal = _Temp(temp)
    mcu = McuC01(
        MCU_PROFILE,
        clock,
        SignalSet({"temp_board": signal}, 1, c.DEVICE_MCU_C_01),
        boot_id=0x0BAD_CAFE,
        **kwargs,
    )
    return mcu, clock, signal


def _frames(data: bytes) -> list[Any]:
    return list(FrameReader().feed(data))


def test_mcu_starts_with_state_then_boot() -> None:
    mcu, _, _ = _mcu()
    mcu.start()
    frames = _frames(mcu.drain_tx())
    assert isinstance(frames[0], State) and frames[0].boot_id == 0x0BAD_CAFE
    assert isinstance(frames[1], Event) and frames[1].code == 1  # BOOT


def test_mcu_tick_sends_telemetry_heartbeat_and_periodic_state() -> None:
    mcu, clock, _ = _mcu()
    mcu.start()
    mcu.drain_tx()
    kinds: list[str] = []
    for _ in range(c.UART_STATE_PERIOD_S):
        clock.advance(c.UART_HEARTBEAT_PERIOD_S)
        mcu.feed(encode_frame(Heartbeat(0, 0)))  # 上位は生きている
        mcu.tick()
        kinds += [type(f).__name__ for f in _frames(mcu.drain_tx())]
    assert kinds.count("Telemetry") == kinds.count("Heartbeat") == c.UART_STATE_PERIOD_S
    assert kinds.count("State") == 1  # 60秒ごと
    # 種別ごとの連番（#25）
    seqs = [f.seq for f in mcu.sent if isinstance(f, Telemetry)]
    assert seqs == list(range(len(seqs)))


def _tick(mcu: McuC01, clock: VirtualClock, n: int = 1) -> None:
    for _ in range(n):
        clock.advance(1)
        mcu.feed(encode_frame(Heartbeat(0, 0)))  # 上位は生きている
        mcu.tick()


_TEMP_OF = {INIT: 25.0, NORMAL: 25.0, WARNING: 50.0, CRITICAL: 70.0, SENSOR_FAULT: 25.0}
"""既定のしきい値（45／60／3℃）で各状態に分類される温度。"""


class _Rig:
    """状態を作り、温度・故障・承認を差し替えて遷移させる。"""

    def __init__(self, state: int, **kwargs: Any) -> None:
        self.mcu, self.clock, self.signal = _mcu(_TEMP_OF[state], **kwargs)
        self.faulty = state == SENSOR_FAULT
        self.mcu._temp_faulty = lambda: self.faulty  # type: ignore[method-assign]
        if state != INIT:
            self.mcu.start()
            assert self.mcu.state == state

    def tick(self, n: int = 1) -> None:
        _tick(self.mcu, self.clock, n)

    def events_during(self, action: Any) -> list[Event]:
        before = len(self.mcu.sent)
        action()
        return [f for f in self.mcu.sent[before:] if isinstance(f, Event)]


def _make_transition(rig: _Rig, to: int) -> None:
    """rig.mcu を今の状態から to へ遷移させる（作品01の詳細設計 第4章の条件どおり）。"""
    frm = rig.mcu.state
    if frm == INIT:
        rig.signal.value = _TEMP_OF[to]
        rig.faulty = to == SENSOR_FAULT
        rig.mcu.start()
    elif to == SENSOR_FAULT:
        rig.faulty = True
        rig.tick()
    elif frm == SENSOR_FAULT:
        rig.faulty = False
        rig.signal.value = _TEMP_OF[to]
        rig.tick()  # 正常なサンプルが1秒続くまでは承認しても戻らない
        rig.mcu.press_ack()
        rig.tick()
    elif frm == CRITICAL:
        rig.signal.value = 50.0 if to == WARNING else 30.0  # crit−hyst 未満で承認
        rig.mcu.press_ack()
        rig.tick()
    else:
        rig.signal.value = {NORMAL: 30.0, WARNING: 50.0, CRITICAL: 70.0}[to]
        rig.tick(2)  # 1秒継続で確定


@pytest.mark.parametrize(
    ("frm", "to"), sorted(TRANSITION_EVENTS), ids=lambda s: MCU_PROFILE.state_name(s)
)
def test_mcu_sends_one_event_per_transition_from_the_table(frm: int, to: int) -> None:
    """v0.2.2 #41：表の全行。1回の遷移で EVENT は1件、from・to は遷移の前後の状態番号。"""
    rig = _Rig(frm)
    events = rig.events_during(lambda: _make_transition(rig, to))
    if frm == INIT:
        events = events[1:]  # STATE → BOOT の後
    assert rig.mcu.state == to
    code, severity = TRANSITION_EVENTS[(frm, to)]
    assert [(e.code, e.severity, e.from_state, e.to_state) for e in events] == [
        (EVENT_CODE.to_uart(code), SEVERITY.to_uart(severity), frm, to)
    ]


def test_transition_table_matches_spec() -> None:
    """spec.md 第7章「状態遷移と EVENT（v0.2.2）」の表と同じ（作品01の基本設計 第8章）。"""
    by_code: dict[str, set[tuple[int, int]]] = {}
    for key, (code, _) in TRANSITION_EVENTS.items():
        by_code.setdefault(code, set()).add(key)
    assert by_code["THRESHOLD_EXCEEDED"] == {
        (NORMAL, WARNING),
        (NORMAL, CRITICAL),
        (WARNING, CRITICAL),
    }
    assert by_code["THRESHOLD_CLEARED"] == {
        (WARNING, NORMAL),
        (CRITICAL, WARNING),
        (CRITICAL, NORMAL),
    }
    assert by_code["SENSOR_FAULT"] == {(s, SENSOR_FAULT) for s in (INIT, NORMAL, WARNING, CRITICAL)}
    assert by_code["SENSOR_RECOVERED"] == {(SENSOR_FAULT, s) for s in (NORMAL, WARNING, CRITICAL)}
    assert by_code["STATE_CHANGED"] == {(INIT, s) for s in (NORMAL, WARNING, CRITICAL)}
    assert TRANSITION_EVENTS[(NORMAL, WARNING)][1] == "warning"
    assert {TRANSITION_EVENTS[k][1] for k in by_code["THRESHOLD_CLEARED"]} == {"info"}


def test_mcu_thresholds_hysteresis_and_approval() -> None:
    rig = _Rig(NORMAL)
    assert Thresholds() == Thresholds(4500, 6000, 300)
    rig.signal.value = 46.0
    rig.tick()
    assert rig.mcu.state == NORMAL  # 1秒続くまでは確定しない
    rig.tick()
    assert rig.mcu.state == WARNING
    rig.signal.value = 43.0  # warn−hyst（42℃）以上なので戻らない
    rig.tick(3)
    assert rig.mcu.state == WARNING
    rig.signal.value = 70.0
    rig.tick(2)
    assert rig.mcu.state == CRITICAL
    rig.signal.value = 30.0
    rig.tick(5)
    assert rig.mcu.state == CRITICAL  # 温度が下がっても承認がなければ戻らない
    rig.signal.value = 58.0  # crit−hyst（57℃）以上での承認は無効で、消費される
    rig.mcu.press_ack()
    rig.tick()
    rig.signal.value = 30.0
    rig.tick()
    assert rig.mcu.state == CRITICAL
    rig.mcu.press_ack()
    rig.tick()
    assert rig.mcu.state == NORMAL


def test_mcu_fan_floor_limited_and_interlock_events() -> None:
    rig = _Rig(WARNING)
    assert rig.mcu.fan_pm() == 700  # 下限
    sent = len(rig.mcu.sent)
    rig.mcu.feed(encode_frame(Cmd(0, 1, c.UART_OP_SET_FAN_OVERRIDE, 60, (400,))))
    out = rig.mcu.sent[sent:]
    assert isinstance(out[0], Ack) and (out[0].status, out[0].limited, out[0].applied) == (
        1,
        1,
        700,
    )
    assert isinstance(out[1], Event) and (out[1].code, out[1].severity) == (9, 1)
    assert out[1].from_state == out[1].to_state == WARNING
    # 下限を超える指定は丸めない。引き上げをやめたので INTERLOCK_RELEASED
    events = rig.events_during(
        lambda: rig.mcu.feed(encode_frame(Cmd(1, 2, c.UART_OP_SET_FAN_OVERRIDE, 60, (800,))))
    )
    assert [(e.code, e.severity, e.from_state, e.to_state) for e in events] == [
        (10, 0, WARNING, WARNING)
    ]
    ack = [f for f in rig.mcu.sent if isinstance(f, Ack)][-1]
    assert (ack.limited, ack.applied) == (0, 800)


def test_mcu_interlock_follows_state_changes_after_the_transition_event() -> None:
    rig = _Rig(NORMAL)
    rig.mcu.feed(encode_frame(Cmd(0, 1, c.UART_OP_SET_FAN_OVERRIDE, 600, (300,))))
    assert rig.mcu.fan_pm() == 300 and not rig.mcu.interlocked
    rig.signal.value = 50.0
    events = rig.events_during(lambda: rig.tick(2))
    assert [(e.code, e.from_state, e.to_state) for e in events] == [
        (3, NORMAL, WARNING),  # 遷移の EVENT は1件
        (9, WARNING, WARNING),  # そのあと、下限での引き上げ
    ]
    rig.signal.value = 30.0
    events = rig.events_during(lambda: rig.tick(2))
    assert [(e.code, e.from_state, e.to_state) for e in events] == [
        (4, WARNING, NORMAL),
        (10, NORMAL, NORMAL),
    ]
    # リースが切れたときも戻す
    rig.signal.value = 50.0
    rig.tick(2)
    assert rig.mcu.interlocked
    events = rig.events_during(lambda: rig.tick(600))
    assert (10, WARNING, WARNING) in [(e.code, e.from_state, e.to_state) for e in events]


def test_mcu_auto_fan_without_override() -> None:
    rig = _Rig(NORMAL)
    for temp, fan in ((30.0, 0), (35.0, 0), (40.0, 200), (44.99, 399)):
        rig.signal.value = temp
        rig.tick()
        assert rig.mcu.fan_pm() == fan, temp


@pytest.mark.parametrize(
    ("ttl_s", "accepted"), [(0, False), (1, True), (600, True), (601, False), (0xFFFF, False)]
)
def test_mcu_ttl_s_range(ttl_s: int, accepted: bool) -> None:
    """v0.2.2 #40：SET_FAN_OVERRIDE の ttl_s は 1〜600、範囲外は rejected（invalid_arg）。"""
    mcu, _, _ = _mcu()
    mcu.start()
    mcu.feed(encode_frame(Cmd(0, 1, c.UART_OP_SET_FAN_OVERRIDE, ttl_s, (500,))))
    (ack,) = [f for f in _frames(mcu.drain_tx()) if isinstance(f, Ack)]
    assert (ack.status, ack.reason) == ((1, 0) if accepted else (2, 2))


def test_mcu_never_returns_expired() -> None:
    mcu, clock, _ = _mcu()
    mcu.start()
    ref = 0
    for op, ttl_s, args in [
        (c.UART_OP_SET_FAN_OVERRIDE, 1, (500,)),
        (c.UART_OP_SET_FAN_OVERRIDE, 0, (500,)),
        (c.UART_OP_SET_FAN_OVERRIDE, 600, (1001,)),
        (c.UART_OP_CLEAR_OVERRIDE, 0, ()),
        (c.UART_OP_SET_THRESHOLD, 0, (4500, 6000, 300)),
        (c.UART_OP_GET_STATUS, 0, ()),
        (c.UART_OP_GET_DIAG, 0, ()),
        (0x7F, 0, ()),
    ]:
        ref += 1
        mcu.feed(encode_frame(Cmd(ref, ref, op, ttl_s, args)))
        clock.advance(2)
        mcu.tick()
    acks = [f for f in mcu.sent if isinstance(f, Ack)]
    assert len(acks) == ref
    assert REASON.to_uart("expired") not in {a.reason for a in acks}


def test_mcu_set_threshold_rules() -> None:
    """作品01の設計 第8章：CRITICAL・SENSOR_FAULT 中は unsafe、範囲外は invalid_arg。"""
    rig = _Rig(NORMAL)
    cases = [
        ((4500, 6000, 300), (1, 0)),
        ((-2000, 8000, 50), (1, 0)),
        ((-2001, 6000, 300), (2, 2)),
        ((4500, 8001, 300), (2, 2)),
        ((6000, 6000, 300), (2, 2)),
        ((4500, 6000, 49), (2, 2)),
        ((4500, 6000, 1001), (2, 2)),
        ((5800, 6000, 300), (2, 2)),  # crit−warn < hyst
    ]
    for ref, (args, expected) in enumerate(cases, start=1):
        rig.mcu.feed(encode_frame(Cmd(ref, ref, c.UART_OP_SET_THRESHOLD, 0, args)))
        ack = [f for f in rig.mcu.sent if isinstance(f, Ack)][-1]
        assert (ack.status, ack.reason) == expected, args
    for state in (CRITICAL, SENSOR_FAULT):
        rig = _Rig(state)
        rig.mcu.feed(encode_frame(Cmd(0, 99, c.UART_OP_SET_THRESHOLD, 0, (4500, 6000, 300))))
        ack = [f for f in rig.mcu.sent if isinstance(f, Ack)][-1]
        assert (ack.status, ack.reason) == (2, 1)  # unsafe


def test_mcu_get_diag_sends_diag_then_ack_and_resends_on_duplicate() -> None:
    mcu, _, _ = _mcu(reset_reason="watchdog", reset_count=3)
    mcu.start()
    mcu.drain_tx()
    mcu.feed(encode_frame(Cmd(0, 7, c.UART_OP_GET_DIAG, 0)))
    frames = _frames(mcu.drain_tx())
    assert [type(f).__name__ for f in frames] == ["Diag", "Ack"]
    diag, ack = frames
    assert isinstance(diag, Diag) and isinstance(ack, Ack)
    assert (diag.reset_reason, diag.reset_count) == (3, 3)
    assert (ack.cmd_ref, ack.status, ack.reason) == (7, 1, 0)
    mcu.feed(encode_frame(Cmd(1, 7, c.UART_OP_GET_DIAG, 0)))
    again = _frames(mcu.drain_tx())
    assert [type(f).__name__ for f in again] == ["Diag", "Ack"]
    assert isinstance(again[0], Diag) and again[0].seq == diag.seq + 1


def test_mcu_diag_counters_and_saturation() -> None:
    mcu, _, _ = _mcu()
    good = encode_frame(Heartbeat(0, 0))
    raw = bytearray(cobs.decode(good[:-1]))
    raw[-1] ^= 0xFF
    mcu.feed(cobs.encode(bytes(raw)) + b"\x00")  # CRC 不一致
    mcu.feed(b"\x05\x00")  # COBS 不正
    mcu.feed(encode_frame(Telemetry(0, 0, 0, 0, 0, 0)))  # 受け付けない種別
    diag = mcu.diag_frame()
    assert (diag.rx_crc_errors, diag.rx_frame_errors) == (1, 2)
    mcu.stats["discarded:crc_mismatch"] = 70_000
    mcu.stats["unexpected_frame"] = 70_000
    diag = mcu.diag_frame()
    assert diag.rx_crc_errors == diag.rx_frame_errors == c.UART_DIAG_U16_SATURATED


@pytest.mark.parametrize("reason", list(c.UART_RESET_REASONS.values()))
def test_mcu_boot_carries_reset_reason(reason: str) -> None:
    """v0.2.2 #38：BOOT の from は reset_reason、to は 0。"""
    mcu, _, _ = _mcu(reset_reason=reason)
    mcu.start()
    boot = next(f for f in mcu.sent if isinstance(f, Event))
    assert (boot.code, boot.from_state, boot.to_state) == (1, RESET_REASON.to_uart(reason), 0)


def test_mcu_default_reset_reason_is_power_on() -> None:
    mcu, _, _ = _mcu()
    mcu.start()
    boot = next(f for f in mcu.sent if isinstance(f, Event))
    assert boot.from_state == RESET_REASON.to_uart("power_on")


def test_mcu_ack_and_duplicate_cmd_ref() -> None:
    mcu, _clock, _ = _mcu()
    mcu.start()
    mcu.drain_tx()
    cmd = Cmd(0, 0x1234, c.UART_OP_SET_FAN_OVERRIDE, 60, (800,))
    mcu.feed(encode_frame(cmd))
    (first,) = [f for f in _frames(mcu.drain_tx()) if isinstance(f, Ack)]
    assert (first.cmd_ref, first.status, first.applied) == (0x1234, 1, 800)
    assert mcu.fan_pm() == 800
    mcu.feed(encode_frame(Cmd(1, 0x1234, c.UART_OP_SET_FAN_OVERRIDE, 60, (100,))))
    (again,) = [f for f in _frames(mcu.drain_tx()) if isinstance(f, Ack)]
    assert (again.status, again.applied) == (first.status, first.applied)
    assert again.seq == first.seq + 1  # ACK の連番は進む
    assert mcu.fan_pm() == 800  # 再実行しない
    assert mcu.stats["duplicate_cmd_ref"] == 1


def test_mcu_remembers_only_last_16_cmd_refs() -> None:
    mcu, _, _ = _mcu()
    for ref in range(c.UART_CMD_REF_DEDUP_CAPACITY + 1):
        mcu.feed(encode_frame(Cmd(ref, ref, c.UART_OP_GET_DIAG, 0)))
    mcu.feed(encode_frame(Cmd(99, 0, c.UART_OP_GET_DIAG, 0)))
    assert mcu.stats["duplicate_cmd_ref"] == 0  # 最も古い cmd_ref 0 は忘れている
    mcu.feed(encode_frame(Cmd(100, c.UART_CMD_REF_DEDUP_CAPACITY, c.UART_OP_GET_DIAG, 0)))
    assert mcu.stats["duplicate_cmd_ref"] == 1


def test_mcu_rejects_unknown_op_and_bad_args() -> None:
    mcu, _, _ = _mcu()
    mcu.feed(encode_frame(Cmd(0, 1, 0x7F, 0)))
    mcu.feed(encode_frame(Cmd(0, 2, c.UART_OP_SET_FAN_OVERRIDE, 0, (500,))))  # ttl_s 0
    mcu.feed(encode_frame(Cmd(1, 3, c.UART_OP_SET_THRESHOLD, 0, (4500, 3500, 100))))  # warn >= crit
    acks = [f for f in _frames(mcu.drain_tx()) if isinstance(f, Ack)]
    assert [(a.status, a.reason) for a in acks] == [(2, 3), (2, 2), (2, 2)]


def test_mcu_upstream_lost_clears_override_once() -> None:
    mcu, clock, _ = _mcu()
    mcu.start()
    mcu.feed(encode_frame(Cmd(0, 1, c.UART_OP_SET_FAN_OVERRIDE, 600, (900,))))
    for _ in range(c.UART_UPSTREAM_LOST_S - 1):
        clock.advance(1)
        mcu.tick()
    assert not mcu.upstream_lost and mcu.fan_pm() == 900
    clock.advance(1)
    mcu.tick()
    clock.advance(1)
    mcu.tick()
    assert mcu.upstream_lost and mcu.override is None
    lost = [f for f in mcu.sent if isinstance(f, Event) and f.code == 7]
    assert len(lost) == 1
    mcu.feed(encode_frame(Heartbeat(0, 0)))
    assert not mcu.upstream_lost


def test_mcu_sensor_fault_and_corrupt_frames() -> None:
    scenario = parse_scenario(
        {
            "name": "t",
            "start": format_ts(T0),
            "duration_s": 10,
            "devices": [
                {
                    "device_id": "mcu-c-01",
                    "type": "mcu_c_01",
                    "signals": {"temp_board": {"kind": "constant", "value": 25}},
                    "acks_at_s": [6],  # 故障が終わって正常なサンプルが1秒続いたあとの承認
                }
            ],
            "faults": [
                {
                    "type": "sensor_fault",
                    "device": "mcu-c-01",
                    "channel": "temp_board",
                    "at_s": 2,
                    "duration_s": 3,
                },
                {"type": "corrupt_frame", "device": "mcu-c-01", "at_s": 7, "count": 2},
            ],
        }
    )
    sim = Simulation(scenario, speed=0)
    sim.schedule()
    mcu = sim.mcu_c
    assert mcu is not None
    states: list[int] = []
    sim.runtime.run_until(scenario.end, after_step=lambda: states.append(mcu.state))
    assert SENSOR_FAULT in states and states[-1] == NORMAL
    codes = [f.code for f in mcu.sent if isinstance(f, Event)]
    assert 5 in codes and 6 in codes  # SENSOR_FAULT と SENSOR_RECOVERED
    assert mcu.stats["corrupted_sent"] == 2


# ---------------------------------------------------------------- gw-01 簡易版（UART 上位側）


def _gw(**kwargs: Any) -> tuple[Gw01Lite, VirtualClock]:
    clock = VirtualClock(T0)
    gw = Gw01Lite(
        load_profile(c.DEVICE_GW_01), clock, site=c.DEFAULT_SITE, uart_profile=MCU_PROFILE, **kwargs
    )
    return gw, clock


def _cmds(data: bytes) -> list[Cmd]:
    return [f for f in _frames(data) if isinstance(f, Cmd)]


def test_gw_rate_limit_and_in_flight_limit() -> None:
    """毎秒1件まで（再送も数える。open-questions #33）、未完了は UART_CMD_MAX_IN_FLIGHT 件まで。"""
    gw, clock = _gw()
    for level in range(10):
        gw.submit_uart(
            {"op": "set_actuator", "args": {"id": "fan_main", "level": level}, "lease_s": 60}
        )
    per_second: list[list[int]] = []
    for _ in range(30):
        clock.advance(1)
        gw.tick()
        per_second.append([cmd.cmd_ref for cmd in _cmds(gw.drain_uart())])
        assert len(gw._in_flight) <= c.UART_CMD_MAX_IN_FLIGHT
    assert all(len(refs) <= c.UART_CMD_RATE_PER_S for refs in per_second)
    sent = [ref for refs in per_second for ref in refs]
    # ACK を返さないので、どの cmd_ref も最初の送信＋3回の再送までで、終わったものは timed_out
    assert all(sent.count(ref) <= 1 + c.UART_CMD_MAX_RETRIES for ref in set(sent))
    finished = [r for r in gw.cmds.values() if r.timed_out]
    assert finished and all(sent.count(r.cmd_ref) == 1 + c.UART_CMD_MAX_RETRIES for r in finished)  # type: ignore[arg-type]
    assert sorted(set(sent)) == list(range(len(set(sent))))


def test_gw_retransmits_same_cmd_ref_three_times_then_times_out() -> None:
    gw, clock = _gw()
    record = gw.submit_uart({"op": "get_status"})
    refs: list[int] = []
    for _ in range(20):
        clock.advance(1)
        gw.tick()
        refs += [cmd.cmd_ref for cmd in _cmds(gw.drain_uart())]
    assert refs == [record.cmd_ref] * (1 + c.UART_CMD_MAX_RETRIES)
    assert record.timed_out and gw.stats["uart_cmd_timeout"] == 1


def test_gw_and_mcu_linked_in_memory() -> None:
    sim = Simulation(load_scenario("uart_link"), speed=0)
    out = sim.run_batch()
    gw, mcu = sim.gw, sim.mcu_c
    assert gw is not None and mcu is not None and gw.bridge is not None
    assert all(r.ack is not None and r.ack["status"] == "done" for r in gw.cmds.values())
    assert mcu.stats["duplicate_cmd_ref"] == 1 and gw.stats["duplicate_ack"] == 1
    assert gw.bridge.discards["crc_mismatch"] == 2
    assert mcu.upstream_lost is False  # HEARTBEAT の途絶は解消している
    events = [o.message["payload"]["code"] for o in out if o.topic.endswith("mcu-c-01/event")]  # type: ignore[index]
    assert events[0] == "BOOT" and "UPSTREAM_LOST" in events
    for o in out:
        assert validate(o.message) == [], o.topic
    seqs: dict[str, list[int]] = {}
    for o in out:
        if "/mcu-c-01/" in o.topic:
            seqs.setdefault(o.kind, []).append(int(o.message["seq"]))  # type: ignore[call-overload]
    # 破損で欠けた TELEMETRY 以外は kind ごとに連番（#25）
    assert seqs["cmd/ack"] == list(range(len(seqs["cmd/ack"])))


def test_gw_reports_device_offline_after_uart_silence() -> None:
    gw, clock = _gw()
    gw.feed_uart(encode_frame(Heartbeat(0, 1)))
    for _ in range(c.UART_HEARTBEAT_LOSS_S):
        clock.advance(1)
        gw.tick()
    codes = [o.message["payload"]["code"] for o in gw.drain() if o.kind == c.KIND_EVENT]  # type: ignore[index]
    assert codes == ["DEVICE_OFFLINE"]
    gw.feed_uart(encode_frame(Heartbeat(1, 2)))
    codes = [o.message["payload"]["code"] for o in gw.drain() if o.kind == c.KIND_EVENT]  # type: ignore[index]
    assert codes == ["DEVICE_ONLINE"]


# ---------------------------------------------------------------- gw-01 と app-01（request）


def test_request_flow_in_memory() -> None:
    sim = Simulation(load_scenario("request_flow"), speed=0)
    out = sim.run_batch()
    app, gw = sim.app, sim.gw
    assert app is not None and gw is not None
    final = {
        (r.payload["op"], (r.payload.get("args") or {}).get("id")): (r.status, r.reason, r.attempts)
        for r in app.requests.values()
    }
    assert final[("set_actuator", "pump")] == ("done", None, 1)
    assert final[("set_actuator", "buzzer")] == ("rejected", "not_permitted", 1)
    assert final[("set_mode", None)] == ("failed", "gateway_timeout", 1 + c.REQUEST_MAX_RETRIES)
    assert gw.stats["duplicate_request"] == c.REQUEST_MAX_RETRIES
    for o in out:
        assert validate(o.message) == []


def test_schedule_request_id_is_deterministic() -> None:
    sim = Simulation(load_scenario("request_flow"), speed=0)
    sim.run_batch()
    assert sim.app is not None
    scheduled = next(r for r in sim.app.requests.values() if r.payload["kind"] == "schedule")
    planned = scheduled.payload["schedule"]
    from iotspec.clock import parse_ts

    assert scheduled.request_id == schedule_request_id(
        planned["id"], parse_ts(planned["planned_at"])
    )


def test_app_final_result_timeout() -> None:
    clock = VirtualClock(T0)
    app = App01(load_profile(c.DEVICE_APP_01), clock, site=c.DEFAULT_SITE)
    state = app.request({"kind": "manual", "target": "mcu-cpp-01", "op": "get_status"})
    (sent,) = [o for o in app.drain() if o.kind == c.KIND_REQUEST]
    gw, _ = _gw()
    gw.clock = clock
    gw.request_rules = [{"result": "done", "done_after_s": c.REQUEST_FINAL_RESULT_TIMEOUT_S * 2}]
    gw.handle_message(sent.topic, sent.payload())
    for out in gw.drain():
        app.handle_message(out.topic, out.payload())
    assert state.status == "accepted"
    clock.advance(c.REQUEST_FINAL_RESULT_TIMEOUT_S - 1)
    app.tick()
    assert state.status == "accepted"
    clock.advance(1)
    app.tick()
    assert (state.status, state.reason) == ("failed", "gateway_timeout")


def test_gw_request_dedup_returns_current_state() -> None:
    gw, clock = _gw(request_rules=[{"result": "done", "done_after_s": 2}])
    app = App01(load_profile(c.DEVICE_APP_01), clock, site=c.DEFAULT_SITE)
    app.request({"kind": "manual", "target": "mcu-cpp-01", "op": "get_status"})
    (sent,) = [o for o in app.drain() if o.kind == c.KIND_REQUEST]

    def statuses() -> list[str]:
        return [str(o.message["payload"]["status"]) for o in gw.drain()]  # type: ignore[index]

    gw.handle_message(sent.topic, sent.payload())
    assert statuses() == ["accepted"]
    gw.handle_message(sent.topic, sent.payload())
    assert statuses() == ["accepted"]
    clock.advance(2)
    gw.tick()
    assert statuses() == ["done"]
    gw.handle_message(sent.topic, sent.payload())
    assert statuses() == ["done"]
    clock.advance(c.REQUEST_DEDUP_TTL_S)
    gw.handle_message(sent.topic, sent.payload())  # 1時間を過ぎたら新しい要求として扱う
    assert statuses() == ["accepted"]


# ---------------------------------------------------------------- 仮想シリアル


def test_own_pty_and_device_port_exchange_raw_bytes() -> None:
    """0x00・0x0A・0x0D を含むバイト列が書き換えられずに両方向に届く（raw モード）。"""
    own = OwnPty()
    try:
        peer = DevicePort(own.peer_path)
        try:
            data = bytes(range(256))
            own.write(data)
            got = b""
            for _ in range(100):
                got += peer.read_available()
                if len(got) >= len(data):
                    break
            assert got == data
            peer.write(b"\x00\x0a\x0d\xff")
            back = b""
            for _ in range(100):
                back += own.read_available()
                if len(back) >= 4:
                    break
            assert back == b"\x00\x0a\x0d\xff"
        finally:
            peer.close()
    finally:
        own.close()
    assert own.peer_path.startswith("/dev/")
    assert not os.path.exists("/nonexistent") or True


# ---------------------------------------------------------------- シナリオ


@pytest.mark.parametrize("name", ["request_flow", "uart_link"])
def test_new_scenarios_load(name: str) -> None:
    assert load_scenario(name).name == name


def test_type_specific_options_are_checked() -> None:
    base: dict[str, Any] = {
        "name": "t",
        "start": format_ts(T0),
        "duration_s": 10,
        "devices": [{"device_id": "app-01", "type": "app_01", "uart_commands": []}],
    }
    with pytest.raises(ScenarioError) as info:
        parse_scenario(base)
    assert any(e.startswith("/devices/0/uart_commands") for e in info.value.errors)
    base["devices"] = [
        {
            "device_id": "app-01",
            "type": "app_01",
            "requests": [
                {"at_s": 1, "kind": "schedule", "target": "mcu-cpp-01", "op": "get_status"}
            ],
        }
    ]
    with pytest.raises(ScenarioError) as info:
        parse_scenario(base)
    assert any(e.startswith("/devices/0/requests/0") for e in info.value.errors)
    _ = timedelta


# ---------------------------------------------------------------- #34・#36


def test_mcu_sends_upstream_restored_when_heartbeat_returns() -> None:
    mcu, clock, _ = _mcu()
    mcu.start()
    for _ in range(c.UART_UPSTREAM_LOST_S):
        clock.advance(1)
        mcu.tick()
    assert mcu.upstream_lost
    mcu.feed(encode_frame(Heartbeat(0, 0)))
    mcu.feed(encode_frame(Heartbeat(1, 1)))
    codes = [f.code for f in mcu.sent if isinstance(f, Event)]
    assert codes[-2:] == [7, 8]  # UPSTREAM_LOST、UPSTREAM_RESTORED（1回だけ）


def test_gw_publishes_offline_state_with_bridged_by_after_uart_silence() -> None:
    from iotspec.dedup import exempt_from_seq_checks

    gw, clock = _gw()
    gw.feed_uart(encode_frame(State(0, 0x0102, 0x01020304, 1000)))
    (online,) = [o for o in gw.drain() if o.kind == c.KIND_STATE]
    assert online.message["payload"]["bridged_by"] == c.DEVICE_GW_01  # type: ignore[index]
    for _ in range(c.UART_HEARTBEAT_LOSS_S):
        clock.advance(1)
        gw.tick()
    states = [o for o in gw.drain() if o.kind == c.KIND_STATE]
    (offline,) = states
    assert offline.topic == f"iot/v1/{c.DEFAULT_SITE}/{c.DEVICE_MCU_C_01}/state"
    assert offline.retain is True
    payload = offline.message["payload"]
    assert payload["status"] == "offline" and payload["bridged_by"] == c.DEVICE_GW_01  # type: ignore[index]
    assert offline.message["boot_id"] == "01020304"
    assert offline.message["seq"] == online.message["seq"] + 1  # type: ignore[operator]
    assert validate(offline.message) == [] and exempt_from_seq_checks(offline.message)


def test_gw_turns_request_for_uart_device_into_cmd_and_reports_result() -> None:
    """UART デバイス宛ての request は CMD に変わり、ACK で request の最終結果が決まる。"""
    gw, clock = _gw()
    app = App01(load_profile(c.DEVICE_APP_01), clock, site=c.DEFAULT_SITE)
    state = app.request(
        {
            "kind": "manual",
            "target": c.DEVICE_MCU_C_01,
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 450},
            "lease_s": 30,
        }
    )
    (sent,) = [o for o in app.drain() if o.kind == c.KIND_REQUEST]
    gw.handle_message(sent.topic, sent.payload())
    for out in gw.drain():
        app.handle_message(out.topic, out.payload())
    assert state.status == "accepted"
    gw.feed_uart(encode_frame(State(0, 0x0102, 1, 1000)))
    clock.advance(1)
    gw.tick()
    (cmd,) = _cmds(gw.drain_uart())
    assert cmd.op == c.UART_OP_SET_FAN_OVERRIDE and cmd.args == (450,) and cmd.ttl_s == 30
    gw.feed_uart(encode_frame(Ack(0, cmd.cmd_ref, 1, 0, 0, 450)))
    for out in gw.drain():
        if out.kind == c.KIND_REQUEST_ACK:
            app.handle_message(out.topic, out.payload())
    assert state.status == "done"
    assert state.acks[0]["cmd_id"] == state.acks[-1]["cmd_id"]


def test_gw_does_not_send_expired_cmd_and_rejects_request_with_expired() -> None:
    """v0.2.2 #40：expires_at を過ぎた指令は UART に送らず、mcu-c-01 名義の cmd/ack を作らない。
    要求元には request/ack（rejected、reason expired）で伝える。"""
    gw, clock = _gw()
    app = App01(load_profile(c.DEVICE_APP_01), clock, site=c.DEFAULT_SITE)
    state = app.request(
        {
            "kind": "manual",
            "target": c.DEVICE_MCU_C_01,
            "op": "set_actuator",
            "args": {"id": "fan_main", "level": 450},
        }
    )
    (sent,) = [o for o in app.drain() if o.kind == c.KIND_REQUEST]
    gw.handle_message(sent.topic, sent.payload())
    for out in gw.drain():
        app.handle_message(out.topic, out.payload())
    assert state.status == "accepted"
    (record,) = gw.cmds.values()
    record.payload["expires_at"] = format_ts(clock.now())  # ちょうどで期限切れ
    gw.feed_uart(encode_frame(State(0, 0x0102, 1, 1000)))
    clock.advance(1)
    gw.tick()
    assert _cmds(gw.drain_uart()) == []
    assert record.expired and gw.stats["uart_cmd_expired"] == 1
    outs = gw.drain()
    assert not [o for o in outs if o.kind == c.KIND_CMD_ACK]
    for out in outs:
        if out.kind == c.KIND_REQUEST_ACK:
            app.handle_message(out.topic, out.payload())
    assert (state.status, state.reason) == ("rejected", "expired")
    assert all(validate(o.message) == [] for o in outs)


def test_gw_sends_unexpired_cmd() -> None:
    gw, clock = _gw()
    gw.feed_uart(encode_frame(State(0, 0x0102, 1, 1000)))
    expires = format_ts(clock.now() + timedelta(seconds=5))
    gw.submit_uart({"op": "release", "args": {"id": "fan_main"}, "expires_at": expires})
    clock.advance(1)
    gw.tick()
    assert [cmd.op for cmd in _cmds(gw.drain_uart())] == [c.UART_OP_CLEAR_OVERRIDE]


def test_gw_get_diag_keeps_history_and_publishes_nothing() -> None:
    """v0.2.2 #37：DIAG と GET_DIAG の ACK は MQTT に出さず、DIAG は履歴に残す。"""
    sim = Simulation(load_scenario("uart_link"), speed=0)
    sim.schedule()
    gw, mcu = sim.gw, sim.mcu_c
    assert gw is not None and mcu is not None
    sim.runtime.run_until(sim.scenario.start + timedelta(seconds=2), after_step=sim.pump_uart)
    gw.drain()
    record = gw.submit_internal(c.UART_OP_GET_DIAG)
    sim.runtime.run_until(sim.scenario.start + timedelta(seconds=5), after_step=sim.pump_uart)
    assert record.ack is not None and record.ack["status"] == "done"
    assert record.ack["op"] == "GET_DIAG"
    (diag,) = gw.diag_history
    assert diag["boot_id"] == f"{mcu.boot_id:08x}"
    assert diag["reset_reason"] == RESET_REASON.to_uart("power_on")
    assert gw.bridge is not None and gw.bridge.discards["not_converted"] == 2
    acks = [o.message["payload"] for o in gw.drain() if o.kind == c.KIND_CMD_ACK]
    assert record.cmd_id not in {a["cmd_id"] for a in acks}  # type: ignore[index]
    assert not [d for d in gw.diag_history if "cmd_id" in d]


def test_gw_publishes_rejected_fan_override_ack_without_applied() -> None:
    """#44（v0.2.2）：gw-01 簡易版が発行する cmd/ack も、rejected なら applied なし。"""
    gw, clock = _gw()
    gw.feed_uart(encode_frame(State(0, 0x0102, 1, 1000)))
    gw.drain()
    record = gw.submit_uart(
        {"op": "set_actuator", "args": {"id": "fan_main", "level": 300}, "lease_s": 30}
    )
    clock.advance(1)
    gw.tick()
    (cmd,) = _cmds(gw.drain_uart())
    gw.feed_uart(encode_frame(Ack(0, cmd.cmd_ref, 2, 1, 0, 0)))  # rejected unsafe
    (ack,) = [o for o in gw.drain() if o.kind == c.KIND_CMD_ACK]
    assert ack.message["payload"] == {  # type: ignore[comparison-overlap]
        "cmd_id": record.cmd_id,
        "status": "rejected",
        "limited": False,
        "reason": "unsafe",
    }


def test_gw_publishes_quality_counts_and_host_monitor_telemetry() -> None:
    """#45：品質の件数（60秒、count）と自己監視（10秒）を別々の telemetry で発行する。"""
    gw, clock = _gw()
    gw.start()
    (state,) = [o for o in gw.drain() if o.kind == c.KIND_STATE]
    channels = {ch["channel"]: ch for ch in state.message["payload"]["channels"]}  # type: ignore[index]
    assert len(channels) == 12
    assert channels["mqtt_gaps"]["unit"] == "count" and channels["mqtt_gaps"]["period_ms"] == 60000
    assert channels["cpu_temp"]["unit"] == "degC" and channels["cpu_temp"]["period_ms"] == 10000
    # 不正な request を1件数えておく（mqtt_rejected）
    gw.handle_message(gw.topic(c.KIND_REQUEST, c.DEVICE_APP_01), b"{not json")
    published: list[tuple[int, list[str]]] = []
    for second in range(1, 61):
        clock.advance(1)
        gw.tick()
        for o in gw.drain():
            if o.kind == c.KIND_TELEMETRY:
                assert validate(o.message) == [], o.message
                readings = o.message["payload"]["readings"]  # type: ignore[index]
                published.append((second, [r["channel"] for r in readings]))
                if readings[0]["unit"] == "count":
                    counts = {r["channel"]: r["value"] for r in readings}
                    assert counts["mqtt_rejected"] == 1
                    assert all(isinstance(v, int) and v >= 0 for v in counts.values())
    host = [s for s, chs in published if "cpu_temp" in chs]
    quality = [s for s, chs in published if "mqtt_gaps" in chs]
    assert host == [10, 20, 30, 40, 50, 60]
    assert quality == [60]
    assert all(len(chs) in (4, 8) for _, chs in published)  # 2種類を混ぜない
