# 未決の論点（open questions）

仕様（docs/spec.md）に書かれていない、または食い違いがあり、実装側で判断が必要になった点を記録する。
判断した内容は共通仕様の書類にも反映する（v0.2.x として）。

状態：`未決`（判断待ち）／`仮実装`（推奨案で進めている）／`決定`／`v0.2.0 では扱わない`

| # | 論点 | 選択肢 | 推奨 | 状態 |
|---|---|---|---|---|
| 1 | リポジトリ構成の食い違い：spec.md 第8章はシミュレータを `sim/devices/`・`sim/scenarios/` に置くが、CLAUDE.md と実装計画はコードを `src/iotspec/sim/`（devices を含む）、シナリオを `sim/scenarios/` に置く | A：spec.md 第8章の構成図を実装計画に合わせて直す（v0.2.x）／B：コードを `sim/devices/` に置き spec.md に合わせる | A。パッケージとして import・型検査・配布できる src レイアウトのほうが他作品から使いやすい。spec.md の図は説明なので直す | 未決（Phase 0 は影響なし。Phase 4 着手前に決める） |
| 2 | mcu-c-01 代役の仮想シリアル：spec.md 第8章は「socat で作った仮想シリアル」、実装計画 Phase 5 は Python の `pty.openpty()` | A：`pty.openpty()` を既定とし、socat は任意の接続手段として spec.md に併記（v0.2.x）／B：socat を必須にする | A。追加ツールなしで CI と macOS/Linux の両方で動く。socat は作品01のホスト版や Renode を別端末につなぐ用途に残す | 未決（Phase 5 着手前に決める） |
