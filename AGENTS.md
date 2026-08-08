# Codex 運用ガイド

`CLAUDE.md` をこのリポジトリの運用ルールの唯一の正本とする。
Codex は作業開始前に `CLAUDE.md` と、そこから参照される
`COLLECTION_RULES.md` を読み、それらに従うこと。共通ルールはこのファイルに複製しない。

## Codex 向けの読み替え

- `sonnet` 指定は、新規収集や厳密な根拠照合に向く高品質モデル
  （現在は `gpt-5.6-sol`）を意味する。
- `haiku` 指定は、既存データの差分更新に向く高速・バランス型モデル
  （現在は `gpt-5.6-terra`）を意味する。
- `WebFetch` は、Codex で利用できるWebページの直接取得、ブラウザ、または
  HTTP取得に読み替える。検索結果のAI要約は根拠にしない。
- 標準は5組単位の並列バッチだが、ユーザーがコスト優先・直列を指定した場合は、
  更新処理の種類を問わず（`refresh-smart` / `refresh-hot` / `refresh-all` / `add-artists` 等）、
  1組ずつ実行して10組単位で共通ファイルを更新・commitする方式を選べる。
  並列・直列のどちらでも、各commit/push単位で `update_manifest.py` を実行し、
  成功したアーティストの `data/artists.json` と `data/manifest.json` を同じpushに必ず含める。
- `/refresh-smart` などの表記はワークフロー名として扱う。Codexの専用スラッシュ
  コマンドがなくても、`CLAUDE.md` 記載の手順をそのまま実行する。
