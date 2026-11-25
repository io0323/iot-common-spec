# UART ゴールデンフレーム

作品01（符号化）と作品03（復号・変換）の照合に使う、符号化済みの UART フレームと期待値です（共通仕様 第7章）。
`tools/gen_golden.py` で生成します。手で編集しないでください。

- `NN_name.hex`：符号化済みフレーム（COBS、末尾の `00` を含む）のスペース区切り16進
- `NN_name.json`：
  - `frame`：フレームのフィールド値（`type`・`type_code`・`seq` と種別ごとの項目）
  - `hex`：`.hex` と同じ内容
  - 03 → 01：`input_cmd_payload`（変換元の cmd の payload）と `cmd_ref`。作品03の内部の操作（GET_DIAG）は
    `internal_op` と `cmd_ref`（その ACK は cmd/ack にしない）。対応する汎用指令がない CMD は `frame` だけ
  - 01 → 03：`received_at`（受信時刻）と `expected`（`envelope`：変換後の電文、`discarded`：破棄の理由、
    `request_state`：GET_STATUS で STATE を求めるか、`dropped_telemetry_total`：STATE 待ちで破棄した TELEMETRY の累計、
    DIAG のときだけ `diag`：復号した項目と受信時刻・boot_id）。`discarded` が `not_converted` のもの（DIAG、GET_DIAG の ACK）は
    MQTT に変換しないフレームです（v0.2.2 #37）

UART の `seq` は方向・フレーム種別ごとの連番です（v0.2.1 #25）。v0.2.2 で17番を変更し、28〜35番を足しました（CHANGELOG.md）。ゴールデンは `NN` の順に1本のセッションになっています。01 → 03 の期待値は、それより前のフレームを
すべて同じ順に通した状態での結果です（boot_id、seq の一周、STATE 待ちなど）。
変換にはデバイスプロファイル `profiles/mcu-c-01.yaml` を使います。

再生成して差分がないことは `uv run python tools/gen_golden.py --check` で確かめられます。
