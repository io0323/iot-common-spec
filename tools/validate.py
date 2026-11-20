"""電文を JSON Schema で検証する CLI（共通仕様 v0.2 第8章 tools/validate.py）。

使い方:
    uv run python tools/validate.py FILE [FILE ...]
    cat msg.json | uv run python tools/validate.py        # ファイル省略または "-" で標準入力

電文の schema フィールドから該当する Schema を選んで検証する。
終了コード: 0=すべて適合、1=不適合あり（理由を表示）、2=schema フィールド不明
（複数ファイルのときは最も大きいコードを返す）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from iotspec.schema import select_schema, validate

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNKNOWN_SCHEMA = 2


def check(text: str, label: str) -> int:
    """1件の電文を検証して結果を表示し、終了コードを返す。"""
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"{label}: 不適合: JSON として読めない: {exc}")
        return EXIT_INVALID
    if select_schema(message) is None:
        found = message.get("schema") if isinstance(message, dict) else None
        print(f"{label}: schema フィールド不明: {found!r}")
        return EXIT_UNKNOWN_SCHEMA
    errors = validate(message)
    if errors:
        print(f"{label}: 不適合 ({message['schema']})")
        for error in errors:
            print(f"  {error}")
        return EXIT_INVALID
    print(f"{label}: 適合 ({message['schema']})")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="電文を iot-common-spec の JSON Schema で検証する")
    parser.add_argument("files", nargs="*", help="電文の JSON ファイル（省略または - で標準入力）")
    args = parser.parse_args(argv)
    files: list[str] = args.files or ["-"]
    code = EXIT_OK
    for name in files:
        if name == "-":
            result = check(sys.stdin.read(), "<stdin>")
        else:
            result = check(Path(name).read_text(encoding="utf-8"), name)
        code = max(code, result)
    return code


if __name__ == "__main__":
    sys.exit(main())
