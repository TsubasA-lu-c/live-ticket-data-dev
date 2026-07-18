# /refresh-smart 進捗（2026-07-18）

最終更新: 2026-07-18T18:55:00+09:00

## 現在地

- 全102組をhash比較し、82組を変更ありと判定。
- 15組は収集・validate・hash accept・manifest更新・commit・pushまで完了。
- 第4論理バッチ5組は収集・validate・hash accept・manifest v74まで完了。commit・push待ち。
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

## 作業中: 第4バッチ

1. `fujii_kaze` — validate・accept・manifest v74完了、未commit
2. `spitz` — validate・accept・manifest v74完了、未commit
3. `lisa` — validate・accept・manifest v74完了、未commit
4. `hana` — validate・accept・manifest v74完了、未commit
5. `snow_man` — validate・accept・manifest v74完了、未commit

確認済み差分:

- 藤井風: 国内外ツアー料金、海外公演JSTを補完。広島・福井追加受付は種別未確認のため未登録。
- スピッツ: 料金と抽選入金期間を補完。
- LiSA: 15周年アジアツアー5公演とLACE UP FC先行を追加。欧州・UKツアーは日程未発表で未収集。
- HANA: `WANIMA presents 1CHANCE FESTIVAL 2026`とBorn to Bloomの終了済み抽選8件を追加。
- Snow Man: 将来の有観客ライブなし。空配列を維持して確認日更新。

再開時は5組のartists同期を確認し、上のチェックポイント手順でmanifest込みpushする。

## 第4バッチ後の未処理順（62組）

```text
sixtones
one_ok_rock
radwimps
ryokuoushoku_shakai
yuuri
naniwa_danshi
hey_say_jump
travis_japan
twice
seventeen
sky_hi
tennimu
touken_ranbu_musical
zutomayo
momoiro_clover_z
macaroni_empitsu
sakurazaka46
babymetal
mrs_green_apple
be_first
glay
yuzu
koda_kumi
kobukuro
timelesz
milk
bump_of_chicken
novelbright
frederic
fruits_zipper
cutie_street
monaki
equal_love
super_beaver
wanima
yabai_tshirts_yasan
the_oral_cigarettes
miura_daichi
kurayamisaka
starglow
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

## 全組完了後

1. `python3 tools/cleanup_past.py`
2. 変更があればvalidate、manifest更新、commit、push。
3. pendingが消えたか、未成功IDだけになっていることを確認。
4. `python3 tools/update_manifest.py`
5. `python3 tools/validate.py`
6. 最終manifest・source hash・この進捗mdをcommit/push。
7. 更新組数、公演数、抽選数、要確認ポイント、manifest versionを報告。
