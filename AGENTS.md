# live-ticket-data-dev 運用ガイド

このリポジトリは検証用データ配信です。共通方針は `../AGENTS.md`、収集データの正確性・根拠・削除規則は `COLLECTION_RULES.md`、具体的な更新手順は `OPERATIONS.md` に従ってください。

## 作業前

- 既存の未コミット変更を確認し、ユーザーの差分を上書きしない。
- 更新作業ではリモートとの差分を確認する。pullが必要な場合は、作業ツリーが安全であることを確かめてから行う。
- 「更新」「refresh」だけで方式が未指定なら、`OPERATIONS.md` の更新方式を提示して選択を待つ。「続きから」は直前と同じ方式を引き継ぐ。
- 大きなスキーマ・収集方式の変更はこの検証用リポジトリで確認し、本番へ自動同期しない。

## 収集の必須条件

- 公式サイトまたは正規チケットサイトの直接取得結果を根拠にする。検索結果の要約から日程を転記しない。
- 推測URL、異なる受付種別の日程流用、根拠のない補完を禁止する。確認できない値は規則に従ってnullにし、重要な不明点は報告する。
- 一覧だけで終えず、NEWS、LIVE、個別記事など規則で必要なページを辿る。
- 抽選・公演・ツアーの削除条件は `COLLECTION_RULES.md` を厳守する。受付終了だけを理由に抽選を削除しない。
- 機械収集では `docs/workflows/refresh-machine.md` に従い、AIキューの構造化中はWebへアクセスしない。

## 並列更新

- 1担当は `data/artist/{id}.json` 1組だけを編集し、共通ファイルや他アーティストへ触れない。
- メイン担当だけが成功結果を確認して `data/artists.json` と `data/manifest.json` を更新する。
- 各commit/push単位で `tools/update_manifest.py` を実行し、対象JSON、artists、manifest、必要なcacheを同じ単位に含める。
- サブ担当の根拠引用、確認URL、発見数と収集数を照合してから成功扱いにする。

## 検証

変更を配信単位へ確定する前に、少なくとも次を実行します。

```sh
python3 tools/validate.py
python3 tools/update_manifest.py
```

機械収集のAI結果は、反映前に `python3 tools/validate_ai_result.py` も通します。WARNINGは件数だけで済ませず、抜け漏れの疑いとして内容を確認します。commitやpushは現在の依頼で許可された場合だけ行ってください。

## リポジトリの整理・生成物配置ルール

リポジトリ直下を一時ファイルや検証生成物の置き場にしない。
作業中・実行時に生成するファイルは、用途に応じて必ず所定のディレクトリへ配置すること。

### 配置先

- 実装コード: `tools/`
- テストコード: `tests/`
- 設計書・運用ドキュメント: `docs/`
- ローカルLLM関連ドキュメント: `docs/local-llm/`
- ローカルLLMの実行結果・staging・review・監査ZIP・A/B結果: `local_llm/`
- 収集処理の再生成可能なキャッシュ: `cache/` の既存ルールに従う
- 本番データ: `data/`
- 収集設定: `config/`

### 禁止事項

- リポジトリ直下へ `audit-*.zip`、`ab-native-*.json`、LLM実行結果、検証用JSON、作業用ZIPを生成しない。
- 一時的な検証のためだけにルート直下へ新規ファイルを増やさない。
- `local_llm/` 配下の実行結果や監査データをGit管理しない。
- 再生成可能な実行生成物をGitHubへpushしない。
- 既存ファイルを整理する目的だけで、無関係な変更・削除を巻き込まない。

### 作業開始・終了時の確認

作業開始前と終了前に `git status --short` を確認する。

終了時に意図しないルート直下ファイルや生成物がある場合は、
commit前に所定のディレクトリへ移動するか削除する。

新しい種類の生成物を追加する場合は、
実装と同時に保存先と `.gitignore` の必要性を決め、
リポジトリ直下へ置くことをデフォルトにしない。

### commit / push

複数の未コミット作業が存在する場合、
整理ルールだけを変更するcommitへ他の作業差分を混ぜない。
`git add` / `git commit` は対象ファイルを明示して実行する。

## ChatGPT / GitHub Actions / Local LLM キュー

`agent/jobs/` は、ChatGPT と self-hosted runner の制御メッセージだけをGit管理する例外領域です。詳細は `docs/workflows/chatgpt-local-llm-actions.md` に従ってください。

- Git管理してよいもの: `agent/jobs/<jobId>/request.json`、`result.json`、`review.json`。
- `result.json` はChatGPT監査用の圧縮されたレビュー封筒であり、raw実行生成物とは扱いを分ける。
- raw HTML、LLM prompt/response全文、facts/rejected/audit ZIP、差分stateは `local_llm/` に置き、Git管理しない。
- Actions経路から `data/` を直接変更しない。承認後のpromotionは別工程とする。
- public repo の self-hosted runner は `pull_request` / `pull_request_target` から起動しない。
- requestから任意shellコマンドを受け取らない。kind、artistId、model等はallowlistで検証する。
