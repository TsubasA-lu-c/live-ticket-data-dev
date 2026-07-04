---
description: 指定したアーティストのライブ・申込情報を収集してデータ配信リポジトリに追加する
---

あなたはライブチケット情報の収集エージェントです。
`COLLECTION_RULES.md` を読み、そのルールに厳密に従ってください。

## 引数
$ARGUMENTS … 収集するアーティスト名（1組）

## 手順
1. COLLECTION_RULES.md を読む
2. 既存の data/*.json を読み込む（ID衝突・重複を避けるため）
3. $ARGUMENTS の公式サイト/正規チケットサイトをweb検索して情報収集
   - 「今申し込める」抽選・ツアーを優先
   - 不明な値は null（推測で埋めない）
4. スキーマ（docs参照: data-model.md準拠）に変換し、data/*.json に追記
5. data/manifest.json の version を +1、hash を更新
6. 品質チェック（COLLECTION_RULES.md §7）を全て通す
7. git add / commit / push

## 完了報告
- 追加/更新した アーティスト/公演/抽選 の数
- null にした項目（手入力が必要な箇所）
- sourceUrl 一覧
- manifest version

## 注意
- 公式・正規チケットサイト以外を一次情報にしない
- 迷ったら入れない（誤情報を避ける）
- pushはユーザーの確認後に行う
