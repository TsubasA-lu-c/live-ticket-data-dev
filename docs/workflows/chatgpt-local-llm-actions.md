# ChatGPT → GitHub Actions → Local LLM → ChatGPT review

## 目的

ChatGPTを司令塔・監査役、WindowsローカルLLMを大量一次処理、GitHubを受け渡しと監査ログにする。

既存の収集・抽出コードを再実装せず、次をそのまま利用する。

1. `tools/collect_live_info.py`
2. `tools/enrich_ai_queue_details.py`（任意）
3. `tools/local_llm_extract_native.py`
4. `tools/classify_local_llm_facts.py`
5. `tools/run_local_llm_job.py` が上記を安全に束ねる

## 全体フロー

```text
ChatGPT
  ↓ agent/jobs/<jobId>/request.json をmainへ追加
GitHub Actions
  ↓ push path trigger
Windows self-hosted runner
  ↓ Python収集
  ↓ 必要な組だけLocal LLM
  ↓ 既存dataとの決定論的分類
GitHub
  ↓ agent/jobs/<jobId>/result.json をbot commit
ChatGPT monitoring task
  ↓ 未reviewのresultを監査
  ↓ agent/jobs/<jobId>/review.json
approved / no_change / needs_rework / manual_review
```

## Windows runner前提

GitHub self-hosted runner に次のラベルを付ける。

- `self-hosted`
- `windows`
- `x64`
- `local-llm-gpu`

Ollama は runner マシンで `127.0.0.1:11434` に公開する。

Actions は次の環境変数を設定する。

```text
LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
LOCAL_LLM_ALLOWED_MODELS=gpt-oss:20b,qwen3.5:9b
```

`actions/checkout` は `clean: false` にしている。理由は `local_llm/actions/state/` にActions専用の差分状態を保持するため。ここは `.gitignore` 対象でGitHubへpushしない。

## 公開リポジトリでの安全境界

このリポジトリはpublicのため、self-hosted runnerをPRイベントへ接続しない。

`.github/workflows/local-llm-review-pipeline.yml` は以下だけで起動する。

- `main` への push
- `agent/jobs/*/request.json` の変更
- 明示的な `workflow_dispatch`

`pull_request` / `pull_request_target` は追加しない。

requestはデータだけを受け取り、任意コマンドは受け取らない。`tools/run_local_llm_job.py` が以下を検証してから処理する。

- jobIdとディレクトリ名が一致
- kindがallowlist内
- artistIdが `data/artist/` に実在
- modelがallowlist内
- historyDays範囲
- result出力先が同じjobディレクトリ

## data/への書き込み境界

このActions経路では `data/` を変更しない。

収集状態は `local_llm/actions/state/`、run生成物は `local_llm/actions/jobs/<jobId>/` に隔離する。

Git管理するのは次の小さいメッセージだけ。

```text
agent/jobs/<jobId>/request.json
agent/jobs/<jobId>/result.json
agent/jobs/<jobId>/review.json
```

配信データへの反映は `review.json` で承認された後の別工程とする。最初の段階では自動promotionを実装しない。

## ChatGPT review

ChatGPTは `result.json` の次を優先確認する。

1. `summary.fetchFailed > 0`
2. `localLlm.classification.counts.review > 0`
3. `localLlm.classification.counts.new > 0`
4. `localLlm.classification.counts.related_member > 0`
5. `collection.artists[].newEvents`
6. `collection.artists[].missingOnSite`（削除理由にしない）
7. `pipelineErrors` / `localLlm.errors`

必要な場合だけ公式Webを再確認する。ローカルLLMの結果だけを根拠に承認しない。

### review.json

```json
{
  "schemaVersion": 1,
  "jobId": "20260822-001",
  "reviewedAt": "2026-08-22T20:00:00+09:00",
  "reviewedBy": "chatgpt",
  "verdict": "approved",
  "summary": "公式根拠と既存データを確認し問題なし",
  "issues": [],
  "rework": null
}
```

## ChatGPT monitoring taskの想定

1時間ごとに `agent/jobs/` を確認する。

- `result.json` がある
- 同じjobに `review.json` がない

この条件のjobだけ監査する。

監査結果が `approved` / `no_change` の場合は `review.json` を保存して終了する。

`needs_rework` は新しいjobIdで再調査requestを作る。既存requestを上書きしない。再試行回数は運用上2回程度で打ち切り、以後は `manual_review` とする。

## 初回セットアップ

1. WindowsにGitHub self-hosted runnerを登録
2. `local-llm-gpu` ラベルを追加
3. Ollama起動確認
4. runner上で `python tools/local_llm_extract_native.py --check`
5. `main` に小さいrequestを1件追加
6. Actions完了後に `result.json` がbot commitされることを確認
7. その後ChatGPT monitoring taskを有効化

## request作成例

```json
{
  "schemaVersion": 1,
  "jobId": "20260822-001",
  "kind": "artist_live_research",
  "targets": ["yuzu"],
  "model": "gpt-oss:20b",
  "historyDays": 180,
  "enrichDetails": true,
  "requestedBy": "chatgpt",
  "requestedAt": "2026-08-22T19:30:00+09:00",
  "note": "Actions疎通確認"
}
```

## 現段階で意図的に未実装

- `review.json` 承認後の `data/` 自動反映
- ActionsからChatGPTへのWebhook通知
- 無制限リトライ
- requestからの任意shell実行

まず「依頼 → ローカル処理 → GitHubへレビュー封筒 → ChatGPT監査」の閉ループを安定させ、その後promotionを追加する。
