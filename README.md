# iot-common-spec

IoT ポートフォリオ（作品01〜04）が共有する通信・データ仕様の「実体」を置くリポジトリです。
JSON Schema、サンプル電文、UART ゴールデンフレーム、共通シミュレータ、契約テストキットを提供し、
各作品はこのリポジトリをタグ指定で取り込んで、実機なしで相互接続と契約テストを行います。

- **仕様の版：共通仕様 v0.2（このリポジトリの版は v0.2.3）**。トピックのメジャー版は `v1`
- 対象：MQTT のトピックと JSON ペイロード、作品01⇔作品03 の UART フレーム、共通シミュレータと契約テスト
- 変更点：[CHANGELOG.md](CHANGELOG.md)

| 作品 | device_id | つながり方 |
|---|---|---|
| 作品01（C × STM32） | `mcu-c-01` | UART で作品03へ |
| 作品02（C++ × ESP32） | `mcu-cpp-01` | MQTT |
| 作品03（C++ × Raspberry Pi、ゲートウェイ） | `gw-01` | MQTT と UART |
| 作品04（Python × Raspberry Pi、アプリ） | `app-01` | MQTT |

## 仕様の正

**仕様の正は [docs/spec.md](docs/spec.md) と [schemas/](schemas/) です。**
spec.md は説明であり、両者が食い違う場合は schemas/ を正とします（spec.md 第1章）。
仕様に書かれていなかった点の決定は [docs/open-questions.md](docs/open-questions.md) にあります（v0.2.2 で #1〜#44、v0.2.3 で #45 を確定）。

## 取り込み方

```sh
uv add "iotspec @ git+https://github.com/io0323/iot-common-spec@v0.2.3"        # ライブラリとして
uv tool install "iotspec @ git+https://github.com/io0323/iot-common-spec@v0.2.3"  # コマンドだけ
```

パッケージには Schema・プロファイル・シナリオ・UART ゴールデンが同梱されています。

## サンプル電文を検証する

```sh
uv run python tools/validate.py samples/valid/*.json      # 終了コード 0=適合、1=不適合、2=schema 不明
cat msg.json | uv run python tools/validate.py            # 標準入力から
```

ライブラリからは `iotspec.schema.validate(message)` が不適合の理由（JSON Pointer つき）のリストを返します。
`samples/valid/` は受信側が受け付けるべき電文、`samples/invalid/` は破棄すべき電文（ファイル名に理由）です。

## シミュレータを動かす

```sh
deploy/mosquitto/gen_passwd.sh                 # パスワードファイル（リポジトリには入れない）
docker compose up -d mosquitto                 # ACL・パスワード付きの Mosquitto
export IOTSPEC_MQTT_PASSWORD=iotspec-dev       # gen_passwd.sh の既定値

uv run iotspec-sim run normal_day --speed 60                       # 作品02の代役が1日を60倍速で
uv run iotspec-sim run request_flow                                # 作品04 → 作品03 の要求の流れ
uv run iotspec-sim run uart_link                                   # 作品03 ⇔ 作品01 を pty でつなぐ
uv run iotspec-sim run normal_day --speed 0 --dump out.jsonl       # ブローカーなしで一括生成
SCENARIO=normal_day docker compose up sim                          # コンテナで
```

シナリオは `sim/scenarios/`（normal_day・sensor_fault・duplicate_and_gap・request_flow・uart_link）。
mcu-c-01 だけのシナリオでは pty を作ってパスを表示するので、作品03（または作品01のホスト版の相手）がそこにつなげます。
既存のシリアルポートを開くときは `--uart PATH` を使います。

## 契約テスト

```sh
iotspec-contract --role mcu-cpp-01 --target-cmd "./build/host/mcu_cpp_01"
iotspec-contract --role mcu-c-01 --target-cmd "./build/host/mcu_c_01 {uart}"
iotspec-contract --role gw-01 --list
```

role ごとのチェック、対象の起動のしかた、GitHub Actions の例は [docs/contract-tests.md](docs/contract-tests.md) にあります。

## ディレクトリ

| パス | 内容 |
|---|---|
| `docs/spec.md` | 共通仕様の本文（v0.2、v0.2.1〜v0.2.3 の明記を含む） |
| `docs/open-questions.md` | 仕様に書かれていなかった点と、その決定 |
| `docs/contract-tests.md` | 契約テストの使い方（他のリポジトリ向け） |
| `schemas/` | kind ごとの JSON Schema と共通定義（`$defs`）、プロファイル・シナリオの Schema |
| `samples/valid/`, `samples/invalid/` | 正常系・異常系のサンプル電文 |
| `uart/golden/` | UART ゴールデンフレーム（`*.hex` と対応する `*.json`） |
| `profiles/` | デバイスプロファイル（state の補完、UART の状態名と指令の対応） |
| `src/iotspec/` | 共通ライブラリ（schema・envelope・ids・quality・dedup・lease・uart・sim・contract） |
| `sim/scenarios/` | シミュレータのシナリオ（YAML） |
| `deploy/` | Mosquitto の設定・ACL・パスワード作成スクリプト、シミュレータのイメージ |
| `tools/` | CLI（validate・gen_golden） |
| `tests/unit/`, `tests/integration/` | 単体テスト／結合テスト（結合は Mosquitto が必要） |

## 開発

```sh
uv sync
uv run pytest -m "not integration"                                             # 単体テスト
deploy/mosquitto/gen_passwd.sh && docker compose up -d mosquitto && uv run pytest -m integration   # 結合テスト
uv run ruff check . && uv run ruff format --check . && uv run mypy src tools   # 静的解析
uv run python tools/gen_golden.py --check                                      # ゴールデンの再生成で差分がないか
```

変更は Pull Request で行い、影響する作品を説明に書きます（spec.md 第1章「版の方針」）。
