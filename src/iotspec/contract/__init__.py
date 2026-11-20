"""契約テストキット（第8章「作品ごとの契約テストで使う相手役」）。

作品01〜04の各リポジトリは、自分の実装を対象にしてこのキットを実行する：

    uv run iotspec-contract --role mcu-cpp-01 --broker localhost:1883

チェックは role ごとに iotspec.contract.roles に登録してある。使い方は docs/contract-tests.md。
"""

from iotspec.contract.core import ROLES, CheckResult, checks_for, format_results, run_checks

__all__ = ["ROLES", "CheckResult", "checks_for", "format_results", "run_checks"]
