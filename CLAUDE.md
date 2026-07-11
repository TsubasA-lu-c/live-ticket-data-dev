# live-ticket-data-dev 運用ガイド

> このリポジトリでの作業は sonnet が担当。
> 詳細ルールは COLLECTION_RULES.md を参照。
> **作業前に必ず `git pull origin main` を実行してから作業を開始すること。**
> **必ず validate.py を通過してから push すること。**

---

## このリポジトリの位置づけ（2026-07確定）

`live-ticket-data`（本番・`https://live-ticket-data.pages.dev`）とは**完全に別のGitHubリポジトリ**。
アプリ側の開発・検証ビルド（`run_dev_devdata.command`）はこのリポジトリのCloudflare Pages配信
（`https://live-ticket-data-dev.pages.dev`）を向いており、本番ユーザーに影響を与えずに
データ収集・スキーマ変更を試せる。

- ここでの作業・pushは本番に一切影響しない（別リポジトリ・別デプロイのため）
- 検証で問題ないと確認できた変更は、本番の `live-ticket-data` リポジトリ側で改めて同じ変更を行う（自動同期はしない）
- ブランチは通常の `main` のみ。dev/mainのブランチ分けはしない（過去に試して廃止した方式）

## 更新指示を受けたときの確認ルール（必須）

「更新して」「refreshして」など更新系の指示を受けたときは、
**実行前に必ず以下を提示してユーザーに確認する。**

「続きをやって」「続きから」など前回の継続を示す指示の場合は確認不要。
前回と同じ更新方法（メモリまたは会話履歴から判断）をそのまま引き継いで実行する。

```
どの更新方法を使いますか？

1. /refresh-smart（推奨・毎週）
   公式サイトに変化があったアーティストのみ更新。セッション消費が少ない。
   ※ 公式TOP＋cache/watch_urls.json に登録した NEWS/LIVE ページを監視。
     未登録ページに情報がある場合は見落としの可能性あり。

2. /refresh-hot（週次フォールバック）
   抽選締切が90日以内のアーティストのみ更新。差分チェックなしで直接取得するため精度高め。

3. /refresh-all（月次）
   全アーティストを更新。最も精度が高いが時間・セッション消費が多い。
```

ユーザーが選択してから実行に移る。独断で開始しない。

---

## 作業の種類と手順

### A. アーティスト追加（新規）

1. **既存データの読み込み**
   - `data/artists.json` を読む（既存ID・artistId を把握）
   - `data/artist/` の一覧を取得（ファイル名から既存artistId を把握）

2. **正規名称の解決**（COLLECTION_RULES.md §3.5）
   - 入力名をウェブ検索して公式サイトで正規表記を確認
   - id は英小文字スネーク（例: `mr_children`）

3. **情報収集**（COLLECTION_RULES.md §4）
   - 公式サイトからツアー・公演・抽選情報を取得
   - 不明な値は null（推測で埋めない）

4. **ファイルの作成・更新**
   - `data/artist/{id}.json` を作成（下記テンプレート参照）
   - `data/artists.json` に一覧エントリを追記

5. **バリデーション**（必須）
   ```
   python3 tools/validate.py
   ```
   エラーがあれば修正して再実行。

6. **manifest 更新**（必須）
   ```
   python3 tools/update_manifest.py
   ```

7. **監視URL登録**（必須・/refresh-smart の見落とし防止）
   ```
   python3 tools/discover_watch_urls.py {id}
   ```

8. **commit & push**
   ```
   git add data/artist/{id}.json data/artists.json data/manifest.json cache/watch_urls.json
   git commit -m "add: {アーティスト名}を追加"
   git push origin main
   ```

---

### B. アーティスト更新（鮮度更新・新情報追加）

1. `data/artist/{id}.json` を読む（既存データを把握）
2. 公式サイト・チケットサイトを確認（最新ツアー・抽選情報）
3. 変更を反映（終了データの除外は COLLECTION_RULES.md §5.1 に従う）
4. `python3 tools/cleanup_past.py` を実行（全公演が過去のツアーを自動削除）
5. `python3 tools/validate.py` を実行（エラーなし確認）
6. `python3 tools/update_manifest.py` を実行
7. `git add data/artist/{id}.json data/artists.json data/manifest.json && git commit -m "update: {アーティスト名}情報更新" && git push origin main`

---

---

## 1組×5並列、5組完了ごとにcommit（C・D・E 共通）

**1つのサブエージェントが1組を担当し、5つを同時起動（background）する。**
5組全完了後にartists.jsonを一括更新してcommit & push。これを繰り返す。
途中でセッションリミットに達しても、完了済みグループはpush済みのため損失は最大5組分。

### 役割分担（違反厳禁）

| 役割 | 書いてよいファイル | 触ってはいけないファイル |
|---|---|---|
| **サブエージェント（1組担当）** | `data/artist/{担当id}.json` のみ | `data/artists.json` / `data/manifest.json` / 他アーティストのファイル |
| **メインエージェント** | `data/artists.json` / `data/manifest.json` | （グループ全完了後のみ操作） |

### 実行フロー

```
メイン: 対象アーティストを5組ずつのグループに分割
  ↓
【グループ①】1組×5つのサブを同時起動（run_in_background: true）
  ↓ 全5つの完了通知を待つ
メイン: data/artists.json を5組分一括更新 → validate.py → commit & push
  ↓
【グループ②】次の5組で同様に繰り返し
  ↓ …繰り返し…
全グループ完了後: update_manifest.py → commit & push
```

- サブは `run_in_background: true` で5つ同時起動する
- artists.jsonの更新は**グループ全員完了後に一括で**行う（途中更新は不整合の原因）
- manifest更新（`update_manifest.py`）は**全グループ完了後に1回だけ**
- **メインは commit 前に各サブの根拠引用と JSON 入力値を照合する**（引用がない・引用と食い違う日程は null に修正するか再収集を指示。validate.py の WARNING も「抜け漏れの疑い」として内容確認してから push）
- **メインは commit 前に各サブの「確認したページのURL一覧」に NEWS ページが含まれているか必ず確認する**（2026-07にサカナクションのツアー拡大情報がNEWSページ未確認により見落とされた事例あり。LIVE/TOURページのみの報告は不完全とみなし、そのアーティストだけ再収集を指示する。COLLECTION_RULES.md §2.7参照）

### サブエージェントへの指示テンプレート

各サブエージェントには以下を伝える:
- 担当アーティストID（**1組のみ**）
- 実施内容（追加 or 更新 or 更新+終了掃除）
- **`data/artist/{id}.json` のみ書くこと（artists.json/manifest.json は触らない）**
- 完了後に `lastVerifiedAt`（更新日時）と参照URLを報告すること
- **完了報告に以下を必ず含めること（COLLECTION_RULES.md §2.5「根拠引用の義務」）:**
  - 確認したページのURL一覧（LIVE/TOURページ・NEWSページ・辿った個別記事・チケットサイト）
  - 収集・更新した各抽選日程の**根拠引用**（取得ページ内の日程が書かれている一文＋URL）。引用できない日程は入力せず null にする
  - 発見したライブ関連イベント数と収集したイベント数（差があれば理由を明記）
- validate.py は**実行しない**（メインが一括で行う）
- **情報収集の注意点（必ず守ること）:**
  - **`sourceUrl` がツアー特設ページ等の深いページの場合でも、公式TOPページとNEWS一覧ページは必ず別途WebFetchで確認する**（`sourceUrl` の確認だけで済ませない）。完了報告の確認URL一覧にNEWSページのURLが無い場合、収集漏れとみなされ再収集となる
  - 公式サイトの schedule・news 一覧ページを確認したら、個別記事リンク（`/news/detail/*` 等）も必ずWebFetchで開いて詳細を確認する
  - 「日時不明」と判断する前に、一覧ページ内のリンクを最低1階層辿ること
  - `performanceAt` は null 禁止。不明な場合は `18:00:00` をデフォルトで設定する
  - **WebSearch の AI 生成サマリーから日程・締切を転記してはならない**（検索AIは日程を混同・創作することがある）。必ず `WebFetch` で公式/正規チケットページを直接取得し、そのページに日程が明記されていることを確認してから入力する
  - **異なる種別の日程を流用してはならない**（例: FC先行の締切日を一般先行に使いまわすことは禁止）
  - **存在を確認していないURLをパスで推測して試すことは禁止**。URLは必ず WebFetch で取得したページ内のリンクから辿る。公式サイトが JS レンダリングで読めない場合は COLLECTION_RULES.md §2.6 の回避手順（curl で生HTML確認 → RSS/埋め込みJSON → 正規チケットサイト → WebSearch でURL探索）に従う。外部レンダリングサービスは使わない
  - 受付終了済みの抽選（Lottery）もツアーが継続中であれば必ず残す・収集し続ける（ユーザーが当落・入金状況を管理するために必要）
  - `performance.kind` は `"oneman"` / `"fes"` / `"taiban"` のみ使用可。それ以外の値は禁止（アプリが認識しない）。迷ったら `"oneman"` を選ぶ
- **削除ルール（必ず守ること）:**
  - **Lottery（抽選）の削除は、対象 performanceIds の公演が全て終了した場合のみ**。抽選受付期間が過ぎているだけでは削除しない（ユーザーが入金・当落状況をステータス管理するため）
  - **Performance（公演）の削除は、ツアー配下の全公演が終了した場合のみ**。ツアーに未来の公演が1つでもあれば終了済み公演も残す
  - 詳細は COLLECTION_RULES.md §5.1 を参照すること

---

### C. スマート差分更新（/refresh-smart）【推奨・毎週】

公式サイトに変化があったアーティストだけを自動検出して更新する。

1. `python3 tools/check_updates.py 2>/dev/null > /tmp/changed.txt` を実行
2. `/tmp/changed.txt` を読み込み、対象IDを5組ずつグループに分割
3. グループごとに1組×5並列で実行:
   - サブエージェント（haiku）を5つ同時起動（background）→ 全完了を待つ
   - `data/artists.json` の `lastVerifiedAt` を5組分一括更新
   - `python3 tools/validate.py`（エラーがあれば修正）
   - `git add data/artist/... data/artists.json && git commit -m "refresh: {アーティスト名}など" && git push origin main`
4. 全グループ完了後:
   - `python3 tools/cleanup_past.py` → `git add data/artist/*.json && git commit -m "cleanup: 終了ツアー削除" && git push origin main`
   - `python3 tools/update_manifest.py` → `git add data/manifest.json && git commit -m "update: manifest" && git push origin main`
   - `git add cache/source_hashes.json && git commit -m "update: source hash cache" && git push origin main`

---

### D. 毎週の鮮度更新（/refresh-hot）

Hot tier（3ヶ月以内に抽選締切があるアーティスト）のみを更新する。（check_updates.py が使えない場合のフォールバック）

1. 全 `data/artist/*.json` を読み込み、Hot tier を抽出
   - 判定: `lotteries[].entryEndAt` が今日から90日以内かつ未来のものが1件以上あるか
2. 抽出したアーティストを5組ずつのグループに分割
3. グループごとに1組×5並列で実行:
   - サブエージェント（haiku）を5つ同時起動（background）→ 全完了を待つ
   - `data/artists.json` の `lastVerifiedAt` を5組分一括更新
   - `python3 tools/validate.py`（エラーがあれば修正）
   - `git add data/artist/... data/artists.json && git commit -m "refresh: Hot tier {アーティスト名}など" && git push origin main`
4. 全グループ完了後:
   - `python3 tools/cleanup_past.py` → `git add data/artist/*.json && git commit -m "cleanup: 終了ツアー削除" && git push origin main`
   - `python3 tools/update_manifest.py` → `git add data/manifest.json && git commit -m "update: manifest" && git push origin main`

---

### E. 月次の全件更新（/refresh-all）

全アーティストを更新 + 終了ツアーの掃除を行う。

1. `data/artists.json` を読んで全 id を取得
2. 全アーティストを5組ずつのグループに分割
3. グループごとに1組×5並列で実行:
   - サブエージェント（haiku）を5つ同時起動（background）→ 全完了を待つ
   - `data/artists.json` の `lastVerifiedAt` を5組分一括更新
   - `python3 tools/validate.py`（エラーがあれば修正）
   - `git add data/artist/... data/artists.json && git commit -m "refresh: {アーティスト名}など更新" && git push origin main`
4. 全グループ完了後:
   - `python3 tools/cleanup_past.py` → `git add data/artist/*.json && git commit -m "cleanup: 終了ツアー削除" && git push origin main`
   - `python3 tools/update_manifest.py` → `git add data/manifest.json && git commit -m "update: manifest" && git push origin main`

---

### F. 新規アーティスト追加バッチ（/add-artists）

1. 追加対象リストを確認（TREND_NOTES.md・ジャンルバランスを参照）
2. 対象を5組ずつのグループに分割
3. グループごとに1組×5並列で実行:
   - サブエージェント（sonnet）を5つ同時起動（background）→ 全完了を待つ
   - `data/artists.json` に5組のエントリを一括追記
   - `python3 tools/validate.py`（エラーがあれば修正）
   - `git add data/artist/... data/artists.json && git commit -m "add: {アーティスト名}など" && git push origin main`
4. 全グループ完了後: `python3 tools/update_manifest.py` → `git add data/manifest.json && git commit -m "update: manifest" && git push origin main`

---

## スキーマテンプレート

### `data/artists.json` への追記エントリ
```json
{
  "id": "artist_id",
  "name": "アーティスト正規名",
  "aliases": ["略称1", "略称2"],
  "genre": "バンド|ソロ|アイドル|K-POP|声優|ユニット|ヒップホップ",
  "imageUrl": null,
  "source": "system",
  "sourceUrl": "https://公式サイトURL",
  "lastVerifiedAt": "2026-06-14T00:00:00+09:00"
}
```

### `data/artist/{id}.json` テンプレート
```json
{
  "artistId": "artist_id",
  "tours": [
    {
      "id": "artistid_tourslug_2026",
      "artistId": "artist_id",
      "title": "ツアー名",
      "startDate": "2026-08-01T00:00:00+09:00",
      "endDate": "2026-09-30T00:00:00+09:00",
      "prices": [
        {"label": "指定席", "amount": 8800}
      ],
      "source": "system",
      "sourceUrl": "https://公式URL",
      "lastVerifiedAt": "2026-06-14T00:00:00+09:00"
    }
  ],
  "performances": [
    {
      "id": "tourId_会場省略_mmdd",
      "tourId": "上記tourのid",
      "venue": "会場名（正式名称）",
      "performanceAt": "2026-08-01T18:00:00+09:00",
      "doorOpenAt": "2026-08-01T17:00:00+09:00",
      "kind": "oneman",
      "eventName": null,
      "source": "system",
      "sourceUrl": "https://公式URL",
      "lastVerifiedAt": "2026-06-14T00:00:00+09:00"
    }
  ],
  "lotteries": [
    {
      "id": "tourId_fc先行",
      "tourId": "上記tourのid",
      "type": "FC先行",
      "entryStartAt": "2026-06-01T12:00:00+09:00",
      "entryEndAt": "2026-06-20T23:59:00+09:00",
      "resultAt": "2026-06-30T12:00:00+09:00",
      "paymentStartAt": null,
      "paymentEndAt": null,
      "performanceIds": ["上記performanceのid"],
      "source": "system",
      "sourceUrl": "https://公式URL",
      "lastVerifiedAt": "2026-06-14T00:00:00+09:00"
    }
  ]
}
```

---

## モデル運用ルール（サブエージェント）

バッチサブエージェントを起動する際は、作業種別に応じてモデルを使い分ける。

| 作業 | モデル | 理由 |
|------|--------|------|
| `/add-artists`（新規追加） | `sonnet` | WebFetchした生HTMLから情報を正確に抽出する必要がある。質が重要で後からのリカバリーが面倒 |
| `/refresh-smart`（差分更新） | `haiku` | 既存データがあり差分チェックが主体。単純な構造化タスクなのでhaikuで十分 |
| `/refresh-hot`（鮮度更新） | `haiku` | 同上 |
| `/refresh-all`（全件更新） | `haiku` | 同上 |

Agent呼び出し時に `model: "sonnet"` または `model: "haiku"` を明示すること。

---

## よくある間違いと防止策

| 間違い | 防止策 |
|-------|-------|
| IDを重複させる | 作業前に必ず data/artist/ の全ファイルを読んで既存IDを把握 |
| tourIdの参照ミス | performance/lottery の tourId は同一ファイル内の tour.id と一致させる |
| 日時にオフセットなし | 必ず `+09:00` を付ける（例: `2026-08-01T18:00:00+09:00`） |
| 推測で埋める | 不明な値は必ず null |
| validate.py をスキップ | push 前に必ず実行（エラーがあれば修正してから） |
| manifest を手動更新 | update_manifest.py を使う（hash の手動計算は禁止） |
