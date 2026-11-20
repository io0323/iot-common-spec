# Changelog

## v0.2.1（初回リリース、2026-11-20）

共通仕様 v0.2 の実体の初回リリースです。仕様の本文は [docs/spec.md](docs/spec.md)、
正は [schemas/](schemas/) です。**トピックのメジャー版は `v1` のまま**（`iot/v1/{site}/{device_id}/{kind}`）で、
v0.2.1 の変更はすべて項目の追加と、仕様に書かれていなかった点の確定です。

### 仕様 v0.2 の内容（v0.1 からの変更）

- `request/ack` トピックを追加（作品04が要求の結果を知る）
- cmd に `priority`・`lease_s`・`expires_at`・`origin` を追加し、`release` を定義（リースと優先度の調停）
- cmd/ack の `status` を accepted／done／rejected／failed に統一し、`limited`・`applied`・`reason` を追加
- 重複排除の決め事（記憶する ID の数と期間、前回結果の再送）。MQTT は発信元・kind ごとの `seq` 1,024個
- エンベロープに `boot_id` と任意の `uptime_ms` を追加
- state に `channels[]`（`period_ms`）と `actuators[]` を必須化
- データ品質の扱い（故障・stale・欠損・重複・時刻ずれ・遅延到着・形式不正）を統一
- イベントコードと重大度を定義
- UART に STATE 種別、双方向 HEARTBEAT、CMD の `ttl_s`、ACK の `limited`・`applied` を追加
- 共通シミュレータと契約テストを `iot-common-spec` に集約

### v0.2.1 で確定したこと（[docs/open-questions.md](docs/open-questions.md) の #1〜#36）

**電文と Schema**
- `boot_id` は全デバイスで16進小文字8桁（#9）。device_id は `{種別}-{言語}-{連番}`、言語部は省略可（#3）
- cmd：必須は `cmd_id`・`op`・`priority`。`lease_s` は省略時60秒で、`set_actuator` 以外では無視（#4・#13）。
  op ごとの args（`release` は `id`、`set_mode` は `mode`、`set_param` は未定義で unknown_op で拒否してよい）（#5）。`level` は0–1000の整数（#8）
- cmd/ack・request/ack：rejected・failed では `reason` 必須、request/ack の `cmd_id` は accepted・done で必須（#6・#7）。
  重複した cmd_id への ack は payload を前回と同じにし、エンベロープは新しく作る（#28）
- telemetry：`actuators[].owner` は任意で interlock・safety・manual・auto・schedule・local・none（#10・#24）。
  任意項目 `device_state` を追加（#17）。fault の `value` も数値（#11）
- state：任意項目 `bridged_by` を追加。作品03は UART デバイスの HEARTBEAT が3秒途絶えたら、そのデバイス名義の
  offline の state を retain で発行する（#36）
- offline の state（LWT など）は seq を進めず、受信側は seq の判定から外す（#29）。復旧コードの重大度は info（#30）
- スケジュール実行の request_id のハッシュ入力は `"{schedule_id}|{planned_at}"`（#14）。
  リースは受信時刻から数え、受信時刻＋lease_s ちょうどで切れる（#15）

**UART（作品01 ⇔ 作品03）**
- seq は方向・フレーム種別ごとの連番。作品03は種別ごとに32ビットへ広げて対応する kind の seq に使う（#25）
- STATE にない state の項目はデバイスプロファイル `profiles/{device_id}.yaml` から補う（#16）。
  TELEMETRY の state と EVENT の from・to は状態名で `device_state`・`detail.from_state/to_state` に（#17）
- `fw_version:u16` は `"{major}.{minor}"`（#18）。ACK の `applied` は SET_FAN_OVERRIDE のときだけ `{"level": n}`（#19）
- CMD の args（#20）、リースのない op の `ttl_s` は0（#21）、変換できないフレームは破棄して数える（#22）、
  STATE 待ちの EVENT・ACK も破棄し、作品01は STATE → BOOT の順に送る（#23）
- EVENT code 8 に UPSTREAM_RESTORED を追加（#34）。作品01への指令の「毎秒1件」には再送も含める（#33）

**シミュレータ・環境**
- シミュレータのコードは `src/iotspec/sim/`、シナリオは `sim/scenarios/`（#1、spec.md 第8章の図を更新）
- 仮想シリアルは「pty を作る」「指定パスを開く（pyserial）」の2モード（#2）
- MQTT のユーザー名は device_id、ACL で cmd は gw-01、request は app-01、request/ack は gw-01 だけが発行できる（#31）
- 作品02の代役の計測周期とインターロックの解除条件（#26・#27）
- v0.2 では扱わない：代役の内部値（#32）、開発用 Mosquitto のファイル権限の警告（#35）

### このリリースに含まれるもの

- `schemas/`：common・kind ごとの7件・profile・scenario の JSON Schema
- `samples/valid/`・`samples/invalid/`：サンプル電文（invalid はファイル名に理由）
- `uart/golden/`：UART ゴールデンフレーム27件
- `iotspec`（Python パッケージ）：検証（`iotspec.schema`）、ID・エンベロープ・品質・重複排除・リース、
  UART コーデックと変換の参照実装、共通シミュレータ（`iotspec-sim`）、契約テストキット（`iotspec-contract`）
- `deploy/`：Mosquitto の設定・ACL・パスワード作成スクリプト、シミュレータのイメージ
