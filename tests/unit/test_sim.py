"""Phase 4：シミュレータ核と mcu-cpp-01 の代役のテスト（ブローカー不要）。

時刻は VirtualClock で進め、time.sleep は使わない。
"""

from __future__ import annotations

import itertools
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from iotspec import constants as c
from iotspec.clock import VirtualClock, format_ts, parse_ts
from iotspec.dedup import SeqDedupWindow, exempt_from_seq_checks
from iotspec.envelope import EnvelopeBuilder, SeqCounter
from iotspec.profile import load_profile
from iotspec.schema import validate
from iotspec.sim.devices import Outgoing
from iotspec.sim.devices.mcu_cpp_01 import (
    FAN_INTERLOCK_CHANNEL,
    FAN_INTERLOCK_RELEASE_HOLD_S,
    FAN_INTERLOCK_RELEASE_TEMP_C,
    FAN_INTERLOCK_TEMP_C,
    PUMP_RESTART_DELAY_S,
    CmdDedup,
    McuCpp01,
)
from iotspec.sim.ratelimit import RateLimiter
from iotspec.sim.runtime import Runtime
from iotspec.sim.scenario import ScenarioError, load_scenario, parse_scenario
from iotspec.sim.signals import Constant, SignalSet, build_signal
from iotspec.sim.simulation import Simulation

T0 = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
PROFILE = load_profile(c.DEVICE_MCU_CPP_01)
PUMP = next(a for a in PROFILE.actuators if a.max_on_s is not None)
assert PUMP.max_on_s is not None
PUMP_MAX_ON_S = PUMP.max_on_s

# ---------------------------------------------------------------- 準備


def _device(
    temp: float = 25.0, *, local_rules: dict[str, int] | None = None
) -> tuple[McuCpp01, VirtualClock]:
    clock = VirtualClock(T0)
    signals = SignalSet(
        {
            ch.channel: Constant(temp if ch.channel == FAN_INTERLOCK_CHANNEL else 50.0)
            for ch in PROFILE.channels
        },
        seed=1,
        device_id=PROFILE.device_id,
    )
    device = McuCpp01(PROFILE, clock, signals, local_rules=local_rules, boot_id="0000c0de")
    device.start()
    device.drain()
    return device, clock


_gw = EnvelopeBuilder(c.DEVICE_GW_01, VirtualClock(T0), SeqCounter("00000001"))
_cmd_counter = 0


def _cmd(op: str, args: dict[str, Any] | None = None, **extra: Any) -> tuple[str, bytes]:
    global _cmd_counter
    _cmd_counter += 1
    payload: dict[str, Any] = {
        "cmd_id": f"01JD3R0A5K7M2N4P6R8T{_cmd_counter:06d}",
        "op": op,
        "priority": "auto",
    }
    if args is not None:
        payload["args"] = args
    payload.update(extra)
    message = _gw.build(c.KIND_CMD, payload).to_dict()
    assert validate(message) == [], validate(message)
    return payload["cmd_id"], json.dumps(message).encode()


def _send_cmd(
    device: McuCpp01, op: str, args: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    _, raw = _cmd(op, args, **extra)
    device.handle_message(device.cmd_topic, raw)
    return _last_ack(device)


def _last_ack(device: McuCpp01) -> dict[str, Any]:
    acks = [o for o in device.drain() if o.kind == c.KIND_CMD_ACK]
    assert acks, "ack が出ていない"
    message = acks[-1].message
    assert validate(message) == []
    payload = message["payload"]
    assert isinstance(payload, dict)
    return payload


def _owner(device: McuCpp01, actuator: str) -> tuple[int, str]:
    return device.levels[actuator], device.owners[actuator]


# ---------------------------------------------------------------- signals


def test_signals_are_reproducible_with_seed() -> None:
    specs = {
        "temp_air": {"kind": "diurnal", "mean": 24, "amplitude": 6, "peak_hour": 14, "noise": 0.5},
        "lux": {
            "kind": "daylight",
            "max": 20000,
            "sunrise_hour": 6,
            "sunset_hour": 18,
            "noise": 50,
        },
        "soil": {
            "kind": "soil",
            "start": 40,
            "decay_per_hour": 1,
            "refill_per_pump_s": 0.2,
            "noise": 0.1,
        },
    }

    def series(seed: int) -> list[dict[str, float]]:
        signals = SignalSet({k: build_signal(v) for k, v in specs.items()}, seed, "mcu-cpp-01")
        return [
            signals.sample(T0 + timedelta(minutes=10 * i), 600, {"pump": 0}) for i in range(144)
        ]

    assert series(42) == series(42)
    assert series(42) != series(43)


def test_diurnal_and_daylight_shapes() -> None:
    temp = build_signal({"kind": "diurnal", "mean": 24, "amplitude": 6, "peak_hour": 14})
    lux = build_signal({"kind": "daylight", "max": 20000, "sunrise_hour": 6, "sunset_hour": 18})
    rng = __import__("random").Random(0)
    at = lambda h: T0 + timedelta(hours=h)  # noqa: E731
    assert temp.sample(at(14), 0, {}, rng) == pytest.approx(30.0)
    assert temp.sample(at(2), 0, {}, rng) == pytest.approx(18.0)
    assert lux.sample(at(12), 0, {}, rng) == pytest.approx(20000.0)
    assert lux.sample(at(3), 0, {}, rng) == 0.0
    assert lux.sample(at(20), 0, {}, rng) == 0.0


def test_soil_drops_and_rises_with_pump() -> None:
    soil = build_signal(
        {"kind": "soil", "start": 40, "decay_per_hour": 3.6, "refill_per_pump_s": 0.5}
    )
    rng = __import__("random").Random(0)
    assert soil.sample(T0, 1000, {"pump": 0}, rng) == pytest.approx(39.0)
    assert soil.sample(T0, 10, {"pump": 1000}, rng) == pytest.approx(39.0 - 0.01 + 5.0)


# ---------------------------------------------------------------- scenario


@pytest.mark.parametrize("name", ["normal_day", "sensor_fault", "duplicate_and_gap"])
def test_bundled_scenarios_load(name: str) -> None:
    scenario = load_scenario(name)
    assert scenario.name == name
    assert scenario.devices[0].device_id == c.DEVICE_MCU_CPP_01


def _scenario_data() -> dict[str, Any]:
    return {
        "name": "t",
        "start": "2026-11-20T00:00:00.000Z",
        "duration_s": 60,
        "devices": [
            {
                "device_id": "mcu-cpp-01",
                "type": "mcu_cpp_01",
                "signals": {
                    ch.channel: {"kind": "constant", "value": 1} for ch in PROFILE.channels
                },
            }
        ],
        "faults": [
            {
                "type": "sensor_fault",
                "device": "mcu-cpp-01",
                "channel": "soil",
                "at_s": 1,
                "duration_s": 5,
            }
        ],
    }


def _broken(path: list[Any], value: Any) -> dict[str, Any]:
    data = _scenario_data()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        (_broken(["start"], "2026-11-20 00:00"), "/start"),
        (_broken(["duration_s"], 0), "/duration_s"),
        (_broken(["devices", 0, "type"], "mcu_c_99"), "/devices/0/type"),
        (
            _broken(["devices", 0, "signals", "lux"], {"kind": "daylight", "max": 1}),
            "/devices/0/signals/lux",
        ),
        (
            _broken(["devices", 0, "signals", "co2"], {"kind": "constant", "value": 1}),
            "/devices/0/signals/co2",
        ),
        (_broken(["devices", 0, "local_rules"], {"heater": 10}), "/devices/0/local_rules/heater"),
        (_broken(["devices", 0, "initial_mode"], "turbo"), "/devices/0/initial_mode"),
        (_broken(["faults", 0, "channel"], "co2"), "/faults/0/channel"),
        (_broken(["faults", 0, "device"], "mcu-c-01"), "/faults/0/device"),
        (_broken(["faults", 0, "type"], "explode"), "/faults/0/type"),
        (
            _broken(["faults", 0], {"type": "silence", "device": "mcu-cpp-01", "at_s": 1}),
            "/faults/0",
        ),
    ],
)
def test_invalid_scenarios_are_rejected(data: dict[str, Any], pointer: str) -> None:
    parse_scenario(_scenario_data())
    with pytest.raises(ScenarioError) as info:
        parse_scenario(data)
    assert any(
        e.startswith(f"{pointer}: ") or e.startswith(f"{pointer}/") for e in info.value.errors
    ), info.value.errors


# ---------------------------------------------------------------- runtime


def test_runtime_runs_in_time_order_without_waiting() -> None:
    clock = VirtualClock(T0)
    runtime = Runtime(clock, speed=0)
    seen: list[tuple[str, datetime]] = []
    runtime.every(1.0, lambda: seen.append(("tick", clock.now())), first=T0 + timedelta(seconds=1))
    runtime.at(T0 + timedelta(seconds=2.5), lambda: seen.append(("once", clock.now())))
    count = runtime.run_until(T0 + timedelta(seconds=3))
    assert count == 4
    assert [name for name, _ in seen] == ["tick", "tick", "once", "tick"]
    assert seen[2][1] == T0 + timedelta(seconds=2.5)
    assert clock.now() == T0 + timedelta(seconds=3)


async def test_runtime_async_batch_mode_does_not_sleep() -> None:
    clock = VirtualClock(T0)
    runtime = Runtime(clock, speed=0)
    ticks: list[datetime] = []
    runtime.every(3600.0, lambda: ticks.append(clock.now()), first=T0)
    assert await runtime.run_until_async(T0 + timedelta(days=30)) == 30 * 24 + 1


# ---------------------------------------------------------------- mcu-cpp-01：起動と送信


def test_start_publishes_state_and_boot_with_retain_and_qos() -> None:
    clock = VirtualClock(T0)
    signals = SignalSet(
        {ch.channel: Constant(20.0) for ch in PROFILE.channels}, 1, PROFILE.device_id
    )
    device = McuCpp01(PROFILE, clock, signals)
    will = device.will()
    assert will.retain and will.message["payload"]["status"] == "offline"  # type: ignore[index]
    device.start()
    out = device.drain()
    assert [o.kind for o in out] == [c.KIND_STATE, c.KIND_EVENT]
    state, boot = out
    assert state.retain is True and state.qos == c.QOS_BY_KIND[c.KIND_STATE]
    assert state.message["payload"]["status"] == "online"  # type: ignore[index]
    assert state.message["payload"]["mode"] in PROFILE.modes  # type: ignore[index]
    assert boot.message["payload"]["code"] == "BOOT"  # type: ignore[index]
    for o in [will, *out]:
        assert validate(o.message) == []
        assert o.topic == f"iot/v1/{c.DEFAULT_SITE}/{c.DEVICE_MCU_CPP_01}/{o.kind}"


def test_telemetry_contains_owner_and_conforms() -> None:
    device, clock = _device(local_rules={"fan_main": 200})
    clock.advance(1)
    device.tick()
    (tel,) = device.drain()
    assert validate(tel.message) == []
    assert tel.qos == c.QOS_BY_KIND[c.KIND_TELEMETRY] and not tel.retain
    owners = {a["id"]: (a["level"], a["owner"]) for a in tel.message["payload"]["actuators"]}  # type: ignore[index]
    assert owners["fan_main"] == (200, "local")
    assert owners["pump"] == (0, "none")


# ---------------------------------------------------------------- リース


def test_lease_expiry_returns_owner_to_local() -> None:
    device, clock = _device(local_rules={"fan_main": 200})
    ack = _send_cmd(
        device, "set_actuator", {"id": "fan_main", "level": 800}, lease_s=c.LEASE_DEFAULT_S
    )
    assert ack == {
        "cmd_id": ack["cmd_id"],
        "status": "done",
        "limited": False,
        "applied": {"level": 800},
        "reason": None,
    }
    assert _owner(device, "fan_main") == (800, "auto")
    clock.advance(c.LEASE_DEFAULT_S - 0.001)
    device.tick()
    assert _owner(device, "fan_main") == (800, "auto")
    clock.advance(0.001)  # 受信時刻＋lease_s ちょうどで切れる（#15）
    device.tick()
    assert _owner(device, "fan_main") == (200, "local")


def test_lease_default_and_owner_none_without_local_rule() -> None:
    device, clock = _device()
    _send_cmd(device, "set_actuator", {"id": "led_grow", "level": 500}, priority="schedule")
    assert _owner(device, "led_grow") == (500, "schedule")
    clock.advance(c.LEASE_DEFAULT_S)
    device.tick()
    assert _owner(device, "led_grow") == (0, "none")


def test_renewal_with_new_cmd_id_replaces_lease() -> None:
    device, clock = _device(local_rules={"fan_main": 100})
    _send_cmd(device, "set_actuator", {"id": "fan_main", "level": 600}, lease_s=c.LEASE_DEFAULT_S)
    clock.advance(c.LEASE_DEFAULT_S / c.LEASE_RENEW_DIVISOR)
    _send_cmd(device, "set_actuator", {"id": "fan_main", "level": 700}, lease_s=c.LEASE_DEFAULT_S)
    clock.advance(c.LEASE_DEFAULT_S - 1)
    device.tick()
    assert _owner(device, "fan_main") == (700, "auto")


def test_release_returns_to_local() -> None:
    device, _ = _device(local_rules={"fan_main": 100})
    _send_cmd(device, "set_actuator", {"id": "fan_main", "level": 900})
    ack = _send_cmd(device, "release", {"id": "fan_main"})
    assert ack["status"] == "done" and "applied" not in ack
    assert _owner(device, "fan_main") == (100, "local")


def test_expired_command_is_rejected() -> None:
    device, clock = _device()
    clock.advance(10)
    ack = _send_cmd(
        device,
        "set_actuator",
        {"id": "fan_main", "level": 900},
        expires_at=format_ts(clock.now() - timedelta(seconds=1)),
    )
    assert (ack["status"], ack["reason"]) == ("rejected", "expired")
    assert _owner(device, "fan_main") == (0, "none")


# ---------------------------------------------------------------- インターロック


def test_fan_interlock_limits_and_owner_interlock() -> None:
    device, clock = _device(temp=FAN_INTERLOCK_TEMP_C)
    ack = _send_cmd(device, "set_actuator", {"id": "fan_main", "level": 300})
    assert ack["limited"] is True
    assert ack["applied"] == {"level": 1000}
    assert _owner(device, "fan_main") == (1000, "interlock")
    events = [o.message["payload"]["code"] for o in device.drain() if o.kind == c.KIND_EVENT]  # type: ignore[index]
    clock.advance(1)
    device.tick()
    tel = next(o for o in device.drain() if o.kind == c.KIND_TELEMETRY)
    fan = next(a for a in tel.message["payload"]["actuators"] if a["id"] == "fan_main")  # type: ignore[index]
    assert fan == {"id": "fan_main", "level": 1000, "owner": "interlock"}
    assert "INTERLOCK_ACTIVE" in events or device._interlocked == {"fan_main"}


def test_fan_below_threshold_is_not_limited() -> None:
    device, _ = _device(temp=FAN_INTERLOCK_TEMP_C - 0.01)
    ack = _send_cmd(device, "set_actuator", {"id": "fan_main", "level": 300})
    assert ack["limited"] is False
    assert _owner(device, "fan_main") == (300, "auto")


def test_pump_is_stopped_after_max_on_s_and_ack_is_limited() -> None:
    device, clock = _device()
    ack = _send_cmd(device, "set_actuator", {"id": "pump", "level": 1000}, lease_s=c.LEASE_MAX_S)
    # lease_s が連続運転の上限を超える：出力は途中で止めるので limited（第4章の例）
    assert ack["limited"] is True and ack["applied"] == {"level": 1000}
    for _ in range(int(PUMP_MAX_ON_S) - 1):
        clock.advance(1)
        device.tick()
    assert _owner(device, "pump") == (1000, "auto")
    clock.advance(1)
    device.tick()
    assert _owner(device, "pump") == (0, "interlock")
    codes = [o.message["payload"]["code"] for o in device.drain() if o.kind == c.KIND_EVENT]  # type: ignore[index]
    assert codes == ["INTERLOCK_ACTIVE"]
    # 更新（新しい cmd_id）が来てもインターロックは外れない
    renew = _send_cmd(device, "set_actuator", {"id": "pump", "level": 1000}, lease_s=c.LEASE_MAX_S)
    assert renew["limited"] is True and renew["applied"] == {"level": 0}
    # 要求が0に戻っても、停止から PUMP_RESTART_DELAY_S 経つまでは解除しない（#27）
    stopped_at = clock.now()
    _send_cmd(device, "release", {"id": "pump"})
    assert _owner(device, "pump") == (0, "interlock")
    clock.set(stopped_at + timedelta(seconds=PUMP_RESTART_DELAY_S))
    device.tick()
    assert _owner(device, "pump") == (0, "none")
    assert "pump" not in device._interlocked


def test_short_pump_lease_is_not_limited() -> None:
    device, _ = _device()
    ack = _send_cmd(
        device, "set_actuator", {"id": "pump", "level": 500}, lease_s=int(PUMP_MAX_ON_S)
    )
    assert ack["limited"] is False


# ---------------------------------------------------------------- 指令の種類と重複


def test_duplicate_cmd_id_returns_same_ack() -> None:
    device, clock = _device(temp=FAN_INTERLOCK_TEMP_C + 1)
    _, raw = _cmd("set_actuator", {"id": "fan_main", "level": 100})
    device.handle_message(device.cmd_topic, raw)
    first = _last_ack(device)
    clock.advance(30)
    device.handle_message(device.cmd_topic, raw)
    second = _last_ack(device)
    assert first == second
    assert device.stats["duplicate_cmd"] == 1


def test_cmd_dedup_capacity_and_ttl() -> None:
    dedup = CmdDedup()
    for i in range(c.CMD_DEDUP_CAPACITY + 1):
        dedup.put(f"id{i}", {"n": i}, T0)
    assert len(dedup) == c.CMD_DEDUP_CAPACITY
    assert dedup.get("id0", T0) is None
    assert dedup.get("id1", T0) == {"n": 1}
    assert dedup.get("id1", T0 + timedelta(seconds=c.CMD_DEDUP_TTL_S - 1)) == {"n": 1}
    assert dedup.get("id1", T0 + timedelta(seconds=c.CMD_DEDUP_TTL_S)) is None


def test_set_param_is_rejected_with_unknown_op() -> None:
    device, _ = _device()
    ack = _send_cmd(device, "set_param", {"name": "x", "value": 1})
    assert (ack["status"], ack["reason"]) == ("rejected", "unknown_op")


def test_set_mode_and_get_status() -> None:
    device, _ = _device()
    _, raw = _cmd("set_mode", {"mode": "power_save"})
    device.handle_message(device.cmd_topic, raw)
    out = device.drain()
    kinds = [o.kind for o in out]
    assert kinds == [c.KIND_EVENT, c.KIND_STATE, c.KIND_CMD_ACK]
    assert out[1].message["payload"]["mode"] == "power_save"  # type: ignore[index]
    assert _send_cmd(device, "set_mode", {"mode": "turbo"})["reason"] == "invalid_arg"
    _, raw = _cmd("get_status")
    device.handle_message(device.cmd_topic, raw)
    assert [o.kind for o in device.drain()] == [c.KIND_STATE, c.KIND_CMD_ACK]


def test_unknown_target_and_invalid_messages() -> None:
    device, _ = _device()
    assert (
        _send_cmd(device, "set_actuator", {"id": "heater", "level": 1})["reason"]
        == "unknown_target"
    )
    assert _send_cmd(device, "release", {"id": "heater"})["reason"] == "unknown_target"
    device.handle_message(device.cmd_topic, b"{not json")
    device.handle_message(device.cmd_topic, json.dumps({"schema": "iot.v1.cmd"}).encode())
    device.handle_message("iot/v1/home/mcu-cpp-02/cmd", b"{}")
    assert device.drain() == []
    assert device.stats["invalid_cmd"] == 2


# ---------------------------------------------------------------- 送信の上限


def test_rate_limiter_burst_and_average() -> None:
    clock = VirtualClock(T0)
    limiter = RateLimiter(clock)
    assert sum(limiter.try_acquire() for _ in range(100)) == c.PUBLISH_RATE_BURST_PER_S
    clock.advance(1)
    assert sum(limiter.try_acquire() for _ in range(100)) == c.PUBLISH_RATE_AVG_PER_S


def test_rate_limiter_sliding_window_never_exceeds_burst() -> None:
    clock = VirtualClock(T0)
    limiter = RateLimiter(clock)
    sent: list[datetime] = []
    for _ in range(2000):
        if limiter.try_acquire():
            sent.append(clock.now())
        clock.advance(0.01)
    for i, t in enumerate(sent):
        window = [s for s in sent[i:] if s - t < timedelta(seconds=1)]
        assert len(window) <= c.PUBLISH_RATE_BURST_PER_S
    span = (sent[-1] - sent[0]).total_seconds()
    assert len(sent) <= c.PUBLISH_RATE_BURST_PER_S + c.PUBLISH_RATE_AVG_PER_S * span + 1


def test_device_respects_publish_limit_per_kind() -> None:
    device, clock = _device()
    for _ in range(200):
        device.handle_message(device.cmd_topic, _cmd("get_status")[1])
    out = device.drain()
    per_kind = Counter(o.kind for o in out)
    assert per_kind[c.KIND_CMD_ACK] == c.PUBLISH_RATE_BURST_PER_S
    assert per_kind[c.KIND_STATE] == c.PUBLISH_RATE_BURST_PER_S - 1  # 起動時の state を1件使った
    assert device.stats[f"rate_limited:{c.KIND_CMD_ACK}"] == 200 - c.PUBLISH_RATE_BURST_PER_S
    clock.advance(1)
    device.tick()
    assert [o.kind for o in device.drain()] == [c.KIND_TELEMETRY]


# ---------------------------------------------------------------- シナリオ全体


def _seq_by_kind(out: list[Outgoing]) -> dict[str, list[int]]:
    seqs: dict[str, list[int]] = defaultdict(list)
    for o in out:
        seqs[o.kind].append(int(o.message["seq"]))  # type: ignore[call-overload]
    return seqs


def test_normal_day_batch_conforms_and_seq_is_consecutive() -> None:
    sim = Simulation(load_scenario("normal_day"), speed=0)
    sim.scenario = sim.scenario.__class__(**{**sim.scenario.__dict__, "duration_s": 3600})
    out = sim.run_batch()
    assert len(out) > 3600
    boot_ids = {o.message["boot_id"] for o in out}
    assert len(boot_ids) == 1
    for o in out:
        assert validate(o.message) == []
    for kind, seqs in _seq_by_kind(out).items():
        assert seqs == list(range(len(seqs))), kind


def test_normal_day_is_reproducible() -> None:
    def run() -> list[Any]:
        scenario = load_scenario("normal_day")
        sim = Simulation(scenario.__class__(**{**scenario.__dict__, "duration_s": 120}), speed=0)
        return [o.message["payload"] for o in sim.run_batch()]

    assert run() == run()


def test_fault_scenarios_produce_expected_anomalies() -> None:
    out = Simulation(load_scenario("duplicate_and_gap"), speed=0).run_batch()
    tel = _seq_by_kind(out)[c.KIND_TELEMETRY]
    gaps = [b - a for a, b in itertools.pairwise(tel) if b - a > 1]
    dups = [b for a, b in itertools.pairwise(tel) if a == b]
    assert 3 + 1 in gaps  # seq_gap 3（欠番3つ）
    assert len(dups) == 2  # duplicate 2（同じ電文を2回余分に送る）
    assert 30 + 1 in gaps  # silence 30秒（その間の電文は届かない）
    states = [o for o in out if o.kind == c.KIND_STATE]
    assert len(states) == 2 and states[0].message == states[1].message

    out = Simulation(load_scenario("sensor_fault"), speed=0).run_batch()
    codes = [o.message["payload"]["code"] for o in out if o.kind == c.KIND_EVENT]  # type: ignore[index]
    assert codes.count("SENSOR_FAULT") == 1 and codes.count("SENSOR_RECOVERED") == 1
    assert "INTERLOCK_ACTIVE" in codes
    faulty = [
        r
        for o in out
        if o.kind == c.KIND_TELEMETRY
        for r in o.message["payload"]["readings"]  # type: ignore[index]
        if r["quality"] == "fault"
    ]
    assert {r["channel"] for r in faulty} == {"soil"}
    skew = next(f for f in load_scenario("sensor_fault").faults if f.type == "clock_skew")
    ts = [parse_ts(str(o.message["ts"])) for o in out if o.kind == c.KIND_TELEMETRY]
    jumps = [(b - a).total_seconds() for a, b in itertools.pairwise(ts)]
    assert max(jumps) == pytest.approx(skew.offset_s + 1)  # 時刻ずれの始まりで ts が先へ飛ぶ
    assert min(jumps) == pytest.approx(-skew.offset_s + 1)  # 終わりで戻る
    for o in out:
        assert validate(o.message) == []


# ---------------------------------------------------------------- #27・#29


class _Scripted:
    """値を順に返す信号（テスト用）。"""

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)

    def sample(self, t: datetime, dt_s: float, inputs: Any, rng: Any) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _scripted_device(temps: list[float]) -> tuple[McuCpp01, VirtualClock]:
    clock = VirtualClock(T0)
    signals = SignalSet(
        {
            ch.channel: (
                _Scripted(temps) if ch.channel == FAN_INTERLOCK_CHANNEL else Constant(50.0)
            )
            for ch in PROFILE.channels
        },
        seed=1,
        device_id=PROFILE.device_id,
    )
    device = McuCpp01(PROFILE, clock, signals, boot_id="0000c0de")
    device.start()
    device.drain()
    return device, clock


def _interlock_events(device: McuCpp01) -> list[str]:
    return [
        str(o.message["payload"]["code"])  # type: ignore[index]
        for o in device.drain()
        if o.kind == c.KIND_EVENT and str(o.message["payload"]["code"]).startswith("INTERLOCK")  # type: ignore[index]
    ]


def test_fan_interlock_does_not_flap_with_noise_at_threshold() -> None:
    """45℃付近のノイズでは INTERLOCK_ACTIVE／RELEASED を繰り返さない（#27）。"""
    noisy = [FAN_INTERLOCK_TEMP_C + (0.1 if i % 2 else -0.1) for i in range(120)]
    device, clock = _scripted_device([25.0, *noisy])
    events: list[str] = []
    for _ in range(len(noisy)):
        clock.advance(1)
        device.tick()
        events += _interlock_events(device)
    assert events == ["INTERLOCK_ACTIVE"]
    assert _owner(device, "fan_main") == (1000, "interlock")


def test_fan_interlock_releases_after_cool_hold() -> None:
    hold = int(FAN_INTERLOCK_RELEASE_HOLD_S)
    below = FAN_INTERLOCK_RELEASE_TEMP_C - 0.1
    between = FAN_INTERLOCK_RELEASE_TEMP_C + 0.5
    temps = [25.0, FAN_INTERLOCK_TEMP_C, *[below] * (hold - 1), between, *[below] * (hold + 1)]
    device, clock = _scripted_device(temps)
    released_at: list[int] = []
    for step in range(1, len(temps)):
        clock.advance(1)
        device.tick()
        if "INTERLOCK_RELEASED" in _interlock_events(device):
            released_at.append(step)
    # 43℃未満が途中で途切れたら数え直し、43℃未満が10秒続いた時点で解除する
    assert released_at == [1 + (hold - 1) + 1 + hold + 1]
    assert _owner(device, "fan_main") == (0, "none")


def test_pump_restarts_only_after_zero_and_delay() -> None:
    device, clock = _device()
    _send_cmd(device, "set_actuator", {"id": "pump", "level": 1000}, lease_s=c.LEASE_MAX_S)
    for _ in range(int(PUMP_MAX_ON_S)):
        clock.advance(1)
        device.tick()
    stopped_at = clock.now()
    assert _owner(device, "pump") == (0, "interlock")
    # 60秒経っても、要求が一度も0に戻っていなければ再開しない
    clock.set(stopped_at + timedelta(seconds=PUMP_RESTART_DELAY_S + 5))
    _send_cmd(device, "set_actuator", {"id": "pump", "level": 1000}, lease_s=c.LEASE_MAX_S)
    assert _owner(device, "pump") == (0, "interlock")
    # 要求が0に戻り（release）、停止から60秒以上経っていれば、次の指令で再開する
    _send_cmd(device, "release", {"id": "pump"})
    assert _owner(device, "pump") == (0, "none")
    ack = _send_cmd(
        device, "set_actuator", {"id": "pump", "level": 800}, lease_s=int(PUMP_MAX_ON_S)
    )
    assert ack["applied"] == {"level": 800}
    assert _owner(device, "pump") == (800, "auto")


def test_lwt_does_not_advance_state_seq_and_is_always_accepted() -> None:
    """#29：LWT は seq を進めない。受信側は offline の state を seq の判定から外す。"""
    clock = VirtualClock(T0)
    signals = SignalSet(
        {ch.channel: Constant(20.0) for ch in PROFILE.channels}, 1, PROFILE.device_id
    )
    device = McuCpp01(PROFILE, clock, signals)
    will = device.will()
    device.start()
    online = next(o for o in device.drain() if o.kind == c.KIND_STATE)
    assert will.message["seq"] == online.message["seq"] == 0
    assert validate(will.message) == []
    assert exempt_from_seq_checks(will.message)
    assert not exempt_from_seq_checks(online.message)

    window = SeqDedupWindow()
    assert window.accept(online.message, c.KIND_STATE) is True
    assert window.accept(online.message, c.KIND_STATE) is False  # 通常の重複は捨てる
    # LWT は online と同じ seq でも、何度来ても受け入れ、記憶もしない
    assert window.accept(will.message, c.KIND_STATE) is True
    assert window.accept(will.message, c.KIND_STATE) is True
    assert window.remembered(device.device_id, c.KIND_STATE) == 1
    shutdown = device.shutdown()
    assert shutdown.message["seq"] == 1 and exempt_from_seq_checks(shutdown.message)


def test_telemetry_includes_each_channel_at_its_period() -> None:
    """#26：telemetry には周期が来たチャネルだけを入れる（例：lux 1000ms、soil 10000ms）。"""
    device, clock = _device()
    periods = {ch.channel: ch.period_ms for ch in PROFILE.channels}
    seen: dict[str, list[float]] = defaultdict(list)
    for step in range(1, 21):
        clock.advance(1)
        device.tick()
        (tel,) = [o for o in device.drain() if o.kind == c.KIND_TELEMETRY]
        for r in tel.message["payload"]["readings"]:  # type: ignore[index]
            seen[r["channel"]].append(step)
    for channel, period_ms in periods.items():
        step = period_ms / 1000
        expected = [1 + i * step for i in range(int(20 // step))]
        assert seen[channel] == expected, channel
