"""契約テストの CLI。

    uv run iotspec-contract --role mcu-cpp-01 [--broker host:port] [--only ack] [--junit out.xml]
    uv run iotspec-contract --role mcu-c-01 --target-cmd "./build/mcu_c_01_host {uart}"
    uv run iotspec-contract --role gw-01 --uart /dev/ttyUSB0
    uv run iotspec-contract --role app-01 --target-standin     # このリポジトリの代役で試す

対象は次のどれか：
- 何も指定しない：すでに動いている対象を観測する（起動順・LWT のチェックは省略）
- --target-cmd：そのコマンドで対象を起動し、終わったら止める。
  {uart} は仮想シリアルのパスに置き換える
- --target-standin：このリポジトリの代役を対象にする（使い方を試す・自己テスト用）
接続先は --broker、なければ IOTSPEC_MQTT_URL。
パスワードは IOTSPEC_MQTT_PASSWORD（ユーザー名は相手役の device_id）。
終了コード：0＝不合格・エラーなし、1＝不合格かエラーあり、2＝引数の誤り。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from iotspec.contract.context import ExternalTarget, SubprocessTarget, Target
from iotspec.contract.core import ROLES, checks_for, format_results, write_junit
from iotspec.contract.runner import run_contract, standin_target
from iotspec.sim.mqtt import BrokerAddress, broker_from_env


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iotspec-contract", description="iot-common-spec の契約テスト"
    )
    parser.add_argument("--role", required=True, choices=ROLES, help="対象の役割")
    parser.add_argument("--broker", help="host:port（省略時は IOTSPEC_MQTT_URL）")
    parser.add_argument(
        "--uart", help="対象が作ったシリアルポートを開く（省略時は契約テストが pty を作る）"
    )
    parser.add_argument("--only", action="append", help="このチェックだけ行う（複数回指定できる）")
    parser.add_argument("--device-id", help="対象の device_id（省略時は role と同じ）")
    parser.add_argument(
        "--duration-s", type=float, help="app-01：request を観測する秒数（既定 30）"
    )
    parser.add_argument("--junit", type=Path, help="JUnit XML の出力先（既定 contract-<role>.xml）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--target-cmd", help="対象を起動するコマンド（{uart} は仮想シリアルのパス）")
    group.add_argument(
        "--target-standin", action="store_true", help="このリポジトリの代役を対象にする"
    )
    parser.add_argument("--list", action="store_true", help="チェックの一覧を表示して終わる")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for item in checks_for(args.role):
            mark = "（対象の起動が必要）" if item.needs_target_control else ""
            print(f"{item.name}: {item.description}{mark}")
        return 0
    broker = BrokerAddress.parse(args.broker) if args.broker else broker_from_env()
    target: Target
    if args.target_cmd:
        target = SubprocessTarget(args.target_cmd)
    elif args.target_standin:
        target = standin_target(args.role)
    else:
        target = ExternalTarget()
    options = {} if args.duration_s is None else {"duration_s": args.duration_s}
    results = asyncio.run(
        run_contract(
            args.role,
            broker,
            target,
            device_id=args.device_id,
            uart_path=args.uart,
            only=args.only,
            options=options,
        )
    )
    print(format_results(results))
    junit = args.junit or Path(f"contract-{args.role}.xml")
    write_junit(results, junit, args.role)
    print(f"JUnit XML: {junit}")
    return 1 if any(r.status in ("failed", "error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
