# live-ticket-data-dev

チケノート向けの公開ライブ情報データリポジトリです。

この公開リポジトリには配信用データのみを置き、収集・巡回・AI処理・運用自動化の実装は含めません。

## Public data

- `data/artists.json` — アーティスト一覧
- `data/manifest.json` — Artist詳細配信データのmanifest
- `data/artist/*.json` — アーティスト別ライブ情報
- `data/artist_relations.json` — ユーザー承認済みのArtist間ライブ表示方針

### `artist_relations.json` の配信契約

`artist_relations.json` は小さい制御ファイルとしてアプリが独立取得し、HTTP ETagで更新確認します。
`data/manifest.json` のhash管理対象には含めません。Artist詳細データの更新とrelation承認の更新を独立させるためです。

このファイルは人物相関図の網羅リストではなく、ライブ表示方針についてユーザー判断が済んだ関係だけを保持するallowlistです。未掲載の関係を「無関係」と解釈してはいけません。

スキーマ・Artist ID参照・relation type・direction・重複/矛盾の検証は非公開の収集基盤 `TsubasA-lu-c/chatgpt-workspace/live-ticket-collector/source/tools/validate.py` で行います。

収集処理の内部実装は非公開です。
