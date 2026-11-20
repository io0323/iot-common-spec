"""JSON Schema の読み込みと電文の検証（共通仕様 v0.2 第3章）。

電文の schema フィールド（例："iot.v1.cmd_ack"）から該当する Schema を選んで検証する。
Schema は schemas/ を正とする。シミュレータや他作品のテストから import して使う。

    >>> from iotspec.schema import validate
    >>> validate(message)   # 適合なら []、不適合なら "JSON Pointer: 理由" のリスト
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from iotspec.constants import SCHEMA_NAMES

__all__ = [
    "SCHEMA_DIR",
    "ValidationError",
    "iter_errors",
    "json_pointer",
    "registry",
    "select_schema",
    "validate",
    "validator_for",
]


def _find_schema_dir() -> Path:
    """配布物に同梱した Schema（iotspec/_schemas）か、リポジトリの schemas/ を探す。"""
    here = Path(__file__).resolve().parent
    for candidate in (here / "_schemas", here.parents[1] / "schemas"):
        if (candidate / "common.schema.json").is_file():
            return candidate
    raise FileNotFoundError("schemas/common.schema.json が見つからない")


SCHEMA_DIR: Path = _find_schema_dir()
"""Schema ファイルを置いたディレクトリ。"""

_KNOWN_SCHEMAS: frozenset[str] = frozenset(SCHEMA_NAMES.values())


def _file_name(schema_name: str) -> str:
    """'iot.v1.cmd_ack' → 'cmd_ack.schema.json'。"""
    return schema_name.removeprefix("iot.v1.") + ".schema.json"


@cache
def registry() -> Registry[Any]:
    """schemas/ のすべての Schema を登録したレジストリ（$ref の解決用）。"""
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents, DRAFT202012)))
    reg: Registry[Any] = Registry().with_resources(resources)
    return reg.crawl()


@cache
def validator_for(schema_name: str) -> Draft202012Validator:
    """schema フィールドの値に対応する検証器を返す。未知の値なら KeyError。"""
    if schema_name not in _KNOWN_SCHEMAS:
        raise KeyError(schema_name)
    contents = json.loads((SCHEMA_DIR / _file_name(schema_name)).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(contents)
    return Draft202012Validator(contents, registry=registry())


def select_schema(message: object) -> str | None:
    """電文の schema フィールドが既知の値ならそれを返し、そうでなければ None。"""
    if isinstance(message, Mapping):
        name = message.get("schema")
        if isinstance(name, str) and name in _KNOWN_SCHEMAS:
            return name
    return None


def json_pointer(error: ValidationError) -> str:
    """エラーの位置を JSON Pointer（例："/payload/args/level"）で返す。ルートは ""。"""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in error.absolute_path)


def iter_errors(message: object) -> Iterator[ValidationError]:
    """電文の検証エラーを列挙する。schema フィールドが不明なら KeyError。"""
    name = select_schema(message)
    if name is None:
        raise KeyError("schema")
    yield from validator_for(name).iter_errors(message)


def validate(message: object) -> list[str]:
    """電文を検証し、不適合の理由を "JSON Pointer: 理由" の形で返す。適合なら空リスト。

    schema フィールドが無い・未知の場合は "/schema: ..." の1件を返す。
    """
    if not isinstance(message, Mapping):
        return [": 電文が JSON オブジェクトではない"]
    if select_schema(message) is None:
        return [f"/schema: 未知または欠落した schema フィールド: {message.get('schema')!r}"]
    errors = sorted(iter_errors(message), key=lambda e: (json_pointer(e), e.message))
    return [f"{json_pointer(e)}: {e.message}" for e in errors]
