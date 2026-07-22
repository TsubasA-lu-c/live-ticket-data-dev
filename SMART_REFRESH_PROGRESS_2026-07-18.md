# /refresh-smart 進捗（2026-07-18）

最終更新: 2026-07-22T09:16:17+09:00

## 現在地

- 全102組をhash比較し、82組を変更ありと判定。
- 60組は収集・validate・hash accept・manifest更新・commit・pushまで完了。
- 第12論理バッチまで配信反映済み。manifestはv82、未処理は22組。
- 次回は`ini`から再開。
- 未追跡のユーザーファイル `x_thread_artists.md` は変更・削除・commit禁止。

## 重要なチェックポイント運用

weekly limit到達時にもアプリへ途中成果を反映するため、今回は**5組ごとにmanifestとsource hashも含めてcommit/push**する。

1. 論理5組すべての担当収集を完了する。
2. 根拠報告とJSONを照合する。
3. `data/artists.json` の5組の `lastVerifiedAt` を更新する。
4. `python3 tools/validate.py` を実行し、対象組のWARNINGも確認する。
5. 成功した5組だけ `python3 tools/check_updates.py --accept ID...` を実行する。
6. `python3 tools/update_manifest.py` を実行する。
7. 再度 `python3 tools/validate.py` を実行する。
8. 5組のJSON、artists、manifest、source hash、この進捗mdを同じcommitに含めてpushする。
9. **push完了前に次バッチを編集し始めない。** manifestが次バッチの未commit hashを先取りするのを防ぐため。

`cache/source_hashes.pending.json` はgitignore対象。収集・validate前にacceptしない。失敗IDはpendingへ残す。

## 配信反映済み

### 第1バッチ

IDs: `golden_bomber`, `yonezu_kenshi`, `mizuki_nana`, `iris`, `official_hige_dandism`

- `ee13c43 refresh: ゴールデンボンバーなど5組を更新`（manifest v70）
- `b77b89a fix: i☆Ris先行抽選2件を追加`（manifest v71）
- i☆Risの抽選ゼロWARNINGを再確認し、イベンター先行・FC二次先行の漏れを修正済み。

### 第2バッチ

IDs: `back_number`, `ado`, `yoasobi`, `nogizaka46`, `hinatazaka46`

- `c872010 refresh: back numberなど5組を更新`（manifest v72）
- 乃木坂46の東京3次・大阪機材席、日向坂46の氣志團万博・アップグレード抽選などを反映。
- YOASOBIはJSレンダリングで本文取得不可。既存内容を保持して確認日更新。

### 第3バッチ

IDs: `aimyon`, `sakanaction`, `larc_en_ciel`, `king_gnu`, `vaundy`

- `5c15d77 refresh: あいみょんなど5組を更新`（manifest v73）
- あいみょん: 日本武道館2公演、2027ツアー36公演、AIM一次抽選。
- サカナクション: 2026年10公演の時刻修正。
- L'Arc-en-Ciel: 35周年ツアー16公演の時刻補完。
- King Gnu: 10周年KICKOFF 4公演。
- Vaundy: 新規4公演・3抽選、既存時刻・会場補完。

## 第4バッチ（配信反映済み）

- `eb52173 refresh: 藤井風など5組を更新`（manifest v74）

1. `fujii_kaze`
2. `spitz`
3. `lisa`
4. `hana`
5. `snow_man`

確認済み差分:

- 藤井風: 国内外ツアー料金、海外公演JSTを補完。広島・福井追加受付は種別未確認のため未登録。
- スピッツ: 料金と抽選入金期間を補完。
- LiSA: 15周年アジアツアー5公演とLACE UP FC先行を追加。欧州・UKツアーは日程未発表で未収集。
- HANA: `WANIMA presents 1CHANCE FESTIVAL 2026`とBorn to Bloomの終了済み抽選8件を追加。
- Snow Man: 将来の有観客ライブなし。空配列を維持して確認日更新。

第4バッチはmanifest込みでpush済み。

## 第5バッチ（配信反映済み）

- `a219f36 refresh: SixTONESなど5組を更新`（manifest v75）

IDs: `sixtones`, `one_ok_rock`, `radwimps`, `ryokuoushoku_shakai`, `yuuri`

- SixTONES: 新規差分なし。公開確認できない既存FC抽選の日程は据え置き。
- ONE OK ROCK: 1 CHANCE FESTIVAL 2026、一般受付3件を追加。
- RADWIMPS: 既存1ツアー・9公演・2抽選を公式照合。
- 緑黄色社会: 既存10公演を照合し、席種2件を追加。販売元未確認の先行2件は未追加。
- 優里: ロサンゼルス1公演、台北2公演を追加。
- validate成功、hash accept・manifest v75更新・commit・pushまで完了。

## 第6バッチ（配信反映済み）

IDs: `naniwa_danshi`, `hey_say_jump`, `travis_japan`, `twice`, `seventeen`

- なにわ男子: ND⁵を45公演へ拡充し、KAMIGATA EXPO PARK FES 2026を追加。FC受付2件とぴあ一般発売2件を整理。
- Hey! Say! JUMP: 山田涼介ドーム/アジアツアー、ミュージカル『ジョセフ』の3ツアー・35公演・7受付を追加。`百鬼夜行鏡`は月のみ公表で未収集。
- Travis Japan: SUMMER SONIC大阪出演日を8/14へ修正し、`けるとめる in めざましWANGANフェス`を追加。WANGANフェスは受付期間未確認のため抽選未作成。
- TWICE: 将来公演なし。国立競技場3公演は終了済みのため空配列を維持して確認日更新。
- SEVENTEEN: 将来公演なし。DxS/YAKUSOKUは終了済みのため空配列を維持して確認日更新。
- validate成功、hash accept・manifest v76更新・commit・pushまで完了。

## 第7バッチ（配信反映済み・停止地点）

IDs: `sky_hi`, `tennimu`, `touken_ranbu_musical`, `zutomayo`, `momoiro_clover_z`

- SKY-HI: `BMSG FES’26` 3公演と抽選2件を追加。`s**t kingz Fes 2026 会社`のFLYERS先行を補完。
- テニミュ: `The Final Stage`のTSC先行・モバイル先行を追加。7/18大阪公演時刻を17:30へ訂正。
- 刀ミュ: `刀剣乱舞 - ICE BLADE -` 2公演と抽選10種を追加。`月夜一縷`プレリク当落日時を補完。
- ずとまよ: 既存6イベントを公式照合し、フェス会場・出演時刻を補正。抽選4件は公式特設で根拠確認済み。
- ももクロ: 11イベント・15公演・8抽選へ整理。TIF優先エリア、桃神祭プレオーダー等を追加。
- validate成功、hash accept・manifest v77更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `macaroni_empitsu` から開始する。

## 第8バッチ（配信反映済み・停止地点）

IDs: `macaroni_empitsu`, `sakurazaka46`, `babymetal`, `mrs_green_apple`, `be_first`

- マカロニえんぴつ: マカロックツアーvol.22の12/26名古屋公演に重複していた誤レコードを除去し、紐づく3受付の対象公演IDを修正。
- 櫻坂46: ARENA TOUR広島・千葉4公演の一般発売（7/18 12:00開始、先着）を追加。BACKS LIVE FC2次先行とNEWSを公式照合。
- BABYMETAL: 公式TOP・NEWS・TOURを確認。CANNONBALL外伝ほか既存予定を維持。
- Mrs. GREEN APPLE: SHADOWS全28公演とFC+CD予約購入者限定2次先行を公式記事で再照合。
- BE:FIRST: ドームツアー料金に注釈付き指定席15,500円を追加。WORLD SHOWCASEは公開済みの4都市以外のチケット詳細未発表を確認。
- validate成功、hash accept・manifest v78更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `glay` から開始する。

## 第9バッチ（配信反映済み・停止地点）

IDs: `glay`, `yuzu`, `koda_kumi`, `kobukuro`, `timelesz`

- GLAY: EXOFIREのローソンチケット・LEncore会員先行を追加（対象11公演、7/1 15:00〜7/13 23:00、結果/決済7/17 15:00〜）。
- ゆず: 心音および30周年ライブのNEWS/TICKETを確認。新規受付はなし。
- 倖田來未: Live Tour 2026 Kingdomの全公演延期と払い戻しを公式確認。将来公演なしを維持。
- コブクロ: 霞日和の石川公演チケット表示・公式リセール開始を確認。新規受付はなし。
- timelesz: MOMENTUMの一般チケット発売中および千秋楽生配信を確認。生配信の視聴券詳細は未発表。
- validate成功、hash accept・manifest v79更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `milk` から開始する。

## 第10バッチ（配信反映済み・停止地点）

IDs: `milk`, `bump_of_chicken`, `novelbright`, `frederic`, `fruits_zipper`

- BUMP OF CHICKEN: Ratio Clavis追加プレオーダー1次が7/26 23:59までであることを公式NEWSで確認。
- Novelbright: 姫路9/26公演の各プレイガイド抽選先行（7/14 13:00〜7/21 23:59）を追加。
- M!LK、フレデリック、FRUITS ZIPPER: TOP・NEWSを確認し、公開済み以外の新規受付なし。
- validate成功、hash accept・manifest v80更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `cutie_street` から開始する。

## 第11バッチ（配信反映済み・停止地点）

IDs: `cutie_street`, `monaki`, `equal_love`, `super_beaver`, `wanima`

- CUTIE STREET: 日本武道館2周年公演のアップグレード抽選（7/9 18:00〜7/14 23:59）を追加。
- モナキ: 10/27豊洲PIT「ハロウィンやで☆しらんけど」とFC先行（7/16 12:00〜7/26 23:59）を追加。
- =LOVE: 9周年コンサートの公式料金・開場時刻を補完し、FC長期会員先行（会員＋会員）の開始時刻と根拠URLを補正。
- SUPER BEAVER、WANIMA: TOP・NEWS・既存チケット情報を再照合。新規受付なし。
- validate成功、hash accept・manifest v81更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `yabai_tshirts_yasan` から開始する。

## 第12バッチ（配信反映済み・停止地点）

IDs: `yabai_tshirts_yasan`, `the_oral_cigarettes`, `miura_daichi`, `kurayamisaka`, `starglow`

- ヤバイTシャツ屋さん: Magical Tank-top Parade 10・11月公演のオフィシャル3次先行（7/17 20:00〜7/21 23:59）を追加。
- THE ORAL CIGARETTES、三浦大知、kurayamisaka、STARGLOW: 公式TOP・NEWS・既存チケット情報を再照合。新規受付なし。
- validate成功、hash accept・manifest v82更新・commit・pushまで完了。
- ここで停止。次回は下記未処理順の先頭 `ini` から開始する。

## 第12バッチ後の未処理順（22組）

```text
ini
jo1
mazzel
not_equal_me
boku_aozora
da_ice
bts
enhypen
ive
aespa
le_sserafim
tomorrow_x_together
sandaime_j_soul_brothers
zorn
hypnosis_mic
uta_no_prince_sama
gre4n_boyz
ikimonogakari
and_team
aina_the_end
yorushika
urashimasakatasen
```

## WARNINGと要確認

- manifest更新前のhash WARNINGは予定どおり。update後に消えることを確認する。
- 既存カバレッジWARNINGは対象組の更新時に公式で確認する。機械的に無視しない。
- あいみょん2027、King Gnu KICKOFFはチケット日程未発表のため抽選ゼロが現時点で妥当。
- Vaundy上海2公演は公式が `STAY TUNED` で会場未発表のため `venue: null` が妥当。
- LiSAアジアツアーは公開ページで抽選日程を確認できず、抽選ゼロWARNINGを現時点で許容。
- 藤井風のハート席（こども）0円は公式料金どおりで、料金範囲WARNINGを許容。
- 優里のLA・台北公演は公開ページに抽選日程がないため、抽選ゼロWARNINGを現時点で許容。
- Travis JapanのWANGANフェスは公式・ぴあで抽選受付期間を確認できないため抽選ゼロWARNINGを現時点で許容。
- Hey! Say! JUMPのYES24／Thai Ticket Majorは直接取得が応答待ち。受付日時は公式アジアツアー特設ページ本文で確認。
- テニミュ立海前編は未来公演あり・抽選ゼロWARNING継続。今回確認範囲ではFinal Stage先行のみ日程根拠あり。
- ももクロXmas/歌合戦はチケット詳細未発表のため抽選ゼロWARNINGを現時点で許容。
- ももクロ立飛フェスの入場0円は公式無料入場どおりで料金範囲WARNINGを許容。
- 刀ミュICE BLADEの刀ステFC／ゲーム内／DMMプレミアム／2.5フレンズ先行は種別リンクのみで日時未確認のため日時null。

## 全組完了後

1. `python3 tools/cleanup_past.py`
2. 変更があればvalidate、manifest更新、commit、push。
3. pendingが消えたか、未成功IDだけになっていることを確認。
4. `python3 tools/update_manifest.py`
5. `python3 tools/validate.py`
6. 最終manifest・source hash・この進捗mdをcommit/push。
7. 更新組数、公演数、抽選数、要確認ポイント、manifest versionを報告。
