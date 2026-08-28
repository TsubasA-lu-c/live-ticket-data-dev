#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ARTIST = ROOT / "data" / "artist" / "enhypen.json"
MANIFEST = ROOT / "data" / "manifest.json"
EXPECTED_BASE_HASH = "e1f49f3634448f16"
VERIFIED_AT = "2026-08-28T18:39:00+09:00"


def sha16(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def add_unique(items: list[dict], values: list[dict], label: str) -> None:
    ids = [x.get("id") for x in items]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"duplicate existing {label} id")
    for value in values:
        object_id = value["id"]
        if object_id in ids:
            raise SystemExit(f"expected absent {label} id already exists: {object_id}")
        items.append(value)
        ids.append(object_id)


def main() -> int:
    raw = ARTIST.read_bytes()
    if sha16(raw) != EXPECTED_BASE_HASH:
        raise SystemExit(f"ENHYPEN baseline moved: expected {EXPECTED_BASE_HASH}, got {sha16(raw)}")

    data = json.loads(raw)
    if (len(data["tours"]), len(data["performances"]), len(data["lotteries"])) != (5, 13, 14):
        raise SystemExit("unexpected ENHYPEN baseline counts")

    tours = [
        {"id":"enhypen_the_sin_bliss_meet_greet_2026_1010","artistId":"enhypen","title":"ENHYPEN 8th Mini Album『THE SIN : BLISS』シリアルナンバー特典第三弾 ミート＆グリートイベント","startDate":"2026-10-10T00:00:00+09:00","endDate":"2026-10-10T00:00:00+09:00","prices":None,"source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/f986b38bfa34","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_long_sign_2026","artistId":"enhypen","title":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「メンバー個別ロングサイン会」","startDate":"2026-10-10T00:00:00+09:00","endDate":"2026-10-10T00:00:00+09:00","prices":None,"source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/a508ee24acf6","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_2shot_2026","artistId":"enhypen","title":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「メンバー個別2ショット撮影会」","startDate":"2026-09-05T00:00:00+09:00","endDate":"2026-09-05T00:00:00+09:00","prices":None,"source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/fece263cade3","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_premium_sign_2026","artistId":"enhypen","title":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「プレミアムサイン会」","startDate":"2026-10-10T00:00:00+09:00","endDate":"2026-10-10T00:00:00+09:00","prices":None,"source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/9579ffba3355","lastVerifiedAt":VERIFIED_AT},
    ]

    performances = [
        {"id":"enhypen_the_sin_bliss_meet_greet_2026_kanto_1010","tourId":"enhypen_the_sin_bliss_meet_greet_2026_1010","venue":"関東某所","performanceAt":None,"performanceDate":"2026-10-10","doorOpenAt":None,"kind":"oneman","eventName":"ENHYPEN 8th Mini Album『THE SIN : BLISS』シリアルナンバー特典第三弾 ミート＆グリートイベント","source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/f986b38bfa34","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_long_sign_2026_kanto_1010","tourId":"enhypen_the_sin_bliss_long_sign_2026","venue":"関東某所","performanceAt":None,"performanceDate":"2026-10-10","doorOpenAt":None,"kind":"oneman","eventName":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「メンバー個別ロングサイン会」","source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/a508ee24acf6","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_2shot_2026_kanto_0905","tourId":"enhypen_the_sin_bliss_2shot_2026","venue":"関東某所","performanceAt":None,"performanceDate":"2026-09-05","doorOpenAt":None,"kind":"oneman","eventName":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「メンバー個別2ショット撮影会」","source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/fece263cade3","lastVerifiedAt":VERIFIED_AT},
        {"id":"enhypen_the_sin_bliss_premium_sign_2026_makuhari_1010","tourId":"enhypen_the_sin_bliss_premium_sign_2026","venue":"幕張メッセ ホール1","performanceAt":None,"performanceDate":"2026-10-10","doorOpenAt":None,"kind":"oneman","eventName":"ENHYPEN 8th Mini Album『THE SIN : BLISS』発売記念「プレミアムサイン会」","source":"system","sourceUrl":"https://enhypen-jp.weverse.io/news/9579ffba3355","lastVerifiedAt":VERIFIED_AT},
    ]

    all_blood = [
        "enhypen_blood_saga_japan_2026_tokyo_1201","enhypen_blood_saga_japan_2026_tokyo_1202",
        "enhypen_blood_saga_japan_2026_nagoya_1226","enhypen_blood_saga_japan_2026_nagoya_1227",
        "enhypen_blood_saga_japan_2026_fukuoka_0206","enhypen_blood_saga_japan_2026_fukuoka_0207",
        "enhypen_blood_saga_japan_2026_osaka_0219","enhypen_blood_saga_japan_2026_osaka_0220",
    ]
    sendoff = all_blood[:4]

    def lottery(id_: str, tour: str, type_: str, start, end, result, pids, url):
        return {"id":id_,"tourId":tour,"type":type_,"entryStartAt":start,"entryEndAt":end,"resultAt":result,"paymentStartAt":None,"paymentEndAt":None,"performanceIds":pids,"source":"system","sourceUrl":url,"lastVerifiedAt":VERIFIED_AT}

    lotteries = [
        lottery("enhypen_the_sin_bliss_meet_greet_2026_1010_3rd","enhypen_the_sin_bliss_meet_greet_2026_1010","シリアルナンバー特典第三弾 第3回","2026-09-01T11:00:00+09:00","2026-09-11T10:00:00+09:00","2026-09-16T20:00:00+09:00",["enhypen_the_sin_bliss_meet_greet_2026_kanto_1010"],"https://enhypen-jp.weverse.io/news/f986b38bfa34"),
        lottery("enhypen_the_sin_bliss_meet_greet_2026_1010_4th","enhypen_the_sin_bliss_meet_greet_2026_1010","シリアルナンバー特典第三弾 第4回","2026-09-11T11:00:00+09:00","2026-09-22T10:00:00+09:00","2026-09-29T20:00:00+09:00",["enhypen_the_sin_bliss_meet_greet_2026_kanto_1010"],"https://enhypen-jp.weverse.io/news/f986b38bfa34"),
        lottery("enhypen_the_sin_bliss_meet_greet_2026_1010_5th","enhypen_the_sin_bliss_meet_greet_2026_1010","シリアルナンバー特典第三弾 第5回","2026-09-22T11:00:00+09:00","2026-10-02T10:00:00+09:00","2026-10-07T20:00:00+09:00",["enhypen_the_sin_bliss_meet_greet_2026_kanto_1010"],"https://enhypen-jp.weverse.io/news/f986b38bfa34"),
        lottery("enhypen_the_sin_bliss_long_sign_2026_entry","enhypen_the_sin_bliss_long_sign_2026","メンバー個別ロングサイン会応募","2026-08-25T10:00:00+09:00","2026-09-06T23:59:00+09:00","2026-09-18T18:00:00+09:00",["enhypen_the_sin_bliss_long_sign_2026_kanto_1010"],"https://enhypen-jp.weverse.io/news/a508ee24acf6"),
        lottery("enhypen_the_sin_bliss_2shot_2026_entry","enhypen_the_sin_bliss_2shot_2026","メンバー個別2ショット撮影会応募商品（自動エントリー）","2026-08-24T18:00:00+09:00","2026-08-27T17:59:00+09:00","2026-09-02T20:00:00+09:00",["enhypen_the_sin_bliss_2shot_2026_kanto_0905"],"https://enhypen-jp.weverse.io/news/fece263cade3"),
        lottery("enhypen_the_sin_bliss_premium_sign_2026_entry","enhypen_the_sin_bliss_premium_sign_2026","プレミアムサイン会応募商品（自動エントリー）","2026-08-27T18:00:00+09:00","2026-09-08T17:59:00+09:00","2026-09-18T20:00:00+09:00",["enhypen_the_sin_bliss_premium_sign_2026_makuhari_1010"],"https://enhypen-jp.weverse.io/news/9579ffba3355"),
        lottery("enhypen_blood_saga_japan_2026_sendoff_1st","enhypen_blood_saga_japan_2026","公演後メンバー全員お見送り会 第1回応募商品（自動エントリー）","2026-08-18T18:00:00+09:00","2026-08-23T23:59:00+09:00","2026-09-01T20:00:00+09:00",sendoff,"https://enhypen-jp.weverse.io/news/1079d4b1e481"),
        lottery("enhypen_blood_saga_japan_2026_sendoff_2nd","enhypen_blood_saga_japan_2026","公演後メンバー全員お見送り会 第2回応募商品（自動エントリー）","2026-08-24T00:00:00+09:00","2026-09-02T17:59:00+09:00","2026-09-11T20:00:00+09:00",sendoff,"https://enhypen-jp.weverse.io/news/1079d4b1e481"),
        lottery("enhypen_blood_saga_japan_2026_lawson_first_come","enhypen_blood_saga_japan_2026","ローソンチケット先着先行","2026-08-29T13:00:00+09:00",None,None,all_blood,"https://enhypen-jp.weverse.io/news/122a8f0efcc7"),
    ]

    add_unique(data["tours"], tours, "tour")
    add_unique(data["performances"], performances, "performance")
    add_unique(data["lotteries"], lotteries, "lottery")

    if (len(data["tours"]), len(data["performances"]), len(data["lotteries"])) != (9, 17, 23):
        raise SystemExit("unexpected post-apply counts")

    tour_ids = {x["id"] for x in data["tours"]}
    perf_ids = {x["id"] for x in data["performances"]}
    for p in data["performances"]:
        if p["tourId"] not in tour_ids:
            raise SystemExit(f"dangling performance tourId: {p['id']}")
    for l in data["lotteries"]:
        if l["tourId"] not in tour_ids:
            raise SystemExit(f"dangling lottery tourId: {l['id']}")
        for pid in l.get("performanceIds") or []:
            if pid not in perf_ids:
                raise SystemExit(f"dangling performanceId in {l['id']}: {pid}")

    ARTIST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artist_hash = sha16(ARTIST.read_bytes())
    if artist_hash != "87aeca74ef15e293":
        raise SystemExit(f"unexpected resulting artist hash: {artist_hash}")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != 164 or manifest["artists"]["enhypen"]["hash"] != EXPECTED_BASE_HASH:
        raise SystemExit("manifest baseline moved")
    manifest["version"] = 165
    manifest["updatedAt"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    manifest["artists"]["enhypen"]["hash"] = artist_hash
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("ENHYPEN apply OK")
    print(f"counts tours={len(data['tours'])} performances={len(data['performances'])} lotteries={len(data['lotteries'])}")
    print(f"artist hash={artist_hash} manifest version={manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
