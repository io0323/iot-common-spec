"""デバイスプロファイル（profiles/{device_id}.yaml、v0.2.1 #16）の読み込みと検証。

プロファイルは schemas/profile.schema.json で検証し、加えて commands の target が
actuators にあることなど、Schema で書けない整合を確かめる。不正なら ProfileError（理由つき）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from iotspec.constants import UART_OP_NAMES
from iotspec.schema import SCHEMA_DIR, json_pointer, registry
from iotspec.uart.frames import CMD_ARG_NAMES

__all__ = [
    "PROFILE_DIR",
    "ActuatorSpec",
    "ChannelSpec",
    "CommandSpec",
    "Profile",
    "ProfileError",
    "load_profile",
    "parse_profile",
]

_UART_OP_BY_NAME = {name: code for code, name in UART_OP_NAMES.items()}


def _find_profile_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "_profiles", here.parents[1] / "profiles"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("profiles/ が見つからない")


PROFILE_DIR: Path = _find_profile_dir()
"""同梱のプロファイルを置いたディレクトリ。"""


class ProfileError(ValueError):
    """不正なプロファイル。errors に "JSON Pointer: 理由" のリストを持つ。"""

    def __init__(self, source: str, errors: list[str]) -> None:
        super().__init__(f"{source}: " + "; ".join(errors))
        self.source = source
        self.errors = errors


@dataclass(frozen=True)
class ChannelSpec:
    channel: str
    unit: str
    period_ms: int


@dataclass(frozen=True)
class ActuatorSpec:
    id: str
    min: float
    max: float
    max_on_s: float | None = None


@dataclass(frozen=True)
class CommandSpec:
    """汎用指令（op と対象 id）→ UART の op と引数の作り方。"""

    op: str
    uart_op: int
    target: str | None = None
    args: tuple[tuple[str, str], ...] = ()
    """(UART の引数名, cmd の args の項目名) の並び。並び順は UART の引数の順。"""
    ttl_from_lease: bool = False


@dataclass(frozen=True)
class Profile:
    device_id: str
    transport: str
    channels: tuple[ChannelSpec, ...]
    actuators: tuple[ActuatorSpec, ...]
    states: Mapping[int, str]
    commands: tuple[CommandSpec, ...]
    modes: tuple[str, ...] = ()
    """set_mode で取りうる値（v0.2.1 #5）。"""

    def actuator(self, actuator_id: str) -> ActuatorSpec | None:
        for spec in self.actuators:
            if spec.id == actuator_id:
                return spec
        return None

    def state_name(self, number: int) -> str:
        """状態番号 → 状態名。表にない番号は KeyError。"""
        return self.states[number]

    def find_command(self, op: str, target: str | None) -> CommandSpec | None:
        """汎用指令に対応する UART の指令。target を持たない定義は op だけで一致させる。"""
        for spec in self.commands:
            if spec.op == op and (spec.target is None or spec.target == target):
                return spec
        return None


@cache
def _validator() -> Draft202012Validator:
    contents = json.loads((SCHEMA_DIR / "profile.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(contents)
    return Draft202012Validator(contents, registry=registry())


def _normalise(data: Any) -> Any:
    """YAML の整数キー（states の 0: INIT など）を文字列にする。"""
    if isinstance(data, dict):
        states = data.get("states")
        if isinstance(states, dict):
            data = {**data, "states": {str(k): v for k, v in states.items()}}
    return data


def parse_profile(data: Any, source: str = "<profile>") -> Profile:
    """読み込んだ YAML（dict）を検証して Profile にする。"""
    data = _normalise(data)
    errors = sorted(f"{json_pointer(e)}: {e.message}" for e in _validator().iter_errors(data))
    if errors:
        raise ProfileError(source, errors)

    actuator_ids = {a["id"] for a in data["actuators"]}
    commands = []
    problems: list[str] = []
    for index, raw in enumerate(data.get("commands", [])):
        target = raw.get("target")
        if target is not None and target not in actuator_ids:
            problems.append(f"/commands/{index}/target: actuators にない {target!r}")
        uart_op = _UART_OP_BY_NAME[raw["uart_op"]]
        names = tuple(raw.get("args", {}))
        if set(names) != set(CMD_ARG_NAMES[uart_op]):
            problems.append(
                f"/commands/{index}/args: {raw['uart_op']} の引数は {list(CMD_ARG_NAMES[uart_op])}"
            )
        commands.append(
            CommandSpec(
                op=raw["op"],
                uart_op=_UART_OP_BY_NAME[raw["uart_op"]],
                target=target,
                args=tuple(raw.get("args", {}).items()),
                ttl_from_lease=raw.get("ttl_s") == "lease_s",
            )
        )
    keys = [(c.op, c.target) for c in commands]
    if len(keys) != len(set(keys)):
        problems.append("/commands: 同じ op と target の組が重複している")
    if problems:
        raise ProfileError(source, problems)

    return Profile(
        device_id=data["device_id"],
        transport=data["transport"],
        channels=tuple(ChannelSpec(**c) for c in data["channels"]),
        actuators=tuple(ActuatorSpec(**a) for a in data["actuators"]),
        states={int(k): v for k, v in data.get("states", {}).items()},
        commands=tuple(commands),
        modes=tuple(data.get("modes", [])),
    )


def load_profile(device_id_or_path: str | Path) -> Profile:
    """device_id（同梱の profiles/{device_id}.yaml）またはファイルのパスから読み込む。"""
    path = Path(device_id_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = PROFILE_DIR / f"{device_id_or_path}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile = parse_profile(data, str(path))
    if path.parent == PROFILE_DIR and profile.device_id != path.stem:
        raise ProfileError(str(path), [f"/device_id: ファイル名と違う {profile.device_id!r}"])
    return profile
