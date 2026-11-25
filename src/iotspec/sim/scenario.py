"""シナリオ YAML（sim/scenarios/*.yaml）の読み込みと検証。

schemas/scenario.schema.json で形を検証し、加えてプロファイルとの整合
（信号のチャネル、local_rules のアクチュエーター、故障の対象）を確かめる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from iotspec.clock import parse_ts
from iotspec.constants import DEFAULT_SITE
from iotspec.profile import Profile, load_profile
from iotspec.schema import SCHEMA_DIR, json_pointer, registry

__all__ = [
    "SCENARIO_DIR",
    "DeviceConfig",
    "Fault",
    "Scenario",
    "ScenarioError",
    "load_scenario",
    "parse_scenario",
]


def _find_scenario_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "_scenarios", here.parents[2] / "sim" / "scenarios"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("sim/scenarios/ が見つからない")


SCENARIO_DIR: Path = _find_scenario_dir()
"""同梱のシナリオ（iotspec/_scenarios）か、リポジトリの sim/scenarios/。"""

_OPTION_KEYS = frozenset(
    {
        "thresholds",
        "uart_path",
        "reset_reason",
        "acks_at_s",
        "uart_commands",
        "request_rules",
        "requests",
    }
)
_TYPE_OPTIONS = {
    "mcu_cpp_01": frozenset(),
    "mcu_c_01": frozenset({"thresholds", "uart_path", "reset_reason", "acks_at_s"}),
    "gw_01_lite": frozenset({"uart_path", "uart_commands", "request_rules"}),
    "app_01": frozenset({"requests"}),
}
_DEFAULT_SPEED = 1.0
_DEFAULT_SEED = 0


class ScenarioError(ValueError):
    """不正なシナリオ。errors に "JSON Pointer: 理由" のリストを持つ。"""

    def __init__(self, source: str, errors: list[str]) -> None:
        super().__init__(f"{source}: " + "; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Fault:
    """注入する異常。at は開始時刻、until は終了時刻（一度きりの異常は None）。"""

    type: str
    device: str
    at: datetime
    until: datetime | None = None
    channel: str | None = None
    kind: str | None = None
    count: int = 1
    offset_s: float = 0.0

    def active(self, now: datetime) -> bool:
        return self.at <= now and (self.until is None or now < self.until)


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    type: str
    profile: Profile
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    local_rules: dict[str, int] = field(default_factory=dict)
    initial_mode: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    """型ごとの設定（thresholds、uart_path、reset_reason、acks_at_s、uart_commands、request_rules、
    requests）。"""


@dataclass(frozen=True)
class Scenario:
    name: str
    start: datetime
    duration_s: float
    speed: float
    seed: int
    site: str
    devices: tuple[DeviceConfig, ...]
    faults: tuple[Fault, ...]
    description: str = ""

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)

    def faults_for(self, device_id: str) -> tuple[Fault, ...]:
        return tuple(f for f in self.faults if f.device == device_id)


@cache
def _validator() -> Draft202012Validator:
    contents = json.loads((SCHEMA_DIR / "scenario.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(contents)
    return Draft202012Validator(contents, registry=registry())


def parse_scenario(data: Any, source: str = "<scenario>") -> Scenario:
    errors = sorted(f"{json_pointer(e)}: {e.message}" for e in _validator().iter_errors(data))
    if errors:
        raise ScenarioError(source, errors)
    start = parse_ts(data["start"])
    problems: list[str] = []
    devices: list[DeviceConfig] = []
    for i, raw in enumerate(data["devices"]):
        profile = load_profile(raw["device_id"])
        channels = {c.channel for c in profile.channels}
        for ch in raw.get("signals", {}):
            if ch not in channels:
                problems.append(f"/devices/{i}/signals/{ch}: プロファイルにないチャネル")
        for missing in sorted(channels - set(raw.get("signals", {}))):
            problems.append(f"/devices/{i}/signals: {missing} の信号がない")
        for act in raw.get("local_rules", {}):
            if profile.actuator(act) is None:
                problems.append(
                    f"/devices/{i}/local_rules/{act}: プロファイルにないアクチュエーター"
                )
        for key in sorted(_OPTION_KEYS & set(raw) - _TYPE_OPTIONS[raw["type"]]):
            problems.append(f"/devices/{i}/{key}: {raw['type']} では使えない")
        mode = raw.get("initial_mode")
        if mode is not None and mode not in profile.modes:
            problems.append(f"/devices/{i}/initial_mode: プロファイルの modes にない {mode!r}")
        devices.append(
            DeviceConfig(
                device_id=raw["device_id"],
                type=raw["type"],
                profile=profile,
                signals=dict(raw.get("signals", {})),
                local_rules=dict(raw.get("local_rules", {})),
                initial_mode=mode,
                options={k: v for k, v in raw.items() if k in _OPTION_KEYS},
            )
        )
    by_id = {d.device_id: d for d in devices}
    if len(by_id) != len(devices):
        problems.append("/devices: device_id が重複している")
    faults: list[Fault] = []
    for i, raw in enumerate(data.get("faults", [])):
        device = by_id.get(raw["device"])
        if device is None:
            problems.append(f"/faults/{i}/device: devices にない {raw['device']!r}")
        channel = raw.get("channel")
        if (
            device is not None
            and channel is not None
            and channel not in {c.channel for c in device.profile.channels}
        ):
            problems.append(f"/faults/{i}/channel: プロファイルにないチャネル {channel!r}")
        at = start + timedelta(seconds=raw["at_s"])
        duration = raw.get("duration_s")
        faults.append(
            Fault(
                type=raw["type"],
                device=raw["device"],
                at=at,
                until=None if duration is None else at + timedelta(seconds=duration),
                channel=channel,
                kind=raw.get("kind", "telemetry"),
                count=raw.get("count", 1),
                offset_s=raw.get("offset_s", 0.0),
            )
        )
    if problems:
        raise ScenarioError(source, problems)
    return Scenario(
        name=data["name"],
        description=data.get("description", ""),
        start=start,
        duration_s=float(data["duration_s"]),
        speed=float(data.get("speed", _DEFAULT_SPEED)),
        seed=int(data.get("seed", _DEFAULT_SEED)),
        site=data.get("site", DEFAULT_SITE),
        devices=tuple(devices),
        faults=tuple(faults),
    )


def load_scenario(name_or_path: str | Path) -> Scenario:
    """シナリオ名（sim/scenarios/{name}.yaml）またはファイルのパスから読み込む。"""
    path = Path(name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = SCENARIO_DIR / f"{name_or_path}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_scenario(data, str(path))
