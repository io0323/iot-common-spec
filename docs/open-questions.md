# 未決の論点（open questions）

仕様（docs/spec.md）に書かれていない、または食い違いがあり、実装側で判断が必要になった点を記録する。
判断した内容は共通仕様の書類にも反映する（v0.2.x として）。

状態：`未決`（判断待ち）／`仮実装`（推奨案で進めている）／`決定`／`v0.2.0 では扱わない`

| # | 論点 | 選択肢 | 推奨 | 状態 |
|---|---|---|---|---|
| 1 | リポジトリ構成の食い違い：spec.md 第8章はシミュレータを `sim/devices/`・`sim/scenarios/` に置くが、CLAUDE.md と実装計画はコードを `src/iotspec/sim/`（devices を含む）、シナリオを `sim/scenarios/` に置く | A：spec.md 第8章の構成図を実装計画に合わせて直す（v0.2.x）／B：コードを `sim/devices/` に置き spec.md に合わせる | A。パッケージ内に置けば契約テスト（`iotspec.contract`）や他作品のリポジトリから import できる。シナリオ YAML はデータなので `sim/scenarios/` に置く。spec.md の図は説明用で規範ではない | 決定（2026-11-20）：A。シミュレータのコードは `src/iotspec/sim/`、シナリオ YAML は `sim/scenarios/`。spec.md 第8章の図を v0.2.1 の文書修正で直す（Schema とタグの意味は変わらない） |
| 2 | mcu-c-01 代役の仮想シリアル：spec.md 第8章は「socat で作った仮想シリアル」、実装計画 Phase 5 は Python の `pty.openpty()` | A：`pty.openpty()` だけを使う／B：socat を必須にする／C：2モードを持つ（「自分で pty を作る」モードと「指定したデバイスパスを開く」モード） | C。既定は「自分で pty を作る」モード（`pty.openpty()`。追加依存なしで macOS・Linux の両方で動く）。Renode や作品03の本体など、相手側が作ったポートにつなぐ場面のために「指定したデバイスパスを開く」モード（pyserial）も持つ。socat は必須にしない | 未決（Phase 5 着手前に決める。pyserial の採用と CLAUDE.md の技術スタックの更新も同時に判断する） |
