# local_llm_extract_native

Codex復活前に使うための、既存パイプライン非破壊の「事実抽出専用」ランナーです。

## 何が違うか

- `local_llm_collect.py` は変更しません。
- Ollama native `/api/chat` を使います。
- GPT-OSS 20B は `think=low` を自動選択します。
- LLMへ `parsedEvents` / `existingSummary` を渡しません。
- LLMには新規/既存/重複/add/updateを判断させません。
- evidenceに明示された事実だけを抽出します。
- 取得後に決定論的validatorを通します。
- `data/` は一切書き換えません。

## インストール

このZIPを `live-ticket-data-dev` のルートへ展開してください。

```bash
cd /Users/TsubasA/development/chikenote/live-ticket-data-dev
unzip ~/Downloads/local-llm-extract-native.zip
chmod +x tools/local_llm_extract_native.py
```

## 接続確認

```bash
cd /Users/TsubasA/development/chikenote/live-ticket-data-dev
export LOCAL_LLM_BASE_URL="http://192.168.10.143:11434/v1"
export LOCAL_LLM_MODEL="gpt-oss:20b"
python3 tools/local_llm_extract_native.py --check
```

## queue全件をGPT-OSS 20Bで抽出

```bash
python3 tools/local_llm_extract_native.py \
  --queue cache/ai_queue.json \
  --out-root local_llm/extract_native \
  --model gpt-oss:20b
```

## 3組だけ

```bash
python3 tools/local_llm_extract_native.py \
  --queue cache/ai_queue.json \
  --out-root local_llm/extract_native \
  --model gpt-oss:20b \
  one_ok_rock sky_hi momoiro_clover_z
```

## Qwen 9Bでも比較

```bash
python3 tools/local_llm_extract_native.py \
  --queue cache/ai_queue.json \
  --out-root local_llm/extract_qwen9b \
  --model qwen3.5:9b
```

## テスト

```bash
python3 -m unittest tests/test_local_llm_extract_native.py
```

## 出力

`local_llm/extract_native/runs/<run-id>/`

- `input.json`
- `facts.json`
- `rejected.json`
- `errors.json`
- `report.json`

`facts.json` は候補確定ファイルではありません。
本番反映は禁止です。ChatGPT監査用の中間事実データとして扱ってください。


## ChatGPT監査用ZIP生成

抽出後に表示されたrunディレクトリを指定します。

```bash
python3 tools/export_local_llm_audit.py \
  --run-dir local_llm/extract_native/runs/<run-id>
```

リポジトリ直下に `audit-<run-id>.zip` ができます。
これをChatGPTへ添付すれば、既存データと抽出根拠をまとめて監査できます。


### v2安全ガード

- `2026.09.12` のようなドット日付に対応。
- LLMが長い原文を短く再構成しても、同一source内の個別事実で検証。
- デフォルトで180日より古い公演/受付を `STALE_*` として除外。
- 配信視聴チケットは `STREAMING_TICKET` としてライブ受付から除外。
- 複数日が混ざった出演根拠は引き続き安全側でreject。

保持期間を変える場合:

```bash
python3 tools/local_llm_extract_native.py --history-days 180 ...
```


## 公式detail補完

見出しは取得できたのに詳細ページ本文までqueueへ入っていない場合、同一公式サイト内のanchor textを照合して詳細本文を補います。

```bash
python3 tools/enrich_ai_queue_details.py \
  --queue cache/ai_queue.json \
  --output local_llm/enriched_queue/ai_queue_enriched.json
```

その後、補完済みqueueを抽出器へ渡します。

```bash
python3 tools/local_llm_extract_native.py \
  --queue local_llm/enriched_queue/ai_queue_enriched.json \
  --out-root local_llm/extract_native_enriched \
  --model gpt-oss:20b
```

元の `cache/ai_queue.json` は変更しません。


### detail補完のノイズ制御

v3では、1アーティストにつきdetail本文はスコア上位4件までに制限し、
各detailは見出し周辺2500文字だけを追加します。

必要なら変更できます。

```bash
python3 tools/enrich_ai_queue_details.py   --max-details-per-artist 4   --detail-chars 2500   --queue cache/ai_queue.json   --output local_llm/enriched_queue/ai_queue_enriched.json
```


## v4 chunked extraction

detail補完後にevidence量が増えても、1アーティストを一括投入しません。

- evidence blockが5000文字を超えたら分割
- 1 LLM callあたり約9000文字まで
- GPT-OSS既定 `num_ctx=16384`
- `num_predict=4096`
- chunkごとに抽出・validator
- 最後にevent/ticketを決定論的dedupe
- 1 chunk失敗でも他chunkの成功結果は保持
