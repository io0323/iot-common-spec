"""契約テストの骨組み：チェックの登録、実行、結果の表示、JUnit XML。

各チェックは「相手役を立ち上げる → 対象を観測する → 判定する」の形の async 関数で、
判定で食い違いを見つけたら Checker に記録する。
失敗は「どのチェックが、どの電文で、何が期待と違ったか」で表示する。
"""

from __future__ import annotations

import importlib
import time
import traceback
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from iotspec.contract.context import ContractContext

__all__ = [
    "ROLES",
    "Check",
    "CheckResult",
    "Checker",
    "Failure",
    "Skip",
    "check",
    "checks_for",
    "format_results",
    "run_checks",
    "write_junit",
]

ROLES = ("mcu-c-01", "mcu-cpp-01", "gw-01", "app-01")
Status = Literal["passed", "failed", "skipped", "error"]


@dataclass(frozen=True)
class Failure:
    """期待と違ったこと1件。ref は電文やフレームを指す短い文字列（トピック・seq・cmd_ref など）。"""

    message: str
    ref: str | None = None
    expected: str | None = None
    actual: str | None = None

    def render(self) -> str:
        parts = [self.message]
        if self.ref:
            parts.append(f"電文: {self.ref}")
        if self.expected is not None:
            parts.append(f"期待: {self.expected}")
        if self.actual is not None:
            parts.append(f"実際: {self.actual}")
        return " / ".join(parts)


class Skip(Exception):
    """チェックを行えない（前提が満たされない）。理由を持つ。"""


class Checker:
    """1つのチェックの判定を集める。"""

    def __init__(self) -> None:
        self.failures: list[Failure] = []

    def expect(
        self,
        condition: bool,
        message: str,
        *,
        ref: str | None = None,
        expected: Any = None,
        actual: Any = None,
    ) -> bool:
        if not condition:
            self.fail(message, ref=ref, expected=expected, actual=actual)
        return condition

    def fail(
        self, message: str, *, ref: str | None = None, expected: Any = None, actual: Any = None
    ) -> None:
        self.failures.append(
            Failure(
                message,
                ref,
                None if expected is None else str(expected),
                None if actual is None else str(actual),
            )
        )


CheckFn = Callable[["ContractContext", Checker], Awaitable[None]]


@dataclass(frozen=True)
class Check:
    role: str
    name: str
    description: str
    fn: CheckFn
    order: int
    needs_target_control: bool = False
    """対象を起動・強制終了できるときだけ行える（起動順、LWT）。"""


_REGISTRY: list[Check] = []


def check(
    role: str, name: str, description: str, *, order: int = 50, needs_target_control: bool = False
) -> Callable[[CheckFn], CheckFn]:
    """チェックを登録する。order の小さい順に実行する（同じなら登録順）。"""
    if role not in (*ROLES, "common"):
        raise ValueError(f"unknown role: {role!r}")

    def register(fn: CheckFn) -> CheckFn:
        _REGISTRY.append(Check(role, name, description, fn, order, needs_target_control))
        return fn

    return register


def checks_for(role: str) -> list[Check]:
    """role のチェック（共通チェックを含む）を実行順に返す。"""
    # role ごとのモジュールを読み込むと、@check でチェックが登録される
    importlib.import_module("iotspec.contract.roles")
    selected = [c for c in _REGISTRY if c.role in (role, "common")]
    if role == "mcu-c-01":  # UART だけのデバイスには MQTT の共通チェックを当てない
        selected = [c for c in selected if c.role == role]
    indexed = list(enumerate(selected))
    return [c for _, c in sorted(indexed, key=lambda ic: (ic[1].order, ic[0]))]


@dataclass
class CheckResult:
    role: str
    name: str
    description: str
    status: Status
    failures: list[Failure] = field(default_factory=list)
    reason: str = ""
    duration_s: float = 0.0


async def run_checks(
    ctx: ContractContext, role: str, only: Iterable[str] | None = None
) -> list[CheckResult]:
    """role のチェックを順に行う。only を渡すと、その名前のチェックだけを行う。"""
    wanted = set(only) if only else None
    results: list[CheckResult] = []
    for item in checks_for(role):
        if wanted is not None and item.name not in wanted:
            continue
        started = time.monotonic()
        result = CheckResult(role, item.name, item.description, "passed")
        if item.needs_target_control and not ctx.target.controllable:
            result.status = "skipped"
            result.reason = "対象を起動・強制終了できない（--target-cmd を指定すると行える）"
            results.append(result)
            continue
        checker = Checker()
        try:
            await item.fn(ctx, checker)
        except Skip as exc:
            result.status, result.reason = "skipped", str(exc)
        except Exception as exc:  # チェックの不具合も結果（error）として残す
            result.status = "error"
            result.reason = f"{type(exc).__name__}: {exc}"
            result.failures.append(Failure(traceback.format_exc(limit=5)))
        else:
            if checker.failures:
                result.status = "failed"
                result.failures = checker.failures
        result.duration_s = time.monotonic() - started
        results.append(result)
    return results


_MARK = {"passed": "合格", "failed": "不合格", "skipped": "省略", "error": "エラー"}


def format_results(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for r in results:
        lines.append(
            f"[{_MARK[r.status]}] {r.role} / {r.name}：{r.description}（{r.duration_s:.1f}秒）"
        )
        if r.reason:
            lines.append(f"    理由: {r.reason}")
        for f in r.failures[:20]:
            lines.append(f"    - {f.render()}")
        if len(r.failures) > 20:
            lines.append(f"    - ほか {len(r.failures) - 20} 件")
    counts = {s: sum(1 for r in results if r.status == s) for s in _MARK}
    lines.append(
        "合計: "
        + "、".join(f"{_MARK[s]} {n}" for s, n in counts.items() if n)
        + f"（{len(results)} 件）"
    )
    return "\n".join(lines)


def write_junit(results: list[CheckResult], path: Path, role: str) -> None:
    """JUnit XML を書く（testsuite = role、testcase = チェック）。"""
    suite = ET.Element(
        "testsuite",
        name=f"iotspec-contract.{role}",
        tests=str(len(results)),
        failures=str(sum(r.status == "failed" for r in results)),
        errors=str(sum(r.status == "error" for r in results)),
        skipped=str(sum(r.status == "skipped" for r in results)),
        time=f"{sum(r.duration_s for r in results):.3f}",
    )
    for r in results:
        case = ET.SubElement(
            suite, "testcase", classname=f"{role}", name=r.name, time=f"{r.duration_s:.3f}"
        )
        text = "\n".join(f.render() for f in r.failures)
        if r.status == "failed":
            ET.SubElement(case, "failure", message=r.description).text = text
        elif r.status == "error":
            ET.SubElement(case, "error", message=r.reason).text = text
        elif r.status == "skipped":
            ET.SubElement(case, "skipped", message=r.reason)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
