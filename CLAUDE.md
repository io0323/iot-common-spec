# iot-common-spec

作品01〜04（IoT ポートフォリオ）が共有する通信・データ仕様の「実体」を置くリポジトリ。
JSON Schema、サンプル電文、UART ゴールデンフレーム、共通シミュレータ、契約テストキットを持つ。

## 正とするもの
- 仕様の説明は docs/spec.md（共通仕様 v0.2）。食い違いがあれば schemas/ を正とする（spec.md 第1章）
- 仕様に書かれていないことを独断で決めない。判断が必要なら docs/open-questions.md に
  「論点・選択肢・推奨案」を追記し、作業を止めて報告する
- docs/spec.md は必要な章だけ読む（全文を毎回読まない）

## 技術スタック
- Python 3.12、uv で依存管理、パッケージ名 `iotspec`（src レイアウト）
- jsonschema（Draft 2020-12）、python-ulid、PyYAML、aiomqtt、pyserial（仮想シリアルの「指定したデバイスパスを開くモード」用）
- テスト：pytest、pytest-asyncio、hypothesis
- 静的解析：ruff（lint + format）、mypy --strict
- MQTT ブローカー：Mosquitto（docker compose）

## ディレクトリ
- schemas/            kind ごとの JSON Schema と共通定義（$defs）
- samples/valid/      正常系サンプル電文（kind ごと）
- samples/invalid/    異常系サンプル電文（破棄されるべきもの。ファイル名に理由を含める）
- uart/golden/        UART ゴールデンフレーム（*.hex と対応する *.json）
- src/iotspec/        共通ライブラリ（envelope, ids, quality, dedup, uart, sim, contract）
- sim/scenarios/      シミュレータのシナリオ（YAML）
- deploy/mosquitto/   Mosquitto 設定・ACL
- tools/              CLI（validate など）
- tests/              unit/ と integration/（integration は Mosquitto が必要）

## コマンド
- uv run pytest -m "not integration"   単体テスト
- uv run pytest -m integration          結合テスト（先に docker compose up -d mosquitto）
- uv run ruff check . && uv run ruff format --check . && uv run mypy src tools

## 規約
- 時刻はすべて UTC。ISO 8601 ミリ秒、末尾 Z
- 時刻を読む処理は Clock を注入する（テストで仮想時計に差し替えるため）
- 数値・コード値・上限値は spec.md から定数モジュール（iotspec.constants）に集め、散らばらせない
- 各フェーズの完了条件のコマンドがすべて通るまで「完了」と報告しない
