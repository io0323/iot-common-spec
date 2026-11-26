# 共通仕様 v0.2（iot-common-spec v0.2.2）：作品01〜04 の通信・データ仕様

2026-11-20 · @io（v0.2.2：2026-11-26）

## 1. 目的・適用範囲・版の方針

作品01〜04がやり取りするメッセージの形式と、指令・要求の約束事、データ品質の扱いを定める。各作品の設計書と食い違う場合は本書が優先し、設計書を本書に合わせて直す。作品01の着手前に本版を確定させる。

- 対象：MQTTのトピックとJSONペイロード、作品01⇔作品03のUARTフレーム、共通シミュレータと契約テスト
- 対象外：各作品の内部設計、作品04のREST API（作品04の設計書で定める）
- 仕様の実体：`iot-common-spec` リポジトリのJSON Schema、サンプル電文、UARTのゴールデンフレーム。本書はその説明であり、食い違いがあればSchemaを正とする

### 版の方針

- 本書の版（v0.2）と、トピックに含めるメジャー版（`v1`）は別物。v0.2は項目の追加だけなのでトピックは `v1` のまま
- 実体のリリースは v0.2.x で数える。現在は **v0.2.2**（v0.2.1 に UART の DIAG 種別、BOOT のリセット要因、INTERLOCK のコード、ttl_s の範囲、状態遷移と EVENT の表を追加。いずれも項目の追加で、トピックは `v1` のまま。CHANGELOG.md）
- 受信側は知らない項目を無視する（前方互換）。必須項目の削除や意味の変更をするときだけトピックを `v2` に上げる
- 各作品はREADMEに対応する仕様の版を書き、`iot-common-spec` をタグ指定で取り込む
- 変更は `iot-common-spec` へのPull Requestで行い、影響する作品を説明に書く

## 2. トピック体系と命名

トピックは `iot/v1/{site}/{device_id}/{kind}`。`{device_id}` は常に「発信元」または「宛先」のデバイスを表す。

| kind | 発行元 → 購読元 | `{device_id}` | QoS | retain | 内容 |
|---|---|---|---|---|---|
| `telemetry` | 各デバイス → 03、04 | 発信元 | 0 | しない | 周期的な計測値 |
| `event` | 各デバイス、03 → 03、04 | 発信元 | 1 | しない | 異常・復旧・状態変化（第6章） |
| `state` | 各デバイス、03 → 03、04 | 発信元 | 1 | する | 稼働状態と機器一覧。LWTでoffline |
| `cmd` | 03 → 各デバイス | 宛先 | 1 | しない | 指令。03以外は発行できない（ACL） |
| `cmd/ack` | 各デバイス → 03 | 発信元 | 1 | しない | 指令の受理・結果 |
| `request` | 04 → 03 | 発信元（`app-01`） | 1 | しない | 手動操作・スケジュール実行の要求 |
| `request/ack` | 03 → 04 | 宛先（`app-01`） | 1 | しない | 要求の受理・結果（**v0.2で追加**） |

### 命名規則

| 対象 | 規則 | 例 |
|---|---|---|
| site | 英小文字、数字、ハイフン | `home`, `greenhouse-1` |
| device_id | `{種別}-{言語}-{連番}`。予約済み：`mcu-c-01`、`mcu-cpp-01`、`gw-01`、`app-01` | `mcu-cpp-02` |
| channel（計測値） | デバイス内で一意のsnake_case | `temp_board`, `temp_air`, `humidity`, `lux`, `soil` |
| actuator | デバイス内で一意のsnake_case | `fan_main`, `led_grow`, `pump`, `buzzer` |
| ID（msg_id・cmd_id・request_id） | ULID（26文字）。生成側が採番する | `01JD3Q8Z5K7M2N4P6R8T0V2X4Y` |

### 単位

| unit | 意味 | unit | 意味 |
|---|---|---|---|
| `degC` | 温度（℃） | `hPa` | 気圧 |
| `pct_rh` | 相対湿度（%） | `lux` | 照度 |
| `pct` | 割合（土壌水分、CPU使用率など） | `bytes` | 容量 |
| `level_pm` | アクチュエーター出力（0–1000‰） | `rpm` | 回転数 |

JSON上の値は浮動小数点数で、上記の単位そのものを表す（温度なら 41.25）。固定小数への変換は各デバイスの内部で行う。

## 3. 共通エンベロープとペイロード

### 共通エンベロープ

すべてのMQTTメッセージは次の外側を持ち、種別ごとの内容を `payload` に入れる。

| 項目 | 型 | 必須 | 説明 |
|---|---|---|---|
| `schema` | string | 必須 | `iot.v1.{kind}`。例：`iot.v1.telemetry`、`iot.v1.cmd_ack` |
| `msg_id` | ULID | 必須 | メッセージごとに一意。重複排除に使う（第5章） |
| `device_id` | string | 必須 | 発信元。UART経由の場合も元のデバイス（`mcu-c-01`） |
| `seq` | uint32 | 必須 | 発信元・kindごとの連番。再起動で0に戻る（`boot_id` で区別） |
| `boot_id` | string | 必須 | 起動ごとに変わる短いID。連番のリセットと欠番を区別する。全デバイスで16進小文字8桁（32ビット）。同じデバイスの前回の起動と高い確率で異なればよく、全体での一意性は求めない（v0.2.1で確定） |
| `ts` | string | 必須 | 計測・発生時刻。UTCのISO 8601（ミリ秒） |
| `ts_source` | enum | 必須 | `device`（時刻同期済み）／`gateway`（03が受信時刻で付与） |
| `payload` | object | 必須 | 種別ごとの内容 |
| uptime_ms | uint32 | 任意 | 発信元の起動後経過ミリ秒。時刻を持たないデバイスの元の時間情報を残す（v0.2で追加） |

### 種別ごとの内容

| kind | payloadの主な項目 |
|---|---|
| telemetry | `readings[]`：`{channel, value, unit, quality}`。`actuators[]`：`{id, level, owner}`（任意。`owner` も任意で、値は `interlock`・`safety`・`manual`・`auto`・`schedule`・`local`・`none`。v0.2.1で確定）。`device_state`（任意の文字列。デバイス内の状態名。v0.2.1で追加） |
| event | `code`, `severity`, `message`（任意）, `channel`（任意）, `value`（任意）, `detail`（任意のobject） |
| state | `status`（online／offline）, `mode`, `channels[]`：`{channel, unit, period_ms}`, `actuators[]`：`{id, min, max, max_on_s}`, `fw_version`。`bridged_by`（任意。作品03がUARTデバイスの代わりに発行するstateに付ける、作品03の `device_id`。v0.2.1で追加） |
| cmd | 下記 |
| cmd/ack | 下記 |
| request | 下記 |
| request/ack | 下記 |

`state` に機器一覧と計測周期（`period_ms`）を含める。受信側はこれで機器を自動登録し、stale判定の基準にも使う。

### cmd と cmd/ack

```json
{"schema": "iot.v1.cmd", "...": "エンベロープ省略",
 "payload": {
   "cmd_id": "01JD3R0A...", "op": "set_actuator",
   "args": {"id": "fan_main", "level": 800},
   "priority": "auto", "lease_s": 60,
   "origin": {"kind": "rule", "ref": "cool-on-high-temp"},
   "expires_at": "2026-11-20T06:01:12.000Z"}}

{"schema": "iot.v1.cmd_ack",
 "payload": {
   "cmd_id": "01JD3R0A...", "status": "done",
   "limited": false, "applied": {"level": 800}, "reason": null}}
```

- `op`：`set_actuator`、`release`、`set_mode`、`set_param`、`get_status`。`level` は0–1000‰
- `args`（v0.2.1で確定）：`set_actuator` は `id` と `level`、`release` は `id`、`set_mode` は `mode`（文字列。取りうる値はデバイスプロファイルの `modes` で定める）が必須。`get_status` は引数なし。`set_param` の引数は未定義で、受けたデバイスは `unknown_op` で拒否してよい
- telemetry の `owner`（v0.2.1で確定）：`interlock`＝デバイスの安全制限が効いている、`safety`〜`schedule`＝03の指令のリース中で値はその `priority`、`local`＝デバイス自身のルールや本体ボタン、`none`＝既定値
- `status`：`accepted`（受理、実行中）、`done`、`rejected`、`failed`。即時に終わる指令は `accepted` を飛ばして `done` を返してよい
- `limited: true`：デバイス側の安全制限で値を丸めた。実際の値を `applied` に入れる
- `reason`（拒否・失敗時）：`unsafe`、`invalid_arg`、`unknown_op`、`unknown_target`、`expired`、`busy`、`internal`
- `expires_at` を過ぎて届いた指令は実行せず `rejected`（`expired`）を返す

### request と request/ack

```json
{"schema": "iot.v1.request",
 "payload": {
   "request_id": "01JD3S...", "kind": "manual",
   "target": "mcu-cpp-01", "op": "set_actuator",
   "args": {"id": "pump", "level": 1000}, "lease_s": 20,
   "schedule": {"id": 12, "planned_at": "2026-11-20T22:00:00.000Z"}}}

{"schema": "iot.v1.request_ack",
 "payload": {
   "request_id": "01JD3S...", "status": "accepted",
   "cmd_id": "01JD3R...", "reason": null}}
```

- `kind`：`manual`（優先度manual）、`schedule`（優先度schedule。`schedule` 項目必須）
- `status`：`accepted`、`rejected`、`done`、`failed`。`reason` は上記に加え `not_permitted`、`lower_priority`、`target_offline`、`emergency_mode`、`gateway_timeout`

## 4. 指令と要求

アクチュエーターへの指令（cmd）を出せるのは作品03だけ。作品04は要求（request）を出し、03が調停して指令に変える。どの区間も「同じIDで再送し、受ける側がIDで重複を排除する」という同じ約束に揃える。

> [埋め込み図: 要求から指令完了までのシーケンス]（Markdown 書き出しでは省略。原本: Claude Docs の共通仕様 v0.2）

### 区間ごとの約束

| 区間 | 送る側の待ち時間 | 再送 | 受ける側の重複排除 | 最終的な失敗 |
|---|---|---|---|---|
| 04 → 03（request） | 最初の応答まで5秒 | 同じ `request_id` で最大3回 | 03は `request_id` を1時間記憶し、重複には現在の状態を返す | 応答がなければ、04が `failed`（`gateway_timeout`）とする |
| 03 → 02（cmd） | ackまで2秒 | 同じ `cmd_id` で最大3回 | 02は直近64件・10分の `cmd_id` と結果を記憶し、重複には前回のackを返す | 03がTIMED_OUTとし、要求元に `failed` |
| 03 → 01（UART CMD） | ACKまで2秒 | 同じ `cmd_ref` で最大3回 | 01は直近16件の `cmd_ref` と結果を記憶し、重複には前回のACKを返す | 同上 |

重複した `cmd_id` に前回のackを返すときは、payloadを前回と同じにし、エンベロープ（`msg_id`・`seq`・`ts`）は新しく作る。前回の電文をそのまま再送すると、受信側の `seq` による重複排除（第5章）で捨てられることがあるため（v0.2.1で明記）。

要求は受理後、指令の結果が出た時点で `done`／`failed` を返す。04は受理から60秒以内に最終結果が来なければ `failed`（`gateway_timeout`）とする。スケジュールによる要求の `request_id` は「予定ID＋予定時刻」から決定的に作り、作品04が再起動して同じ予定を送り直して。03が重複として弾く。

### IDの種類と生成する作品

| ID | 生成する作品 | 用途 | 一意の範囲 |
|---|---|---|---|
| `msg_id` | 各メッセージの発信元。UARTから変換したものは03 | ログの追跡と相関 | 全体 |
| `boot_id` ＋ `seq` | 発信元 | 欠損と重複の判定（第5章） | 発信元・kindごと |
| `request_id` | 04。手動操作は通常のULID、スケジュール実行は時刻部＝予定時刻、乱数部＝SHA-256(schedule_id, planned_at) の先頭80ビットで決定的に作る。ハッシュの入力はUTF-8の `"{schedule_id}|{planned_at}"`（schedule_idはゼロ埋めなしの10進、planned_atはUTC・ISO 8601・ミリ秒・末尾Z。例：`12|2026-11-20T22:00:00.000Z`）。v0.2.1で明記 | 要求の再送と重複排除 | 全体 |
| `cmd_id` | 03 | 指令の再送と重複排除、要求との紐付け | 全体 |
| `cmd_ref` | 03（UART専用。`cmd_id` と1対1） | UARTで `cmd_id` の代わりに使う短い番号 | 作品01との間。65,536で一周 |

### 重複排除の記憶が再送期間を覆うことの確認

受ける側の記憶は、送る側が同じIDを再送し続ける最大の時間より長くなければならない。

| 区間 | 再送が続く最大時間 | 受ける側の記憶 | 判定 |
|---|---|---|---|
| 04 → 03 | 5秒 ×（1＋3回）＝20秒 | 1時間 | 十分 |
| 03 → 02 | 2秒 ×（1＋3回）＝8秒 | 64件かつ10分 | 十分。03から02への指令は多くても毎秒数件で、64件は8秒分を大きく上回る |
| 03 → 01 | 8秒 | 16件 | **条件付きで十分**。03は01への指令を毎秒1件まで、未完了は同時4件までに制限する（16件＝16秒分） |
| MQTTの再配送（QoS 1） | 通常は数秒。再接続をまたぐとその間 | 発信元・kindごとに `seq` 1,024個 | 十分。再配送されるのは確認待ちの最大32件だけで、発信元・kindごと毎秒10件の上限なら1,024個は100秒以上にあたる |

作品01への「毎秒1件」には再送も含める。ACKが返らない指令の再送は、新しい指令より先に送る（v0.2.1で明記）。

`cmd_ref` は65,536で一周するが、毎秒1件の制限なら一周に18時間以上かかり、16件の記憶と衝突しない。

### 送信量の上限（重複排除の前提）

- 発信元・kindごとのMQTT発行は、平均で毎秒10件、瞬間的には毎秒50件までとする。作品03・04の非機能要件にある毎秒200件は、全発信元の合計を指す
- MQTTクライアントの確認待ち（QoS 1の未確認送信）の上限は32件。再配送されるのはこの範囲に限られるので、重複は「最新の連番から32以内」にしか現れない
- 受信側は発信元・kindごとに直近1,024個の連番を記憶する。上の2つの上限に対して、32件の30倍以上、時間にして100秒以上の余裕がある
- 将来、毎秒数百件を送る発信元を追加する場合は、「送信上限 × 想定する再配送までの時間」で記憶数を見直す。上限を超えて送る発信元は契約テストで検出する

### リース

- すべての `set_actuator` は有効期限（`lease_s`）を持つ。既定60秒、上限600秒。cmd の `lease_s` は省略でき、省略時は60秒とみなす。`lease_s` が意味を持つのは `set_actuator` だけで、他の op では省略してよく、付いていても無視する（v0.2.1で確定）
- デバイスは期限が切れたらその指令を破棄し、自分の優先度の下位（手動操作・ローカルルール・既定値）に戻る
- 制御を続ける場合、03は lease_s の3分の1ごと（60秒なら20秒ごと）に新しい `cmd_id` で同じ対象に送り直す。デバイスは同じ対象への新しい指令で古い指令を置き換える
- 制御をやめるときは `release` を送る。届かなくても期限で自然に解除される
- 絶対時刻を持たない作品01は `ttl_s`（受信時点からの秒数）で扱う
- リースはデバイスが指令を受信した時刻から数え、「受信時刻＋`lease_s`」ちょうどで切れる。`expires_at` はこれとは別の検査で、`expires_at` を過ぎて届いた指令は実行しない（v0.2.1で明記）
- `set_actuator` 以外の op に付いた `lease_s` は値によらず無視する。JSON Schemaも `lease_s` の範囲を `set_actuator` のときだけ検査する（v0.2.1で明記）

リースは「その間、上位が制御する権利」の期限であり、「その間、出力を続けてよい」という意味ではない。デバイスの安全制約はリースより常に優先する。たとえば `lease_s: 600` でポンプを動かしても、作品02は連続30秒で止め、ackに `limited: true` を付ける。更新が2回続けて届かなくても期限には間に合うが、3回途絶えると期限が切れ、デバイスは自律制御に戻る。

### 優先度

| 順位 | 値 | 発生元 |
|---|---|---|
| 0（最優先） | — | デバイス内の安全インターロック。外からは解除できない |
| 1 | `safety` | 03の安全ルール、EMERGENCYモード |
| 2 | `manual` | 04の手動操作、`gwctl` |
| 3 | `auto` | 03の通常ルール |
| 4 | `schedule` | 04のスケジュール |

同じ優先度では新しい方が勝つ。優先度の調停は03が行い、デバイスは届いた指令をリース付きの「上位指令」として扱うだけでよい。`priority` をcmdに含めるのは、デバイス側のログと表示のためである。

## 5. データ品質

品質の問題は「発信元が付けるもの」と「受信側が判定するもの」に分け、どの作品も同じ基準で扱う。原則は、疑わしい値で制御しないことと、疑わしい値も捨てずに記録して分かるようにすること。

| 問題 | 判定する側 | 判定基準 | 制御（03・デバイス）での扱い | 記録・表示（04）での扱い |
|---|---|---|---|---|
| センサー故障 | 発信元 | 各デバイスの故障検知（断線・無応答など） | `quality: fault`。ルールの判定に使わない | 保存し、グラフでは別色の印。平均などの統計から除外 |
| 古い値（stale） | 受信側 | 最終更新から `period_ms` の3倍を超えた | 判定に使わない。その値に依存するルールは「不明」として新しい指令を出さず、リースの更新も止める | 最新値カードに「更新停止」を表示 |
| 欠損 | 受信側 | 同じ `boot_id` 内での `seq` の飛び | 影響なし（最新値で判定） | 欠損件数を集約に残し、グラフでは欠損区間を線でつながない |
| 重複 | 受信側 | 同じ（`device_id`, `boot_id`, kind, `seq`）。発信元・kindごとに直近1,024個の `seq` を記憶する。`msg_id` はログの追跡用で、重複判定には使わない | 2通目以降を破棄 | 2通目以降を破棄 |
| 時刻ずれ | 受信側 | `ts_source: device` で、受信時刻との差が±5分超 | `ts` を受信時刻に置き換え `ts_source: gateway` とする。`CLOCK_SKEW` イベントを発行（同一デバイス1時間1回まで） | 置き換え後の時刻で保存し、元の `ts` も記録 |
| 遅延到着 | 受信側 | `ts` が受信時刻より過去で、差が周期の3倍超（5分以内） | 最新値を更新しない（より新しい値を上書きしない） | 時系列としては保存する |
| 値の形式不正 | 受信側 | JSON Schema違反、未知の単位 | 破棄してカウント | 破棄してカウント |

`bridged_by` を持つstate（作品03がUARTデバイスの代わりに発行したもの）について、受信側は `bridged_by` のデバイスがofflineなら、配下のデバイスも状態不明として扱う（v0.2.1で明記）。

`status: offline` のstate（LWTと正常終了時のoffline、作品03が代わりに発行するUARTデバイスのoffline）は、接続時に登録するので `seq` のカウンタを進めず、登録時点の値（次に使う `seq`）を入れる。受信側はこの電文を重複・欠損・遅延の判定から外して必ず受け入れ、`seq` の記憶にも加えない（v0.2.1で明記）。

破棄や置き換えの件数は、各作品とも状態として公開する（03は `gw-01` のtelemetry、04は `/health`）。

## 6. イベントコードと重大度

重大度は `info`／`warning`／`critical` の3段階。異常と復旧は対になるコードを使い、04は対で通知をまとめる。

| コード | 重大度 | 発行元 | 意味 | 対になるコード |
|---|---|---|---|---|
| `BOOT` | info | 全て | 起動。`detail.reset_reason` に再起動要因（unknown／power_on／pin／watchdog／software／brownout／low_power。v0.2.2で列挙） | — |
| `STATE_CHANGED` | info〜critical | 01、02 | デバイス内の状態遷移。重大度は遷移先に従う | — |
| `THRESHOLD_EXCEEDED` | warning／critical | 01、02、03 | しきい値超過 | `THRESHOLD_CLEARED` |
| `SENSOR_FAULT` | critical | 01、02 | センサー故障 | `SENSOR_RECOVERED` |
| `INTERLOCK_ACTIVE` | warning | 01、02 | 安全制限が動作（出力を丸めた、強制停止した）。01はUARTのEVENT code 9・10で送る（v0.2.2） | `INTERLOCK_RELEASED` |
| `MODE_CHANGED` | info | 02 | 自動／手動／省電力の切替 | — |
| `UPSTREAM_LOST` | warning | 01、02 | 上位（03）との接続断。自律制御で継続中 | `UPSTREAM_RESTORED` |
| `DEVICE_OFFLINE` | warning | 03 | デバイスの切断を確定 | `DEVICE_ONLINE` |
| `SYSTEM_MODE_CHANGED` | info〜critical | 03 | NORMAL／ALERT／EMERGENCYの切替 | — |
| `RULE_FIRED` | info | 03 | ルール発火（`detail.rule_id`） | `RULE_CLEARED` |
| `COMMAND_FAILED` | warning | 03 | 指令が失敗・タイムアウト | — |
| `CLOCK_SKEW` | warning | 03、04 | 時刻ずれを検出（第5章） | — |
| `DATA_DROPPED` | warning | 03、04 | キュー満杯等でデータを破棄（件数を `detail` に） | — |
| `HOST_RESOURCE_HIGH` | warning／critical | 03 | Raspberry Piの温度・メモリ・ストレージのしきい値超過 | `HOST_RESOURCE_NORMAL` |

対になる復旧コード（`THRESHOLD_CLEARED`・`SENSOR_RECOVERED`・`INTERLOCK_RELEASED`・`UPSTREAM_RESTORED`・`DEVICE_ONLINE`・`RULE_CLEARED`・`HOST_RESOURCE_NORMAL`）の重大度は `info` とする（v0.2.1で明記）。

コードを追加するときは本表とサンプル電文を同時に更新する。JSON Schemaではコードを列挙せず形式（英大文字とアンダースコア）だけを検査し、受信側は未知のコードも破棄せず重大度に従って扱う。

## 7. UARTフレーム（作品01 ⇔ 作品03）

作品01はJSONを扱わず、固定長に近いバイナリフレームで通信する。共通エンベロープとの変換は作品03だけが行う。

### フレーム構造

- 物理層：115200 bps、8N1、フロー制御なし
- 構造：`[種別 u8][seq u16][長さ u8][ペイロード 0–200B][CRC-16/CCITT-FALSE u16]` をCOBSで符号化し、末尾に `0x00`
- リトルエンディアン。CRCは種別からペイロード末尾までを対象とする
- `seq` は方向・フレーム種別ごとの連番（v0.2.1で変更。v0.2では方向ごと）。作品03はこれを種別ごとの欠番検知に使い、共通エンベロープの `seq` には、16ビットのUARTの連番に一周ごとに65,536を加えて32ビットに広げた値を、対応するkindの `seq` としてそのまま使う（TELEMETRY→telemetry、EVENT→event、STATE→state、ACK→cmd/ack）。HEARTBEATとCMDはエンベロープを作らないので、生存監視と欠番の計数にだけ使う。種別ごとの連番なので、ほかの種別のフレームを挟んでもkindごとの `seq` に偽の欠番は出ず、本当の欠番の情報は変換後も保たれる

### 種別と対応

| 種別 | 値 | 向き | ペイロード | MQTT側 |
|---|---|---|---|---|
| TELEMETRY | 0x01 | 01→03 | `uptime_ms:u32, temp_c100:i16, quality:u8, state:u8, fan_pm:u16` | `telemetry`（`temp_board` と `fan_main`） |
| EVENT | 0x02 | 01→03 | `uptime_ms:u32, code:u8, severity:u8, from:u8, to:u8, temp_c100:i16` | `event`（コードは下表で変換） |
| HEARTBEAT | 0x03 | 双方向 | `uptime_ms:u32` | なし（生存監視のみ） |
| STATE | 0x04 | 01→03 | `fw_version:u16, boot_id:u32, period_ms:u16` | `state`（03が `mcu-c-01` 名義でretain発行） |
| DIAG | 0x05 | 01→03 | `uptime_ms:u32, reset_reason:u8, reset_count:u16, rx_crc_errors:u16, rx_frame_errors:u16, max_loop_us:u16, overruns:u16`（15バイト。v0.2.2で追加） | なし（03が履歴に残す） |
| CMD | 0x10 | 03→01 | `cmd_ref:u16, op:u8, ttl_s:u16, args:可変長` | `cmd` |
| ACK | 0x11 | 01→03 | `cmd_ref:u16, status:u8, reason:u8, limited:u8, applied:u16` | `cmd/ack` |

- HEARTBEATは双方向1秒周期。3秒途絶えたら相手を切断とみなす。作品03はUARTデバイスのHEARTBEATが3秒途絶えたら、そのデバイス名義のofflineのstateを `bridged_by` 付き・retainで発行する（`seq` は進めない。v0.2.1で明記）。作品01は10秒途絶えたら手動指定を解除し `UPSTREAM_LOST` を送る
- `cmd_ref` は作品03が採番する16ビットの参照番号。作品03は `cmd_ref` ⇔ `cmd_id`（ULID）の対応表を持ち、再送では同じ `cmd_ref` を使う
- `ttl_s` はリース秒数。絶対時刻を持たない作品01は受信時点から数える。SET_FAN_OVERRIDE の `ttl_s` は1〜600で、範囲外は rejected（invalid_arg）（v0.2.2）
- `op`：0x01 SET_FAN_OVERRIDE、0x02 CLEAR_OVERRIDE、0x03 SET_THRESHOLD、0x10 GET_STATUS、0x11 GET_DIAG。汎用指令との対応は作品03の `devices.yaml` で定義する。GET_DIAG は作品03の内部の操作で、汎用指令との対応はない（v0.2.2）
- 数値の変換：`temp_c100` ÷ 100 = `degC`、`fan_pm` はそのまま `level_pm`。`uptime_ms` は作品03が受信時刻から `ts` を求めるのに使う（`ts_source: gateway`）

### 変換で失わない情報

| 情報 | UART側 | MQTT側（作品03が変換） |
|---|---|---|
| 起動の区別 | STATEの `boot_id:u32` | `boot_id`（16進数8桁の文字列）。作品01は起動後の最初のフレームとしてSTATEを送り、以後60秒ごとにも送る |
| 連番 | `seq:u16`（方向・種別ごと） | 対応するkindの `seq`（一周回数を加えた32ビット、上記） |
| 時刻 | `uptime_ms:u32` | `ts` は受信時刻（`ts_source: gateway`）。元の `uptime_ms` もエンベロープに残す |
| 品質 | `quality:u8` | `quality`（値の対応表どおり） |

### 変換の決め事（v0.2.1）

- **デバイスプロファイル**：STATEフレームにないstateの必須項目（`mode`・`channels[]`・`actuators[]`）は、デバイスプロファイル `profiles/{device_id}.yaml` から補う。`channels[].period_ms` はSTATEの `period_ms` を優先する。`mode` は直近のTELEMETRYの `state` を状態名にしたもの（受信前は `INIT`）。状態番号と状態名の対応、汎用指令とUARTのopの対応もプロファイルで定める
- **状態名**：TELEMETRYの `state` はtelemetryの任意項目 `device_state` に、EVENTの `from`・`to` はeventの `detail.from_state`・`detail.to_state` に、いずれもプロファイルの状態名で入れる
- **fw_version**：`fw_version:u16` は上位バイト＝メジャー、下位バイト＝マイナーで `"{major}.{minor}"` とする（例：`0x0102` → `"1.2"`）
- **applied**：ACKの `applied:u16` は、出力を扱うop（SET_FAN_OVERRIDE）のときだけcmd/ackの `applied` に `{"level": n}` として入れる。それ以外のopでは `applied` を付けない（v0.2.2で、SET_FAN_OVERRIDE でも status が accepted・done のときだけに改めた。下の「変換の決め事（v0.2.2）」）
- **ttl_s**：リースを持たないop（CLEAR_OVERRIDE・SET_THRESHOLD・GET_STATUS・GET_DIAG）のCMDでは `ttl_s` を0にし、受けた側は無視する
- **変換できない値**：対応表やプロファイルにない値を含むフレームと、変換結果がJSON Schemaに合わないフレームは、フレームごと破棄して理由ごとに件数を数える（第5章「値の形式不正」）。作品01はrejected・failedのACKでは必ず `reason` を入れる
- **STATE待ち**：起動直後と再起動の検知後は、新しいSTATEを受けるまでTELEMETRY・EVENT・ACKを変換せずに破棄する（TELEMETRYの破棄件数は記録する）。このため作品01は起動後に STATE → BOOTイベント の順に送る
- **CMDのargs**：SET_FAN_OVERRIDEは `duty_pm:u16`、SET_THRESHOLDは `warn_c100:i16, crit_c100:i16, hyst_c100:i16`、CLEAR_OVERRIDE・GET_STATUS・GET_DIAGは引数なし（`set_param` の引数は未定）

STATEが破損で失われた場合に備え、作品03は `uptime_ms` が減ったら再起動とみなしてGET_STATUSでSTATEを求める。新しいSTATEを受けるまでのTELEMETRYは変換せずに破棄し、件数を記録する（古い `boot_id` を付けたまま送らないため）。

### 変換の決め事（v0.2.2）

- **DIAG**（open-questions #37）：作品01は GET_DIAG に DIAG → ACK(done) の順で返す。`rx_frame_errors` はCOBS・長さ・種別・受信あふれで捨てた件数の合計。u16の項目はすべて `0xFFFF` で飽和させ、一周させない。作品03はDIAGをMQTTに変換せず（破棄理由 `not_converted` で数える）、受信時刻と `boot_id` を付けて履歴に残す。STATE待ちの間のDIAGは他の種別と同じく破棄する（`awaiting_state`）。ペイロード長が15バイトでないDIAGは、他の種別と同じ理由（`payload_size`）で破棄する
- **GET_DIAGのACK**（#37）：GET_DIAG は作品03の内部の操作で、汎用指令との対応はない。作品03はその `cmd_ref` を覚えておき、ACKを `cmd/ack` に変換しない（`not_converted`）
- **BOOTのリセット要因**（#38）：UARTのEVENT code 1（BOOT）に限り、`from` を reset_reason（下表）、`to` を 0 とする。MQTTのeventでは、BOOTのときに限り `detail` を `{"reset_reason": "<文字列>"}` だけにする（`from_state`・`to_state` は付けない）。`to` が0でないBOOTと、表にないreset_reasonは変換できない値として破棄する
- **INTERLOCK**（#39）：EVENT code 9 INTERLOCK_ACTIVE（warning）、10 INTERLOCK_RELEASED（info）。`from`・`to` はその時点の状態番号（同じ値）で、MQTTでは他のEVENTと同じく `detail.from_state`・`detail.to_state` に入れる。作品01は、状態によるファンの下限で手動指定の値を引き上げ始めたときに ACTIVE、引き上げをやめたとき（指定の解除・期限切れ・上位断、状態が下がった、下限以上の指定に置き換わった）に RELEASED を送る
- **applied（v0.2.2で変更）**（#44）：SET_FAN_OVERRIDE のACKを変換するとき、`status` が accepted・done のときだけcmd/ackに `applied: {"level": n}` を付ける。rejected・failed では `applied` を付けない（指令は実行されていないので、受信側が「ファンが0になった」と読まないようにする）。このときACKの `applied:u16` の値は問わない（作品01は何を入れてもよい）。この決め事はUARTの変換（作品03）にだけ適用する。MQTTで直接cmd/ackを送るデバイス（作品02）の電文は対象外で、JSON Schemaも rejected・failed の `applied` を禁止しない
- **ttl_sとexpired**（#40）：作品01は `expired` を返さない（`reason` の値としては残す）。SET_FAN_OVERRIDE の `ttl_s` は1〜600で、範囲外は rejected（invalid_arg）。`expires_at` を過ぎた指令は、作品03がUARTに送らずに終える。このとき作品03は `mcu-c-01` 名義の `cmd/ack` を作らない（`cmd/ack` の `seq` はUARTのACKから作るため）。結果は要求元への `request/ack`（rejected、reason `expired`）と履歴で伝える。期限の検査は最初の送信の前だけに行い、再送の前には行わない。1回送ったあとは作品01が実行している可能性があり、再送の途中で期限を過ぎても作品01のリース（`ttl_s`）が出力を止めるため（#43、v0.2.2で確定）

### 状態遷移とEVENT（v0.2.2）

1回の状態遷移につき、作品01が送るEVENTは1件（open-questions #41）。`from`・`to` には遷移の前後の状態番号を入れる。状態番号はプロファイル（0 INIT、1 NORMAL、2 WARNING、3 CRITICAL、4 SENSOR_FAULT）。

| 遷移 | code | severity |
|---|---|---|
| NORMAL → WARNING | 3 THRESHOLD_EXCEEDED | warning |
| NORMAL・WARNING → CRITICAL | 3 THRESHOLD_EXCEEDED | critical |
| WARNING → NORMAL、CRITICAL → WARNING・NORMAL（承認後） | 4 THRESHOLD_CLEARED | info |
| 任意の状態 → SENSOR_FAULT | 5 SENSOR_FAULT | critical |
| SENSOR_FAULT → 他の状態（承認後） | 6 SENSOR_RECOVERED | info |
| INIT → NORMAL・WARNING・CRITICAL | 2 STATE_CHANGED | 遷移先に従う（NORMAL は info、WARNING は warning、CRITICAL は critical） |
| 起動（INIT を抜けるとき、STATE の直後） | 1 BOOT | info（from は reset_reason、to は 0） |
| 上位断・復帰 | 7 UPSTREAM_LOST／8 UPSTREAM_RESTORED | warning／info（from・to は現在の状態） |
| 下限による手動指定の引き上げの開始・終了 | 9 INTERLOCK_ACTIVE／10 INTERLOCK_RELEASED | warning／info（from・to は現在の状態） |

BOOT・UPSTREAM・INTERLOCK は状態遷移ではないので、上の「1件」に数えない。状態遷移と同時に下限での引き上げが始まる・終わるときは、遷移のEVENTの後にINTERLOCKを送る（from・to は遷移後の状態）。SET_FAN_OVERRIDE を受けた時点で下限に引き上げたときは、ACKの後にINTERLOCK_ACTIVEを送る（ACKは指令への応答なので先に返す）。INTERLOCK_RELEASED は、下限による引き上げが終わったとき（指定の解除・期限切れ・上位断、状態が下がった、下限以上の指定に置き換わった）のすべてで送る（open-questions #42、v0.2.2で確定）。

### 値の対応表

| UART上の値 | 共通仕様上の値 |
|---|---|
| quality 0／1／2 | good／stale／fault |
| status 0／1／2／3 | accepted／done／rejected／failed |
| reason 0／1／2／3／4／5 | null／unsafe／invalid_arg／unknown_op／expired／internal |
| EVENT code 1／2／3／4／5／6／7／8／9／10 | BOOT／STATE_CHANGED／THRESHOLD_EXCEEDED／THRESHOLD_CLEARED／SENSOR_FAULT／SENSOR_RECOVERED／UPSTREAM_LOST／UPSTREAM_RESTORED／INTERLOCK_ACTIVE／INTERLOCK_RELEASED（8はv0.2.1、9・10はv0.2.2で追加） |
| reset_reason 0／1／2／3／4／5／6 | unknown／power_on／pin／watchdog／software／brownout／low_power（BOOTの `from` とDIAGの `reset_reason`。v0.2.2で追加） |
| severity 0／1／2 | info／warning／critical |

ゴールデンフレーム（符号化済みの16進列と、対応するJSON）を `uart/golden/` に置き、作品01の符号化と作品03の復号の両方をこれでテストする。

## 8. 共通シミュレータと契約テスト

シミュレータは4作品で別々に作らず、`iot-common-spec` に1つだけ置く。どの作品も「自分以外の3作品」をこれで代用し、実機なしで4作品をつないだ結合テストまでできるようにする。

### リポジトリ構成

```
iot-common-spec/
├── docs/spec.md               # 本書の内容
├── schemas/                   # JSON Schema（kindごと）
├── samples/                   # 正常・異常のサンプル電文
├── uart/golden/               # UARTゴールデンフレーム（16進＋対応するJSON）
├── src/iotspec/               # 共通ライブラリ（Pythonパッケージ。他作品からimportできる）
│   ├── sim/                   # 共通シミュレータ
│   │   └── devices/           # mcu-c-01（仮想シリアル）、mcu-cpp-01（MQTT）、gw-01（簡易版）、app-01
│   └── contract/              # 契約テストキット
├── sim/scenarios/             # シナリオ（YAML）
├── tools/validate.py          # 電文をSchemaで検証するCLI
└── docker-compose.yml         # Mosquitto（ACL付き）＋シミュレータ
```

v0.2.1（文書修正）：規範部分（Schema・トピック・フレーム）の変更はない。

### シミュレータが代用できるもの

| 代用対象 | 動作 | 主に使う作品 |
|---|---|---|
| 作品01（`mcu-c-01`） | 仮想シリアルにUARTフレームを送り、CMDにACKを返す。仮想シリアルは「自分でptyを作るモード」と「指定したデバイスパスを開くモード」（Renodeや作品03の本体が作ったポートにつなぐ）の2つ（v0.2.1で変更。socatは必須にしない） | 03 |
| 作品02（`mcu-cpp-01`） | MQTTでtelemetry・stateを送り、cmdにcmd/ackを返す。リースと重複排除も再現 | 03、04 |
| 作品03（`gw-01` 簡易版） | requestに対して設定どおりのrequest/ackを返し、UARTの中継も行う | 01、02、04 |
| 作品04（`app-01`） | シナリオに従ってrequestを送る | 03 |

### 作品ごとの契約テストで使う相手役

| 作品 | 相手役 | 確認すること |
|---|---|---|
| 01 | `gw-01` 簡易版がUARTの上位としてHEARTBEAT・CMDを送る。仮想シリアルで作品01のホスト版に、またはRenodeの仮想UARTをホストの端末につないで接続する（Renode側の接続方法は着手時に確認） | ゴールデンフレームの一致、ACK、重複 `cmd_ref` への前回ACK、上位断での手動解除 |
| 02 | `gw-01` 簡易版がMQTTでcmdを送る | Schema適合、ack、リースの期限切れ、重複 `cmd_id`、`limited` |
| 03 | `mcu-c-01`、`mcu-cpp-01`、`app-01` | UART⇔MQTTの変換、再送と重複排除、調停、`request/ack` |
| 04 | `mcu-c-01`・`mcu-cpp-01`（データ）、`gw-01` 簡易版（`request/ack`） | 取り込み、品質の扱い、要求の再送、決定的な `request_id` |

各作品はCIで、上の相手役を `docker compose` で立ち上げて自分の契約テストを実行する。

シナリオでは、温度の日周変動・照度の昼夜・給水後の土壌水分上昇といった通常の値のほか、第5章の品質問題（故障、途絶、欠番、重複、時刻ずれ、破損フレーム）を時刻指定で注入できる。仮想時間での倍速実行と、過去データの一括生成（作品04の性能試験用）にも対応する。

### MQTTの認証とACL（v0.2.1）

- MQTTのユーザー名は `device_id` と同じにする。パスワードファイルはリポジトリに入れず、`deploy/mosquitto/gen_passwd.sh` で作る（CIも実行時に同じスクリプトで作る）
- `cmd` を発行できるのは `gw-01` だけ、`request` を発行できるのは `app-01` だけ、`request/ack` を発行できるのは `gw-01` だけ
- 各デバイスは自分の `device_id` のトピックにだけ `telemetry`・`event`・`state`・`cmd/ack` を発行できる（`gw-01` はUARTから変換した `mcu-c-01` 名義の電文も発行する）

### 契約テスト

- 各作品のCIで、自分が送る電文を `schemas/` で検証し、`samples/` の電文を自分が受け付けられることを確かめる
- `samples/invalid/` の電文は、受信側が必ず破棄することを確かめる
- UARTは `uart/golden/` で作品01の符号化と作品03の復号を照合する
- `iot-common-spec` 自体のCIで、サンプルがすべてSchemaに合うことと、シミュレータの送る電文がSchemaに合うことを確かめる

## 9. v0.1からの変更点と各作品への影響

| 変更 | 理由 | 影響する作品 |
|---|---|---|
| `request/ack` トピックを追加 | 04が要求の結果を知る手段がなかった | 03、04 |
| cmdに `priority`、`lease_s`、`expires_at`、`origin` を追加。`release` 操作を定義 | リース方式と優先度の調停を全作品で揃える | 01、02、03 |
| cmd/ackの `status` を accepted／done／rejected／failed に統一し、`limited`・`applied`・`reason` を追加 | 安全制限で値を丸めたことを上位が知るため | 01、02、03 |
| 重複排除の決め事（記憶するIDの数と期間、前回結果の再送） | 再送で同じ操作が2回実行されないようにする | 全作品 |
| エンベロープに `boot_id` を追加 | 再起動による連番リセットと欠番を区別する | 全作品 |
| stateに `channels[]`（`period_ms` 付き）と `actuators[]` を必須化 | 自動登録とstale判定の基準に使う | 02、03、04 |
| データ品質の扱いを統一（第5章） | 各設計書で表現がばらついていた | 03、04 |
| イベントコードと重大度を定義（第6章） | 通知と表示を作品をまたいで揃える | 全作品 |
| UARTに STATE 種別、双方向HEARTBEAT、CMDの `ttl_s`、ACKの `limited`・`applied` を追加 | 上記をUART側にも反映 | 01、03 |
| 共通シミュレータを `iot-common-spec` に集約 | 4作品の重複をなくし、実機なしの結合テストを可能にする | 全作品 |

各作品の設計書には、本版の作成に合わせて必要な修正を入れてある。細部の値（ペイロードの配置やコード値）が設計書と食い違う場合は本書を優先する。

### 確定前に決めること

- [ ] 本版の内容で確定してよいか。確定したら `iot-common-spec` にタグを付ける（初回のタグは、実装で確定した点を含めて `v0.2.1` とする。CHANGELOG.md）
- [x] site名の既定値（`home` 案）
- [x] リースの既定値（60秒）と上限（600秒）でよいか

決定（2026-11-20）：site名の既定値は `home`。リースは既定60秒・上限600秒・更新は lease_s の3分の1ごと。v0.2.0は下の最終整合性チェックの後にタグを付ける。

### 最終整合性チェック（2026-11-20）

| 確認項目 | 結果 | 対応 |
|---|---|---|
| 重複排除の記憶が再送期間を覆うか | 作品01の16件は、指令の間隔を決めないと不足しうる。MQTTの `msg_id` 1万件は毎秒200件だと50秒分しかなかった | 03→01の指令を毎秒1件・同時4件に制限。重複判定を発信元ごとの `seq` に変更（第4・5章） |
| IDの用途と生成主体 | 散らばって書かれていた。決定的な `request_id` の作り方が未定義 | IDの表を追加（第4章） |
| UART⇔MQTTで情報を失わないか | 連番を採番し直すと欠番が分からなくなる。元の時間情報が残らない。STATE欠落時の `boot_id` が未定義 | 連番を32ビットに広げて保持、`uptime_ms` を任意項目で追加、再起動検知とSTATEの再要求を定義（第3・7章） |
| 共通シミュレータで4作品の契約テストができるか | 作品01の上位役（UARTでCMDを送る側）が曖昧だった | 作品ごとの相手役の表を追加（第8章） |
| リースと安全制約の関係 | 「600秒＝600秒出力してよい」と読める余地があった | リースは制御権の期限であり安全制約が常に優先、と明記（第4章） |

未解決の不整合はない。Renodeの仮想UARTをホストにつなぐ方法は実際に試していないため、作品01の着手時に確認する。
