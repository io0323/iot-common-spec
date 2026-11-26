# Changelog

## v0.2.2（2026-11-26）

作品01の実装前レビューで出た変更案 CR-1〜5（作品01 `docs/design/03-common-spec-cr-v0.2.2.md`）を取り込みました。
**トピックのメジャー版は `v1` のまま**で、変更はすべて UART の種別・コード値と Schema の項目の追加です。
v0.2.1 の電文は v0.2.2 の Schema でもすべて通ります。

### 決定（[docs/open-questions.md](docs/open-questions.md) の #37〜#44）

- **DIAG**（#37、CR-1）：UART に種別 DIAG（0x05、01→03、15バイト）を新設。作品01は GET_DIAG に DIAG → ACK(done) の順で返す。
  作品03は DIAG を MQTT に変換せず（破棄理由 `not_converted`）、履歴に残す。GET_DIAG は作品03の内部の操作で、その ACK も
  cmd/ack にしない（`not_converted`）
- **BOOT のリセット要因**（#38、CR-2）：UART の BOOT の `from` を reset_reason（0〜6）、`to` を 0 とする。MQTT の event の
  BOOT は `detail` を `{"reset_reason": ...}` だけにする。event の Schema で BOOT の `detail.reset_reason` を7値の列挙で検査
- **INTERLOCK**（#39、CR-3）：UART の EVENT code 9 INTERLOCK_ACTIVE（warning）、10 INTERLOCK_RELEASED（info）
- **ttl_s と expired**（#40、CR-4）：作品01は expired を返さない。SET_FAN_OVERRIDE の ttl_s は 1〜600、範囲外は rejected
  （invalid_arg）。expires_at を過ぎた指令は作品03が UART に送らずに終え、要求元に request/ack（rejected、expired）で伝える
- **状態遷移と EVENT**（#41、CR-5）：1回の状態遷移につき EVENT は1件。遷移→コードの表を spec.md 第7章に追加
- 代役 mcu-c-01 の内部の値を作品01の設計書に合わせた（#32 に追記）：しきい値 45／60／3℃、CRITICAL・SENSOR_FAULT の承認、
  状態ごとのファンの下限と limited、SET_THRESHOLD の拒否条件、1遷移1件の EVENT
- **INTERLOCK の契機と順序**（#42）：状態遷移と同時のときは遷移の EVENT の後に送り、「1遷移1件」には数えない。
  SET_FAN_OVERRIDE を受けた時点で下限に引き上げたときは ACK の後に ACTIVE。RELEASED は引き上げが終わったときすべて
- **expires_at の検査**（#43）：最初の送信の前だけ。再送の前には検査しない（作品03も同じ）
- **applied**（#44）：SET_FAN_OVERRIDE の ACK を変換するとき、status が accepted・done のときだけ cmd/ack に
  `applied: {"level": n}` を付ける。rejected・failed では付けない（ACK の applied の値は問わない）。UART の変換にだけ適用し、
  JSON Schema（cmd_ack）は厳しくしない（MQTT で直接 ack を送る作品02の電文は変わらない）。v0.2.1 の変換からの変更だが、
  影響はゴールデン17番に閉じる（rejected・failed の ACK を含むゴールデンは17番だけ）

### ゴールデン（`uart/golden/`、27件 → 35件）

- 変更：17番 `ack_rejected_expired` → `ack_rejected_invalid_arg`（ttl_s 範囲外の SET_FAN_OVERRIDE への ACK。cmd_ref 0xFFFF、
  reason invalid_arg）。#44 により変換後の cmd/ack に applied は付きません。17番以外の01〜27番は変わっていません
- 追加（同じセッションの続き）：28 `cmd_set_fan_override_ttl_max`（ttl_s 600）、29 `cmd_get_diag_internal`、30 `diag`
  （`expected.diag` に各項目）、31 `ack_get_diag_done`（not_converted）、32 `state_reboot_watchdog`、33 `event_boot_watchdog`
  （reset_reason watchdog）、34 `event_interlock_active`、35 `event_interlock_released`

### 契約テスト・ライブラリ

- role mcu-c-01：`boot_reset_reason`・`get_diag`・`ttl_s_range`・`no_expired` を追加
- role gw-01：`golden_conversion` で DIAG と GET_DIAG の ACK も流し、MQTT に出ないことを確かめる。相手役は GET_DIAG に
  DIAG → ACK を返す
- `iotspec.uart`：`Diag`、`RESET_REASON`、`BridgeResult.diag`・`internal_ack`、`UartBridge.internal_frame`
- シナリオ（mcu_c_01）：`reset_reason`・`acks_at_s` を追加
- gw-01 簡易版：expires_at の扱い（#40）、`submit_internal`（GET_DIAG）、`diag_history`

### 各作品への影響

- 作品01：GET_DIAG に DIAG を返す、BOOT の from に reset_reason、INTERLOCK（code 9・10）を送る、ゴールデン35件。
  INTERLOCK は遷移の EVENT の後・ACK の後に送り、RELEASED は引き上げが終わったときすべてで送る（#42）。
  rejected・failed の ACK の applied は何を入れてもよい（#44。ゴールデン17番の期待値は applied なし）
- 作品03：DIAG・GET_DIAG の ACK を変換しない、BOOT の detail、expires_at を過ぎた指令を送らない（検査は最初の送信の前だけ。#43）。
  rejected・failed の SET_FAN_OVERRIDE の cmd/ack に applied を付けない（#44）
- 作品04：BOOT の `detail.reset_reason`、INTERLOCK_ACTIVE／RELEASED を mcu-c-01 からも受ける。
  cmd/ack の applied は accepted・done のときだけ付くので、rejected・failed の ack から出力の値を読まない（#44）
- 作品02：変更なし（MQTT で直接送る cmd/ack には #44 を適用しない）

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
