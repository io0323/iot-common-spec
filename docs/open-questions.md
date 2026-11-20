# 未決の論点（open questions）

仕様（docs/spec.md）に書かれていない、または食い違いがあり、実装側で判断が必要になった点を記録する。
判断した内容は共通仕様の書類にも反映する（v0.2.x として）。

状態：`未決`（判断待ち）／`仮実装`（推奨案で進めている）／`決定`／`v0.2.0 では扱わない`

| # | 論点 | 選択肢 | 推奨 | 状態 |
|---|---|---|---|---|
| 1 | リポジトリ構成の食い違い：spec.md 第8章はシミュレータを `sim/devices/`・`sim/scenarios/` に置くが、CLAUDE.md と実装計画はコードを `src/iotspec/sim/`（devices を含む）、シナリオを `sim/scenarios/` に置く | A：spec.md 第8章の構成図を実装計画に合わせて直す（v0.2.x）／B：コードを `sim/devices/` に置き spec.md に合わせる | A。パッケージ内に置けば契約テスト（`iotspec.contract`）や他作品のリポジトリから import できる。シナリオ YAML はデータなので `sim/scenarios/` に置く。spec.md の図は説明用で規範ではない | 決定（2026-11-20）：A。シミュレータのコードは `src/iotspec/sim/`、シナリオ YAML は `sim/scenarios/`。spec.md 第8章の図を v0.2.1 の文書修正で直す（Schema とタグの意味は変わらない） |
| 2 | mcu-c-01 代役の仮想シリアル：spec.md 第8章は「socat で作った仮想シリアル」、実装計画 Phase 5 は Python の `pty.openpty()` | A：`pty.openpty()` だけを使う／B：socat を必須にする／C：2モードを持つ（「自分で pty を作る」モードと「指定したデバイスパスを開く」モード） | C。既定は「自分で pty を作る」モード（`pty.openpty()`。追加依存なしで macOS・Linux の両方で動く）。Renode や作品03の本体など、相手側が作ったポートにつなぐ場面のために「指定したデバイスパスを開く」モード（pyserial）も持つ。socat は必須にしない | 未決（Phase 5 着手前に決める。pyserial の採用と CLAUDE.md の技術スタックの更新も同時に判断する） |
| 3 | device_id の命名規則（第2章）は `{種別}-{言語}-{連番}` だが、予約済みの `gw-01`・`app-01` は言語部がない | A：言語部を任意にする（`^[a-z][a-z0-9]*(-[a-z][a-z0-9]*)?-[0-9]{2,}$`）／B：規則どおり3部に限り、予約済み ID を例外として列挙する | A。予約済み ID と規則の両方を満たし、例外の列挙が要らない。連番は2桁以上とした（例がすべて2桁） | 仮実装（Phase 1） |
| 4 | cmd の payload でどの項目が必須かが表になっていない（第3章はコード例のみ）。`lease_s` は「すべての set_actuator はリースを持つ。既定60秒」（第4章）で、省略してよいかが読み取れない | A：`cmd_id`・`op`・`priority` を必須、`args` は set_actuator のとき必須、`lease_s`・`expires_at`・`origin` は任意（`lease_s` 省略時は既定60秒）／B：例にある項目をすべて必須にする | A。既定値がある項目は省略可と読むのが自然。`priority` は第4章でデバイス側のログと表示に使うとあるので必須にした | 仮実装（Phase 1） |
| 5 | set_actuator 以外の op（`release`・`set_mode`・`set_param`・`get_status`）の args が未定義 | A：Phase 1 では制約しない（args は任意の object）／B：`release` は `id`、`set_mode` は `mode`、`set_param` は `name`・`value` を必須にする | A で仮実装。ただし `release` は対象のアクチュエーターが要るので、v0.2.x で B の `release`→`id` 必須を足すことを推奨 | 仮実装（Phase 1） |
| 6 | cmd/ack で `limited`・`applied`・`reason` が必須かどうか、`reason` が「拒否・失敗時」に必ず要るか（第3章） | A：`cmd_id`・`status` だけ必須。`status` が rejected/failed のときは `reason`（null 以外）を必須／B：例の4項目をすべて必須 | A。accepted の時点では `applied` が決まらないことがある。拒否・失敗の理由は上位の処理に必要なので必須にした | 仮実装（Phase 1） |
| 7 | request/ack の `cmd_id` が要る範囲（「accepted 以降」）。failed は 03 が指令を出したあとの失敗と、指令を出す前の失敗（例：`target_offline`）の両方がありうる | A：accepted・done のとき必須、failed・rejected は任意／B：accepted・done・failed のとき必須 | A。failed の全ケースで指令があるとは限らない。rejected・failed では `reason`（null 以外）を必須にした | 仮実装（Phase 1） |
| 8 | `level`（0–1000‰）を整数に限るか、数値（小数可）にするか。単位の節では「JSON 上の値は浮動小数点数」とある | A：整数／B：数値 | A。例はすべて整数で、UART の `fan_pm` も u16。telemetry の `readings[].value` は数値のまま | 仮実装（Phase 1） |
| 9 | `boot_id` の形式が「短いID」以外に未定義。UART 経由は16進8桁（第7章） | A：空でない文字列だけ検査／B：16進8桁に限る／C：長さ上限（例：32文字）を付ける | A。MQTT 直結のデバイスの生成方法が未定義なので、形式を決めるのは Phase 2（boot_id 生成）で一緒に判断するのがよい | 仮実装（Phase 1。Phase 2 で見直す） |
| 10 | telemetry の `actuators[].owner` の値と、state の `actuators[]` の `min`・`max`・`max_on_s` の型・意味が未定義。連続出力の上限がないアクチュエーター（例：`fan_main`）の `max_on_s` も未定義 | A：`owner` は文字列（列挙しない）、`min`・`max` は数値、`max_on_s` は 0 以上の数値か null（上限なし）／B：`owner` を優先度の値（safety/manual/auto/schedule）＋local に限る | A で仮実装。`owner` は優先度の値＋`local` を推奨値として v0.2.x で列挙するかを判断する | 仮実装（Phase 1） |
| 11 | センサー故障（`quality: fault`）のとき `readings[].value` に何を入れるかが未定義 | A：数値を必須のまま（最後の値などを入れる）／B：fault のときだけ null を許す | A で仮実装。受信側は fault の値を判定に使わない（第5章）ので害はないが、作品01・02の実装前に決めておくのがよい | 仮実装（Phase 1） |
| 12 | 第3章の request の例は `kind: manual` なのに `schedule` 項目を含む | A：そのまま許す（未知・余分な項目は許可）／B：manual のときは `schedule` を禁止する | A。前方互換の方針（第1章）に合う。例は「schedule 時の項目」を示すためと読み、samples/valid にそのまま収録した。書類の例を `kind: schedule` に直すかは v0.2.x で判断 | 仮実装（Phase 1） |
