# AGENTS.md

このリポジトリはチケノートのDev配信用データ。実機・表示確認のための検証先で、productionとは別工程として扱う。

- レビュー済み変更は実機確認のためDevへ先行反映してよい。
- Dev反映をproduction承認とみなさない。production昇格はユーザーの明示承認後のみ。
- `data/artist_relations.json` はCollector用control fileで、iOSアプリが直接解釈するfeedではない。
- relation materializeでApple Music情報をmirrorしない。
- 根拠のない値を推測補完せず、無関係な既存データを壊さない。

データ変更時は `.agents/skills/project-change` を使用する。read-only確認では不要。
