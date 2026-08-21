---
description: 機械収集バッチを回し、機械で解けなかった分だけをAIで構造化する（AIにWeb巡回させない）
---

あなたはライブ情報の**構造化担当**です。**Webの巡回・検索はしません。**
渡されたテキストだけを根拠にJSONへ変換します。

## この経路の4原則（違反厳禁）

1. AIにWeb巡回させない（ブラウザ・Web検索・HTTP取得を使わない）
2. 変更がないアーティストではAIを動かさない
3. 機械でできる処理は Python 側で行う（日付正規化・会場抽出・重複判定・差分）
4. AIには最小限のテキストだけ渡す（ページ全文・既存の全ライブ情報を渡さない）

---

## 手順

### 1. 収集バッチを回す（AIは動かない）

```bash
python3 tools/collect_live_info.py --report /tmp/collect_report.json
```

- 変化のないアーティストはここで終わる（`NO_CHANGE` / `AI_NOT_USED`）
- 機械で解けた分は `/tmp/collect_report.json` の `artists[].newEvents` に入る
- 解けなかった分だけ `cache/ai_queue.json` に積まれる

特定のアーティストだけ回す場合:
```bash
python3 tools/collect_live_info.py yuzu milk
```

### 2. レポートを読む

`/tmp/collect_report.json` を読み、以下を確認する。

- `metrics.aiFallbackCount` … AIを呼ぶ組数（少ないほど良い）
- `metrics.aiFreeRatio` … AIを呼ばずに済んだ割合
- `errors` … 取得に失敗したアーティスト（**§15 のとおり、1組の失敗で止めない**）
- `artists[].missingOnSite` … サイトで見つからなかった既存公演。
  **これを理由に削除しない**（掲載期間終了の可能性がある。COLLECTION_RULES §5.1）

### 3. AIキューを消化する

`cache/ai_queue.json` を**このファイルだけ**読む。各 item について:

- `changedLotteryText` … 変化した行のうち抽選（受付・当落・入金）に関わるもの
- `unparsedDateLines` … 日付はあるが会場を機械で特定できなかった行
- `parsedEventKeys` … 機械抽出済みの公演（`日付|会場|タイトル`）。**再解釈しない**。
  抽選をどの公演に紐付けるかの手がかりとしてだけ使う。公演そのものの反映元は
  レポートの `artists[].newEvents`
- `existingSummary` … 既存のツアーID・抽選ID・登録済み公演キー（重複回避用）

このテキストだけを根拠に、次の形のJSONを `cache/ai_result.json` に書く。

```json
{
  "results": [
    {
      "artistId": "yuzu",
      "events": [
        {
          "title": "TOUR 2026",
          "date": "2026-10-03",
          "venue": "さいたまスーパーアリーナ",
          "prefecture": "埼玉県",
          "openTime": "17:00",
          "startTime": "18:00",
          "detailUrl": "https://example.com/live/123"
        }
      ],
      "lotteries": [
        {
          "type": "FC先行",
          "entryStartAt": "2026-08-20T12:00:00+09:00",
          "entryEndAt": "2026-08-28T23:59:00+09:00",
          "resultAt": "2026-09-03T15:00:00+09:00",
          "paymentStartAt": null,
          "paymentEndAt": "2026-09-08T23:59:00+09:00",
          "evidence": "FC先行受付：2026年8月20日(木)12:00〜8月28日(金)23:59"
        }
      ]
    }
  ]
}
```

- **`evidence` には渡されたテキストの該当箇所をそのまま引用する**
  （COLLECTION_RULES §2.5 の根拠引用義務。引用できない日程は入力せず null）
- 渡されたテキストに無い日程を書かない。推測で埋めない
- 足りない情報は「要確認ポイント」として報告に回す。黙って null で放置しない

### 4. 検証してから反映する

```bash
python3 tools/validate_ai_result.py
```

- `error` が出たら該当項目を直すか落とす。**エラーを残したまま配信データに入れない**
- 通ったものは `cache/ai_accepted.json` に出る

### 5. 配信データへ反映

`cache/ai_accepted.json` と `/tmp/collect_report.json` の `newEvents` を、
`data/artist/{id}.json` へ既存スキーマのまま反映する（tours / performances / lotteries）。
ID命名・null ルール・削除ルールは COLLECTION_RULES.md に従う。

```bash
python3 tools/validate.py
python3 tools/update_manifest.py
```

### 6. commit & push

`data/artist/*.json` `data/artists.json` `data/manifest.json`
`config/collect_targets.json` `cache/collect_state.json` を同じcommitに含める。

---

## 完了報告に含めるもと

- 処理組数 / 変化なし / 変化あり / パーサー成功
- **AIを呼んだ組数と、そのアーティスト名・理由**
- AI入力の概算トークン（`metrics.aiInputTokensEstimated`）
- 取得に失敗したアーティスト一覧
- `missingOnSite`（削除はしていないこと）
- 要確認ポイント（公式サイト不明・JSレンダリングで読めない等）
