# muse-inv — Muse の仕組み・技術・環境の蓄積管理 CLI

## 目的と設計

- 正本は構造化データ（JSONL）であり、平文の台帳・MEMORY は閲覧用の生成物という位置づけにする。
- 対象は2層に分ける。
  - プラットフォーム層（`data/platform.jsonl`）： Muse に組み込まれた仕組み。`scan` が実物（docs、スキル、コネクタ、Goal、cron、中核ファイル、ドメイン／エンティティファイル）を列挙して再生成する。手で編集しない。
  - 構築層（`data/registry.jsonl`）： あなたの環境で上に積んだ技術・経路・手順・失敗パターン。`add` で1件ずつ登録する側。
- 見落とし防止は注意ではなく機械で行う： `scan` の実物列挙と差分、`audit` の母集団に対する件数照合、スキーマ検証（不正な行は黙って飛ばさずエラーにする）。
- 積み下ろしは `export` / `import-check` で行う。`export` は移送可否（portability）で絞り、依存（depends_on）の閉包と manifest を付ける。受け側は `import-check` で「既にある／不足／競合」を照合してから取り込む。

## 使い方

```sh
cd ~/workspace/muse-inv
./muse-inv scan          # プラットフォーム層を再生成し、前回との差分を表示
./muse-inv list          # 構築層の一覧（--layer platform|all、--kind、--status で絞れる）
./muse-inv show inv-098  # 1件を JSON で表示
./muse-inv search "Xポストの全文を読む経路" --top 5
./muse-inv add --id ... --name ... --kind tool --status active --source ... \
  --description ... --alias ... --portability unclassified --curated-in ...
./muse-inv audit         # 件数・未分類・出典切れの監査
./muse-inv verify        # 固定テスト8問の検索順位を検証
./muse-inv export        # exports/<日時>/ に manifest.json / items.jsonl / manifest.md
./muse-inv import-check exports/<日時>/
./muse-inv migrate       # 知識台帳 markdown から registry.jsonl を再生成（ワンオフ）
```

Python 3 標準ライブラリのみで動作する（pip 依存なし）。

## スキーマ（1行1レコード、全項目必須）

| 項目 | 内容 |
|---|---|
| id | 安定した一意の slug。構築層は `inv-001` 形式、プラットフォーム層は `platform:<種別>:<名前>` |
| name | 名前 |
| kind | 構築層： tool / route / procedure / failure-pattern / automation / setting。プラットフォーム層： skill / connector / doc / goal / cron / core-file / domain-file |
| layer | built / platform |
| status | active / superseded / unadopted / unknown / prepared |
| source | 出典（ファイルパス、行番号を含むことがある） |
| description | 1行の説明 |
| alias | 日本語の用途・別名。空にできるのはプラットフォーム層のみ |
| portability | portable / conditional / env-specific / do-not-port / unclassified |
| depends_on | 依存先 id のリスト（空可） |
| curated_in | 整理済みの置き場所。`daily-log only` を含むものは日次ログにしかない |
| last_verified | ISO 日付、または空 |

追加フィールドとして、移行したレコードには `inv_no`（台帳の番号）、`section`（台帳の節）、必要に応じて `status_note`（台帳の元の状態表記）が入る。

## 実測値（2026-11-15、初回構築時）

### 構築層： 164件

- 移行元は `~/workspace/goals/memory-structure-redesign/hidden_files/knowledge-inventory-2026-11-14.md`。同ファイルの Summary counts は「151件（A–H節）」と記すが、2026-11-15 のガーデナー追加（I節、#152–164）により、ファイル内の実在行数は **164件**（A:24、B:24、C:26、D:23、E:20、F:8、G:23、H:3、I:13、番号1–164に欠番・重複なし）。`aliases.json` のキー数も164で一致する。移行はこの164件全件を行った。
- alias は `vector-index/aliases.json`（キーは台帳番号の文字列）から結合した。全164件に alias が付いたため、説明文で代用した項目はない。
- 種別： procedure 54、tool 44、failure-pattern 23、route 22、automation 13、setting 8。
- 状態： active 146、unadopted 9、unknown 6、superseded 2、prepared 1。台帳の状態表記が複合形（例： `active (seed route)`）のものは先頭語で正規化し、元の表記を `status_note` に残した。
- `curated_in` に `daily-log only` を含むもの： 55件（部分的に整理済みの行を含む数え方。台帳自身の集計では「実質が日次ログにしかないもの54件、うち46件は完全に日次ログのみ」）。
- portability は全164件が `unclassified`、depends_on は全164件が空、last_verified は全164件が空。台帳がこれらを記載していないため、推測で埋めていない。分類は今後の作業であり、`audit` が件数を出し続ける。
- 出典の実在確認： 検証可能なローカルパス204件を確認し、切れは0件だった。

### プラットフォーム層： 312件（`scan` の実測、手作業の `ls` 集計と一致）

| 区分 | 件数 |
|---|---|
| doc（~/docs/**/*.md） | 32 |
| system-skill（/opt/muse/skills） | 57 |
| custom-skill | 14 |
| system-connector（/opt/hatch/connectors） | 88 |
| custom-connector | 1 |
| goal | 42 |
| cron（cron.d 34 + goals/*/crons 29） | 63 |
| core-file | 7 |
| domain-file | 7 |
| entity-file | 1 |

- `scan` を2回続けて実行した結果、2回目の差分は added 0 / removed 0 / changed 0 で安定した。
- プラットフォーム層と構築層の id の衝突は0件。

### verify（固定テスト8問、期待項目が上位5件以内か）

8問全て PASS（2026-11-15）。順位： FxTwitter 1位（2問とも）、Tailscale 1位、yomiyasu 1位、SwitchBot ボタン名 1位、pass 1位、Amazon 経路 3位、route-api 4位。

### export / import-check

- 既定の `export` で164件を書き出し、`import-check` で照合した結果： 一致164件、不足0件、競合0件。
- portability が全件 unclassified のため、`--portability portable` のような絞り込みでは現状0件になる。依存閉包も depends_on が空のため実質未行使であり、manifest にその旨を明記している。

## 制限

- `search` は標準ライブラリだけのキーワード検索（ラテン語トークン＋日本語バイグラム、名前・別名を重み付け）であり、ベクトル索引（`vector-index/query.py`）は統合していない。固定テスト8問では両者の期待項目が上位に入ることを確認済みの範囲で使う。
- `scan` のプラットフォーム層の説明文は各ファイルの最初の見出しであり、内容の要約ではない。
- cron の列挙には `cron.d/_archive/` の終了済み定義も含む（実在するファイルは全件数える方針のため）。現行分だけを見たい場合はファイルパスで判別する。
- portability と depends_on の分類が終わるまで、`export` は「全件＋未分類の警告付き」が実態であり、環境間の移送の自動判定にはまだ使えない。
