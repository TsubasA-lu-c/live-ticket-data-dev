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

ZIPをリポジトリ直下へ展開しません。Downloadsで展開したフォルダから、所定ディレクトリへ必要ファイルだけコピーしてください。

```bash
cd /Users/TsubasA/development/chikenote/live-ticket-data-dev
mkdir -p docs/local-llm config
cp -R ~/Downloads/local-llm-native-v6-member-aware/tools .
cp -R ~/Downloads/local-llm-native-v6-member-aware/tests .
cp ~/Downloads/local-llm-native-v6-member-aware/config/artist_relations.json config/
cp ~/Downloads/local-llm-native-v6-member-aware/README_LOCAL_LLM_EXTRACT_NATIVE.md docs/local-llm/
chmod +x tools/local_llm_extract_native.py tools/classify_local_llm_facts.py
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

`local_llm/audits/audit-<run-id>.zip` に保存します。
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


## v5 content-safety + deterministic matching

v5 adds:

- `detail_enriched` 本文を一覧見出しより優先するようLLM指示を強化
- LLMが誤ったsourceUrlを選んでも、日付/会場/時刻を持つdetail URLへ決定論的に修復
- 会場名の括弧・都道府県表記差を比較時に吸収
- グッズ販売/整理券/物販日時をライブ公演として除外
- 商品受注販売をticketWindowとして除外
- ニュース掲載日・企画決定日だけのeventを除外
- 既存 `data/artist/*.json` との duplicate/new/review を Python で分類

抽出後:

```bash
python3 tools/classify_local_llm_facts.py \
  --facts local_llm/extract_native_enriched_v5/runs/<run-id>/facts.json \
  --output local_llm/extract_native_enriched_v5/runs/<run-id>/classification.json
```

`new` も自動反映はしません。ChatGPT監査対象です。

`classification.json` が存在する場合は監査ZIPにも自動で含めます。


## v6 member-aware classification + stricter ticket guard

v6 adds:

- event / ticketWindow に `subjectName` を追加（本文に明示された場合だけLLMが抽出）
- `config/artist_relations.json` で親グループとメンバーの関係を決定論的に管理
- メンバー個人活動は `related_member` として保持し、親グループの `new` に混ぜない
- 初期設定として L'Arc~en~Ciel → HYDE / ken / tetsuya / yukihiro を登録
- 「販売期間」だけでは ticketWindow と認めない
- 商品/グッズページの販売期間はsource周辺文脈も見て `MERCHANDISE_SALE` で除外
- 機材席・ステージバック席・追加席等の正規チケット販売は許可
- 複数日を含む relationEvidence でも、対象日を改行/文単位で一意に切り出せる場合は許可
- 監査ZIPの既定保存先を `local_llm/audits/` に変更
- 監査ZIPに `artist_relations.json` も含める

分類:

```bash
python3 tools/classify_local_llm_facts.py \
  --facts local_llm/extract_native_enriched_v6/runs/<run-id>/facts.json \
  --artist-dir data/artist \
  --relations config/artist_relations.json \
  --output local_llm/extract_native_enriched_v6/runs/<run-id>/classification.json
```

出力件数は `duplicate / new / review / related_member`。
`related_member` は捨てずに監査対象として保持しますが、親グループの本番dataへ直接混ぜません。


## v7 recall / merchandise hardening

v7 adds:

- `販売期間` などgenericな販売名は、LLM自身の `name/evidence` に入場券根拠がない限りticketWindowにしない
- 周辺本文に別の「チケット」文言があっても、商品販売を救済しない
- `一般発売` と抽出されても evidence に `機材席開放販売` / `ステージバック席追加販売` 等が明記されていれば決定論的に名称を補正
- `config/artist_relations.json` のメンバー名が公式根拠に現れた場合、その根拠だけを狭くした `member_focus` 追加passを実行
- `member_focus` は通常passより先に統合し、メンバー活動のrecall低下を防ぐ
- 本番 `data/` は引き続き変更しない

`report.json` の requests に `passType: member_focus | normal` が入る。


## v8 member recall + ticket precision

v8 adds:

- メンバー補助passを `member_event_focus` / `member_ticket_focus` に分離
- `member_event_focus` は公演抽出だけに集中し、メンバー公演のrecallを改善
- ticket名補正はLLMの短いevidenceだけでなく、同一開始日時の公式source局所文脈も利用
- `一般発売` と出た場合でも、同じ開始日時の公式文脈に `機材席開放販売` 等があれば名称を補正
- member focus と normal pass が同じ受付を別名で返した場合、同一subject/start/end/sourceに限ってcross-pass重複を除去
- 同じpass内の同時刻別受付は潰さない
- 本番 `data/` は変更しない


## v9 validator recovery

- `2026年9.03` / `9.08` 等のdot日付を正規の根拠として認識
- `※Vaundyは11月8日に出演` のような対象者明示節を多日程見出しから分離
- detail本文に対象日+会場が明示される場合は多日程見出しでもfalse rejectしない
- 会場のないTikTok LIVE/配信限定eventを除外
- 同日複数stageが本文に明示されればPythonで公演を分割
- member/normal passの同一公演を統合し、subjectNameとtitleを保持
- 同日既存公演とタイトルが明確に別ならnew判定可能
- 本番dataは変更しない

- 通常chunkも `event_focus` / `ticket_focus` に分離し、同じ根拠から公演と受付を別々に抽出する。混合出力によるticket取りこぼしを減らす
- `機材席開放につき...販売開始` のような自然文からticket種別を決定論的に補正


## v10 audit hardening

V9監査ZIPで判明した誤りを決定論的に修正:

- 古い公式tourページの年を現在年へ誤推定したticketをreject（evidence年 / fdate / ldate照合）
- ticketのstart/end日付がsourceにない場合をhard reject
- FC宝くじ・賞品抽選など、入場券ではない「抽選チケット」を除外
- start/endが両方nullのticketWindowを除外
- ニュース投稿日＋「連動企画決定」等を公演日にしない
- relationEvidenceに日付がなくても、同じevidenceに日付＋会場/時刻が明示されるatomic factは許可
- ディスクガレージ等の興行問い合わせ先をvenueとして保持しない
- 同日・同startTimeの既存performanceが1件ならduplicate判定
- quoted live titleの決定論的補完、発売記念リリースイベント名の補正
- i☆Ris / ゴールデンボンバーのメンバー関係を分類用に追加（追加LLM passは発生させない）

本番 `data/` は変更しない。


## v11 ticket source repair

v11 adds deterministic ticket-source recovery:

- LLM が受付情報を古い一覧URL・別記事URLへ結び付けても、start/end 日付が実際に載る `detail_enriched` URLへ修復
- URLの `?ima=...` 等のクエリ差は同一公式ページとして扱う
- query付き/無しURLの差で `INVALID_SOURCE_URL` や別記事本文へのfallbackが発生する問題を防止
- event / ticket の source URL allowance を同一path基準でも検証
- 本番 `data/` は変更しない

V10監査で確認した日向坂46 `18th Single ひなた坂46 LIVE` の
8/20〜9/1先行、9/7〜9/9オフィシャル先行、9/19一般発売の
false rejectを対象にした修正。


## v12 evidence precision

v12 is a narrow correctness patch over v11:

- ticket種別は、同一記事の周辺文脈より各ticketの `evidence` を優先
- 日向坂46 9/19一般発売が、同じ記事内のFC先行文言で `ファンクラブ抽選先行` に誤補正される問題を修正
- 機材席 / ステージバック席等だけは、従来どおり同一日時の公式source局所文脈から補完
- Billboard複数stage展開時、各stageの `evidence` をそのstage自身の公式本文断片へ置換
- 2nd stageのfactに1st stageのevidenceが残る監査不整合を修正
- 本番 `data/` は変更しない
