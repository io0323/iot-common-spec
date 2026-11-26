# Changelog

## v0.2.3（2026-11-26）

作品03（gw-01）の品質の件数と自己監視を telemetry で公開できるようにしました（作品03 の変更案
`docs/design/03-common-spec-cr-v0.2.3.md`、[docs/open-questions.md](docs/open-questions.md) #45）。
あわせて作品03 の Phase 4 で必要になった2点、EMERGENCY の解除の承認（#46）とルールの通知の重大度（#47）を含めました。
**トピックのメジャー版は `v1` のまま**で、変更は値の追加と、gw-01 のプロファイルの追加です。
v0.2.2 の電文は v0.2.3 の Schema でもすべて通ります。ただし RULE_FIRED は `detail.rule_id` が必須になりました
（第6章はもともと `detail.rule_id` としていたので、それを Schema で検査するようにしたものです）。

### 決定（#45）

- 単位に `count` を追加（起動からの累積件数、boot_id ごとに0から）。telemetry の Schema は unit が count のとき value を0以上の整数に制限する
- `profiles/gw-01.yaml` の channels：品質の件数8つ（count、60秒）と自己監視4つ（cpu_temp〔degC〕・cpu_usage・mem_used・disk_used〔pct〕、10秒）
- gw-01 は2種類を別々の telemetry で発行する（件数は quality good、取得に失敗した自己監視は fault）
- HOST_RESOURCE_HIGH・NORMAL の detail は `{channel, value, threshold}`

### 決定（#46：EMERGENCY の解除の承認）

- 作品04からの承認は request の `{target: "gw-01", op: "set_mode", args: {mode: "NORMAL"}}`（kind manual）
- `profiles/gw-01.yaml` に `modes: [NORMAL, ALERT, EMERGENCY]` を追加（profile の Schema の modes に大文字を許した）
- 作品03は、条件が回復していれば accepted → done（`cmd_id` には承認の処理を表す ULID）を返して NORMAL に戻し、
  `SYSTEM_MODE_CHANGED` を発行する。回復していなければ rejected（`unsafe`）。NORMAL 以外への set_mode は rejected（`not_permitted`）
- spec.md 第4章「EMERGENCY の解除の承認（v0.2.3）」と第6章の SYSTEM_MODE_CHANGED

### 決定（#47：ルールの通知の重大度）

- RULE_FIRED の重大度は info〜critical で、ルールの notify の level に従う（notify のないルールは info）。RULE_CLEARED は info のまま
- RULE_FIRED の detail は `{rule_id, message?}`。event の Schema で RULE_FIRED の `detail.rule_id`（空でない文字列）を必須にした

### そのほか

- spec.md：第2章（単位表、count は整数）、第3章（gw-01 の telemetry）、第5章（件数の公開とチャネルの意味）、第6章（HOST_RESOURCE の detail）
- samples：valid に `telemetry__gw_quality_counts`・`telemetry__gw_host_monitor`、invalid に `telemetry__count_negative`・`telemetry__count_fractional`
- samples（#46・#47）：valid に `request__approve_emergency_release`・`request_ack__approval_done`・`request_ack__approval_rejected_unsafe`・
  `request_ack__set_mode_emergency_not_permitted`・`event__rule_fired_critical`・`event__rule_fired_info_without_notify`、
  invalid に `request__set_mode_without_mode`・`event__rule_fired_without_detail`・`event__rule_fired_without_rule_id`・
  `event__rule_fired_message_not_string`
- gw-01 簡易版（#46）：システムのモード（シナリオの `initial_mode`）と、gw-01 宛ての set_mode を承認として扱う振る舞い。
  シナリオに `emergency_condition_until_s`（その時刻まで条件が回復していない）を追加。シナリオ `emergency_approval` で
  app-01 の代役から承認を送れる
- gw-01 簡易版：state の channels をプロファイルから出し、件数（60秒）と自己監視（10秒、決まった値）の telemetry を発行する
- シナリオ：signals を全チャネル分求めるのは、信号から値を作る代役（mcu_cpp_01・mcu_c_01）だけにした
- UART・ゴールデンの変更はなし（ゴールデンは1バイトも変わらない）

### 各作品への影響

- 作品03：gw-01 の telemetry（件数60秒・自己監視10秒）を発行する。gw-01 の state の channels に12チャネルが載る。
  gw-01 宛ての set_mode NORMAL を EMERGENCY の解除の承認として扱う（#46）。RULE_FIRED の重大度を notify の level にし、
  detail に rule_id と message を入れる（#47）
- 作品04：単位 `count` と gw-01 の telemetry を取り込んで表示する。HOST_RESOURCE の detail を読む。
  EMERGENCY の解除の承認を set_mode NORMAL の request で送り、rejected（unsafe・not_permitted）を表示する（#46）。
  RULE_FIRED を重大度と message で通知する（#47）
- 作品01・02：なし

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
