"""シミュレータの CLI。

    uv run iotspec-sim run <scenario> [--speed N] [--broker host:port]
    uv run iotspec-sim run <scenario> --speed 0 --dump out.jsonl   # ブローカーなしで一括生成
    uv run iotspec-sim run uart_link --uart /dev/ttyUSB0             # 既存のシリアルポートを開く

mcu-c-01 だけのシナリオで --uart を省くと、自分で pty を作り、相手が開くパスを表示する。

<scenario> はシナリオ名（sim/scenarios/{name}.yaml）かファイルのパス。
接続先は --broker、なければ環境変数 IOTSPEC_MQTT_URL（既定 mqtt://localhost:1883）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiomqtt

from iotspec.sim.mqtt import BrokerAddress, broker_from_env, run_over_mqtt
from iotspec.sim.scenario import ScenarioError, load_scenario
from iotspec.sim.simulation import Simulation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iotspec-sim", description="iot-common-spec のシミュレータ"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="シナリオを流す")
    run.add_argument("scenario", help="シナリオ名またはファイルのパス")
    run.add_argument(
        "--speed", type=float, help="倍速（0 は待たずに一括生成）。省略時はシナリオの値"
    )
    run.add_argument("--broker", help="host:port（省略時は IOTSPEC_MQTT_URL）")
    run.add_argument("--duration-s", type=float, help="シナリオの継続時間を上書きする（仮想秒）")
    run.add_argument(
        "--uart",
        help="mcu_c_01・gw_01_lite が開くシリアルポートのパス（device モード。v0.2.1 #2）",
    )
    run.add_argument(
        "--dump", type=Path, help="MQTT に送らず、電文を JSON Lines でこのファイルに書く"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
    except ScenarioError as exc:
        print(f"シナリオが不正: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"シナリオが見つからない: {exc.filename}", file=sys.stderr)
        return 2
    if args.duration_s is not None:
        from dataclasses import replace

        scenario = replace(scenario, duration_s=args.duration_s)
    if args.uart is not None:
        for config in scenario.devices:
            if config.type in ("mcu_c_01", "gw_01_lite"):
                config.options["uart_path"] = args.uart
    simulation = Simulation(scenario, speed=args.speed)
    if args.dump is not None:
        messages = simulation.run_batch()
        with args.dump.open("w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps({"topic": m.topic, "message": m.message}, ensure_ascii=False))
                f.write("\n")
        print(f"{len(messages)} 件を {args.dump} に書き出した")
        return 0
    broker = BrokerAddress.parse(args.broker) if args.broker else broker_from_env()
    print(
        f"{scenario.name}: {scenario.start:%Y-%m-%dT%H:%M:%SZ} から {scenario.duration_s:g} 秒、"
        f"{simulation.runtime.speed:g} 倍速、{broker.host}:{broker.port} へ"
    )
    try:
        asyncio.run(run_over_mqtt(simulation, broker))
    except (aiomqtt.MqttError, OSError) as exc:
        where = f"{broker.host}:{broker.port}"
        print(f"MQTT ブローカーに接続できない（{where}）: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
