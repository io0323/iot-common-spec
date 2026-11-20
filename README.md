# iot-common-spec

IoT ポートフォリオ（作品01〜04）が共有する通信・データ仕様の「実体」を置くリポジトリです。
JSON Schema、サンプル電文、UART ゴールデンフレーム、共通シミュレータ、契約テストキットを提供し、
各作品はこのリポジトリをタグ指定で取り込んで、実機なしで相互接続と契約テストを行います。

- 対応する仕様の版：**共通仕様 v0.2**（タグ `v0.2.0` は未付与・作業中）
- 対象：MQTT のトピックと JSON ペイロード、作品01⇔作品03 の UART フレーム、共通シミュレータと契約テスト

## 仕様の正

**仕様の正は [docs/spec.md](docs/spec.md) と [schemas/](schemas/) です。**
spec.md は説明であり、両者が食い違う場合は schemas/ を正とします（spec.md 第1章）。
仕様に書かれていない点や食い違いは [docs/open-questions.md](docs/open-questions.md) に記録します。

## ディレクトリ

| パス | 内容 |
|---|---|
| `docs/spec.md` | 共通仕様 v0.2 の本文 |
| `docs/open-questions.md` | 未決の論点 |
| `schemas/` | kind ごとの JSON Schema と共通定義（`$defs`） |
| `samples/valid/` | 正常系サンプル電文 |
| `samples/invalid/` | 異常系サンプル電文（受信側が破棄すべきもの。ファイル名に理由） |
| `uart/golden/` | UART ゴールデンフレーム（`*.hex` と対応する `*.json`） |
| `src/iotspec/` | 共通ライブラリ（envelope, ids, quality, dedup, uart, sim, contract） |
| `sim/scenarios/` | シミュレータのシナリオ（YAML） |
| `deploy/mosquitto/` | Mosquitto の設定・ACL |
| `tools/` | CLI（validate など） |
| `tests/unit/`, `tests/integration/` | 単体テスト／結合テスト（結合は Mosquitto が必要） |

## 必要なもの

- Python 3.12、[uv](https://docs.astral.sh/uv/)
- Docker（Mosquitto を使う結合テスト用）
- socat（任意）

## コマンド

```sh
uv sync                                   # 依存の導入
uv run pytest -m "not integration"        # 単体テスト
docker compose up -d mosquitto            # 結合テストの前に
uv run pytest -m integration              # 結合テスト
uv run ruff check . && uv run ruff format --check . && uv run mypy src tools   # 静的解析
```

## 変更の進め方

変更は Pull Request で行い、影響する作品を説明に書きます（spec.md 第1章「版の方針」）。
