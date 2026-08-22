# ChatGPT ↔ GitHub Actions ↔ Local LLM job queue

このディレクトリは、ChatGPT と Windows self-hosted runner の間で小さい制御メッセージだけを受け渡すための Git 管理キューです。

## job layout

```text
agent/jobs/<jobId>/
├── request.json   # ChatGPT/人間が作成。Actions起動トリガー
├── result.json    # Actionsが作成。ChatGPT監査用の圧縮結果
└── review.json    # ChatGPTが監査後に作成
```

`local_llm/` に生成される raw / facts / rejected / audit / cache は Git 管理しません。

## request.json schema v1

```json
{
  "schemaVersion": 1,
  "jobId": "20260822-001",
  "kind": "artist_live_research",
  "targets": ["yuzu", "milk"],
  "model": "gpt-oss:20b",
  "historyDays": 180,
  "enrichDetails": true,
  "requestedBy": "chatgpt",
  "requestedAt": "2026-08-22T19:30:00+09:00",
  "note": "定期巡回"
}
```

### 許可値

- `kind`: `artist_live_research` のみ
- `targets`: `data/artist/<id>.json` に存在する artistId のみ、最大100件
- `model`: 既定では `gpt-oss:20b` / `qwen3.5:9b` のみ
- `historyDays`: 1〜365

任意 shell / PowerShell / Python コマンドを request から指定する機能は持たせません。

## result.json

Actions は次を圧縮して返します。

- 機械収集の metrics / changed artist / newEvents / missingOnSite
- ローカルLLMを使った場合の分類 (`duplicate/new/review/related_member`)
- validator reject / LLM error / fetch error
- ChatGPTが見るべき review policy

`data/` は変更しません。`result.json` は常に `status: waiting_chatgpt_review` として返し、最終承認をChatGPT側へ残します。

## review.json schema v1

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

`verdict` は `approved` / `needs_rework` / `manual_review` / `no_change` のいずれかです。

`needs_rework` の場合は既存 request を書き換えず、新しい jobId で request を作り、`parentJobId` を note または将来のschema拡張で関連付けます。無限再試行は行いません。
