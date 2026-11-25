# 契約テストの使い方（作品01〜04のリポジトリ向け）

`iot-common-spec` の契約テストキットは、各作品の実装が共通仕様 v0.2.2 を守っているかを、
相手役（このリポジトリの代役）とのやり取りで確かめます。各作品のリポジトリの CI で実行してください。

- 対象：あなたの作品（作品01〜04のどれか1つ）
- 相手役：契約テストが自分で務めます（作品03の役で cmd を送る、作品01の役で UART のフレームを返す、など）
- 結果：画面に「どのチェックが、どの電文で、何が期待と違ったか」を表示し、JUnit XML を書き出します

## 1. 取り込み方

`iotspec` パッケージをタグ指定で入れます。Schema・プロファイル・シナリオ・UART ゴールデンはパッケージに同梱されています。

```sh
# uv のプロジェクトに依存として追加する（Python で書く作品04など）
uv add "iotspec @ git+https://github.com/io0323/iot-common-spec@v0.2.2"

# コマンドだけを入れる（C・C++ の作品01〜03など）
uv tool install "iotspec @ git+https://github.com/io0323/iot-common-spec@v0.2.2"
iotspec-contract --help
```

Mosquitto の設定（ACL・パスワードを作るスクリプト）はパッケージに含まれません。
リポジトリを同じタグで取り出して使います（下の GitHub Actions の例を参照）。

## 2. 実行のしかた

```sh
iotspec-contract --role <role> [--broker host:port] [--uart PATH] [--only CHECK] [--target-cmd CMD] [--junit FILE]
iotspec-contract --role <role> --list      # チェックの一覧
```

| 引数 | 意味 |
|---|---|
| `--role` | `mcu-c-01`（作品01）・`mcu-cpp-01`（作品02）・`gw-01`（作品03）・`app-01`（作品04） |
| `--broker` | MQTT ブローカー。省略時は `IOTSPEC_MQTT_URL`（既定 `mqtt://localhost:1883`） |
| `--uart` | 対象が作ったシリアルポートを開く（Renode や実機）。省略時は契約テストが pty を作り、そのパスを表示する |
| `--target-cmd` | 対象を起動するコマンド。契約テストが起動し、終わったら止める。`{uart}` は契約テストの pty のパスに置き換わる（環境変数 `IOTSPEC_UART_PATH` にも入る） |
| `--only` | そのチェックだけ行う（複数回指定できる） |
| `--junit` | JUnit XML の出力先（既定 `contract-<role>.xml`） |
| `--duration-s` | `app-01` だけ：request を観測する秒数（既定 30） |
| `--target-standin` | このリポジトリの代役を対象にする（キットの動きを試す用） |

- MQTT のユーザー名は device_id です。契約テストの相手役は `gw-01`・`app-01`・`observer` の名義で接続します。
  パスワードは `IOTSPEC_MQTT_PASSWORD` で渡します（`deploy/mosquitto/gen_passwd.sh` と同じ値）
- `--target-cmd` を指定しないと、起動の順序（作品01）と LWT（作品02）のチェックは「省略」になります。
  対象を契約テストから起動できるようにしておくと、すべてのチェックを行えます
- 終了コードは、不合格かエラーがあれば 1、なければ 0

## 3. role ごとのチェック

### 作品01（`--role mcu-c-01`）：UART

相手役は作品03（UART の上位）。契約テストが1秒ごとに HEARTBEAT を送り、CMD を送ります。
この role は UART だけを使うので、**MQTT ブローカーは要りません**（Mosquitto を起動しなくても実行できます）。

| チェック | 内容 |
|---|---|
| `boot_order` | 起動後の最初のフレームが STATE で、その次の EVENT が BOOT（`--target-cmd` が必要） |
| `boot_reset_reason` | BOOT の from が reset_reason（0〜6）で、to が 0（v0.2.2。`--target-cmd` が必要） |
| `ack` | CMD（GET_STATUS・SET_FAN_OVERRIDE）に同じ cmd_ref の ACK（done）が2秒以内に返る |
| `get_diag` | GET_DIAG に DIAG → ACK（done）の順で返る（v0.2.2） |
| `ttl_s_range` | SET_FAN_OVERRIDE の ttl_s 0・601 は rejected（invalid_arg）、600 は done（v0.2.2） |
| `duplicate_cmd_ref` | 重複した cmd_ref には前回の ACK を返し、指令を実行し直さない |
| `upstream_lost_restored` | 上位の HEARTBEAT が10秒途絶えると手動指定を解除して UPSTREAM_LOST、戻ると UPSTREAM_RESTORED（EVENT code 8） |
| `frame_encoding` | 受けた全フレームが CRC・COBS・長さとも正しく、参照実装と同じバイト列に符号化される |
| `no_expired` | ACK の reason に expired を使わない（v0.2.2） |
| `seq_per_type` | UART の seq がフレーム種別ごとの連番 |
| `converted_schema` | 受けたフレームを参照実装（作品03の変換）で電文にすると、すべて JSON Schema に適合する |

対象の起動：作品01のホスト版（PC で動く版）が、引数か `IOTSPEC_UART_PATH` で受けたパスのシリアルを開くようにします。

```sh
iotspec-contract --role mcu-c-01 --target-cmd "./build/host/mcu_c_01 {uart}"
```

Renode で動かす場合は、Renode 側が作った仮想 UART のパスを `--uart` で渡します（接続方法は作品01の着手時に確認）。

ゴールデンフレームとの一致は、作品01の**単体テスト**でも確かめてください。`uart/golden/*.json` の `frame` の値を
作品01の符号化関数に渡し、結果が同じファイルの `hex` と一致することを比べます（作品01→03 の向きのもの。
CMD は作品01の復号のテストに使えます）。ゴールデンは `iotspec` の `_golden/` にも同梱しています。

### 作品02（`--role mcu-cpp-01`）：MQTT

相手役は作品03（gw-01 の名義で cmd を送る）。対象はブローカーに `mcu-cpp-01` で接続し、state（online、retain）を
発行している前提で始めます。出力の対象は、対象の state の `actuators[]` から選びます。

| チェック | 内容 |
|---|---|
| `ack` | set_actuator に同じ cmd_id の cmd/ack（done、applied 付き）が2秒以内に返る |
| `lease_expiry` | リースが切れると、その出力の owner が上位の priority から local（または none）に戻る |
| `duplicate_cmd_id` | 重複した cmd_id には前回の ack を返し、指令を実行し直さない |
| `limited_applied` | 連続運転の上限（max_on_s）を超える lease_s の指令の ack に limited: true と applied |
| `set_param_unknown_op` | set_param には rejected（unknown_op） |
| `lwt` | 接続が切れると LWT の offline の state（retain）が発行され、seq の判定で破棄されない（`--target-cmd` が必要） |
| 共通 | `schema`・`seq_and_boot_id`・`publish_rate`（下記） |

```sh
iotspec-contract --role mcu-cpp-01 --target-cmd "./build/host/mcu_cpp_01 --broker localhost:1883"
```

### 作品03（`--role gw-01`）：UART と MQTT

相手役は作品01（UART、契約テストが pty を作る）と作品04（app-01 の名義で request を送る）。
対象は契約テストの pty を作品01の UART として開き、ブローカーに `gw-01` で接続します。
**ゴールデンの照合は、作品01とのやり取りをまだしていない起動したての対象**に対して最初に行うので、`--target-cmd` で起動してください。

| チェック | 内容 |
|---|---|
| `golden_conversion` | UART のゴールデンフレームを順に送ると、期待どおりのエンベロープ（msg_id・ts 以外の全項目）に変換される。DIAG と GET_DIAG の ACK は MQTT に出ない（v0.2.2）。STATE 待ちでは GET_STATUS を送る |
| `uart_cmd_limits` | 作品01への CMD は毎秒1件（再送を含む）・未完了4件まで、ACK が2秒来なければ同じ cmd_ref で最大3回再送 |
| `request_ack` | request に accepted（cmd_id 付き）→ done を返し、同じ request_id の再送には現在の状態を返す |
| `bridged_offline_state` | UART デバイスが3秒途絶えると、そのデバイス名義の offline の state を bridged_by 付き・retain で発行する |
| 共通 | `schema`・`seq_and_boot_id`・`publish_rate`（UART 由来の電文の seq は欠番を残すので単調増加で判定） |

```sh
iotspec-contract --role gw-01 --target-cmd "./build/gw01 --uart {uart} --broker localhost:1883"
```

### 作品04（`--role app-01`）：MQTT

相手役は作品03（gw-01 の名義で request/ack を返す）。相手役は**最初に届いた request_id にだけわざと応答せず**、
再送を確かめます。ほかには accepted → done を返します。対象は観測の時間（既定30秒）のうちに、
**手動操作の request を2件以上と、スケジュール実行の request を1件以上**送るように設定してください。

| チェック | 内容 |
|---|---|
| `request_schema` | request が Schema に適合し、手動とスケジュール実行の両方が観測できる |
| `retransmission` | 応答がなければ同じ request_id・同じ内容で5秒ごとに最大3回再送して止まる |
| `schedule_request_id` | スケジュール実行の request_id が `schedule_request_id(schedule.id, planned_at)` と一致する |
| `ack_tracking` | request/ack を受けたあとは、その request_id を再送しない |
| 共通 | `schema`・`seq_and_boot_id`・`publish_rate` |

```sh
iotspec-contract --role app-01 --target-cmd "uv run app04 --contract-scenario" --duration-s 30
```

### 共通（MQTT につながる role）

| チェック | 内容 |
|---|---|
| `schema` | 対象が発行した全電文が JSON Schema に適合し、トピックとエンベロープの device_id が一致する |
| `seq_and_boot_id` | seq が発信元・boot_id・kind ごとに連番で、boot_id が付いている（offline の state は除く） |
| `publish_rate` | kind ごとに平均毎秒10件・瞬間毎秒50件以内（最低5秒は観測する） |

MQTT クライアントの確認待ち（QoS 1 の未確認送信）32件の上限は、ブローカーの外から観測できないため、
契約テストでは判定しません。各作品の MQTT クライアントの設定（最大 in-flight 数）で守ってください。

## 4. GitHub Actions の例

設定ファイル（ACL・パスワード）をマウントする必要があるので、Mosquitto は `services:` ではなく
`docker compose` で起動します（サービスコンテナはリポジトリを取り出す前に起動するため、ファイルを渡せない）。

```yaml
name: contract
on: [push, pull_request]
jobs:
  contract:
    runs-on: ubuntu-latest
    env:
      IOTSPEC_MQTT_URL: mqtt://localhost:1883
      IOTSPEC_MQTT_PASSWORD: ci-${{ github.run_id }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: io0323/iot-common-spec
          ref: v0.2.2
          path: iot-common-spec
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.12"
      - name: Mosquitto（ACL・パスワード付き）
        run: |
          iot-common-spec/deploy/mosquitto/gen_passwd.sh
          docker compose -f iot-common-spec/docker-compose.yml up -d mosquitto
      - name: 契約テストキット
        run: uv tool install "iotspec @ git+https://github.com/io0323/iot-common-spec@v0.2.2"
      - name: 対象をビルド
        run: make host            # 作品ごとに読み替える
      - name: 契約テスト
        run: iotspec-contract --role mcu-cpp-01 --target-cmd "./build/host/mcu_cpp_01" --junit contract.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: contract-junit
          path: contract.xml
```

## 5. キット自体の確かめ方（このリポジトリ）

```sh
deploy/mosquitto/gen_passwd.sh && docker compose up -d mosquitto
uv run iotspec-contract --role gw-01 --target-standin          # 代役を対象にして全チェック
uv run pytest -m integration tests/integration/test_contract.py
```

`tests/integration/test_contract.py` は、各 role の代役が全チェックに合格すること（自己テスト）と、
わざと壊した代役（重複 cmd_id で再実行する、送信上限を超える、再送で cmd_ref を変える、
スケジュール実行で毎回違う request_id を作る）が該当するチェックで不合格になることを確かめます。
