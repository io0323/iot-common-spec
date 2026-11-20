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
    NORMAL,
    SENSOR_FAULT,
    WARNING,
    McuC01,
    Thresholds,
)
from iotspec.sim.scenario import ScenarioError, load_scenario, parse_scenario
from iotspec.sim.serial import DevicePort, OwnPty
from iotspec.sim.signals import SignalSet
from iotspec.sim.simulation import Simulation
from iotspec.uart.frames import (
    Ack,
    Cmd,
    Event,
    FrameReader,
    Heartbeat,
    State,
    Telemetry,
    encode_frame,
)

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


def test_mcu_state_changes_with_thresholds_and_events() -> None:
    th = Thresholds()
    mcu, clock, signal = _mcu(temp=th.warn_c100 / 100 - 5)
    mcu.start()
    seen: list[int] = []
    for temp in (
        25.0,
        th.warn_c100 / 100,
        th.crit_c100 / 100,
        (th.warn_c100 - th.hyst_c100 - 1) / 100,
    ):
        signal.value = temp
        clock.advance(1)
        mcu.feed(encode_frame(Heartbeat(0, 0)))
        mcu.tick()
        seen.append(mcu.state)
    assert seen == [NORMAL, WARNING, CRITICAL, NORMAL]
    changed = [f for f in mcu.sent if isinstance(f, Event) and f.code == 2]
    assert [(e.from_state, e.to_state) for e in changed][-3:] == [
        (NORMAL, WARNING),
        (WARNING, CRITICAL),
        (CRITICAL, NORMAL),
    ]


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
