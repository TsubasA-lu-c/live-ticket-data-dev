# live-ticket-data-dev

チケノート向けの公開ライブ情報データリポジトリです。

この公開リポジトリには配信用データのみを置き、収集・巡回・AI処理・運用自動化の実装は含めません。

## Public data

- `data/artists.json` — アーティスト一覧
- `data/manifest.json` — Artist詳細配信データのmanifest
- `data/artist/*.json` — アーティスト別ライブ情報
- `data/artist_relations.json` — Collector/data promotion用のユーザー承認済みArtist間ライブ表示方針

### `artist_relations.json` の契約

`artist_relations.json` は人物相関図の網羅リストではなく、ライブ表示方針についてユーザー判断が済んだ関係だけを保持するallowlist / control fileです。未掲載の関係を「無関係」と解釈してはいけません。

このファイルは**チケノートiOSアプリがrelationを解釈するためのfeedではありません**。App側は既存のArtist詳細データだけを読みます。

承認済みの正方向relation (`same_person_alias` / `group_member_included`) は、非公開Collectorの決定論的materialize処理によって `data/artist/*.json` へ通常のTour / Performance / Lotteryとして展開されます。そのためApp本体にrelation専用実装は不要です。

相互表示で複数名義が混在するArtist詳細では、`tour.title` の先頭にcanonical source名義を `【Artist名】` 形式で必ず付与します。公式の元タイトルとsource Artist / source object IDはrelation metadataに保持し、delivery mirrorであることを追跡可能にします。

Apple Music Artist、artwork、Top Songs / `appleMusicTracks` はrelation materializeの対象外で、各Artist固有のままです。

`data/artist_relations.json` 自体はApp配信manifestのhash管理対象には含めません。Appが直接取得しないCollector制御ファイルだからです。一方、materialize後の `data/artist/*.json` は通常どおり `manifest.json` のhash対象です。

スキーマ・Artist ID参照・relation type・direction・重複/矛盾・delivery materialization整合性は、非公開の収集基盤 `TsubasA-lu-c/chatgpt-workspace/live-ticket-collector/source/tools/validate.py` の通常チェックに含まれます。relation専用ロジックは同基盤の `tools/validate_artist_relations.py` と `tools/materialize_artist_live_relations.py` を正とします。

収集処理の内部実装は非公開です。
