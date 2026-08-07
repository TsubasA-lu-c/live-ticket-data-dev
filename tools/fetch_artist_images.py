#!/usr/bin/env python3
"""Apple Music のアーティスト画像URLと抽出済み配色を取得する。

Apple Music の公開アーティストページには `serialized-server-data` という
埋め込みJSONがあり、その `artistDetailHeaderLockup` セクションに本人のヘッダー情報が入る。
ここから以下が **Developer Token なしで** 取れる。

    artwork.dictionary.url         … "…/{w}x{h}{c}.{f}" 形式のテンプレート
    artwork.dictionary.bgColor     … アートワークから抽出済みの背景色
    artwork.dictionary.textColor1..4 … 同じく前景色

`bgColor` は MusicKit でしか取れないと想定していたが、この経路で取得できる
（設計: `live_ticket_app/docs/design/2026-08-v2-design-decisions.md` §1.4 / §4.1）。

**og:image は使わない。** アーティストによっては編集用バナーが入っており、
ページ内の `AMCArtistImages` を出現順で拾うと類似アーティストの画像を掴む
（米津玄師のページで実際に別バンドの画像を掴んだ）。
本人であることは `storeAdamID` の一致で検証する。

使い方:
    python3 tools/fetch_artist_images.py
    python3 tools/fetch_artist_images.py --force   # 取得済みも引き直す
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = REPO / "cache" / "apple_music_candidates.json"

SLEEP_SEC = 1.5
TIMEOUT_SEC = 25
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SERIALIZED_RE = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S
)


def artist_page_url(row: dict) -> tuple[str, str] | None:
    """(ページURL, 期待するartistId) を返す。"""
    m = row.get("matched") or {}
    aid = m.get("artistId")
    if not aid and row.get("candidates"):
        aid = row["candidates"][0]["artistId"]
    if not aid:
        return None
    for c in row.get("candidates") or []:
        if c["artistId"] == aid and c.get("url"):
            return c["url"].split("?")[0], str(aid)
    return f"https://music.apple.com/jp/artist/_/{aid}", str(aid)


def find_header(page: dict) -> dict | None:
    """artistDetailHeaderLockup セクションの最初の item を返す。"""
    try:
        sections = page["data"][0]["data"]["sections"]
    except (KeyError, IndexError, TypeError):
        return None
    for sec in sections:
        if sec.get("itemKind") == "artistDetailHeaderLockup":
            items = sec.get("items") or []
            if items:
                return items[0]
    return None


def scrape(url: str, expect_id: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            html = res.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"    ! ページ取得失敗: {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(SLEEP_SEC)

    hit = SERIALIZED_RE.search(html)
    if not hit:
        print("    ! serialized-server-data が見つからない", file=sys.stderr)
        return None
    try:
        page = json.loads(hit.group(1))
    except json.JSONDecodeError as exc:
        print(f"    ! JSON解析失敗: {exc}", file=sys.stderr)
        return None

    item = find_header(page)
    if not item:
        print("    ! artistDetailHeaderLockup なし", file=sys.stderr)
        return None

    # 掴んだのが本人か検証する。別人の画像を保存すると気づきにくい。
    got_id = (
        (item.get("contentDescriptor") or {})
        .get("identifiers", {})
        .get("storeAdamID")
    )
    if got_id and str(got_id) != expect_id:
        print(f"    ! ID不一致 expect={expect_id} got={got_id}", file=sys.stderr)
        return None

    # artwork が null でも uberArtwork（ページ上部の大判画像）に入っていることがある。
    art: dict = {}
    for key in ("artwork", "uberArtwork"):
        cand = ((item.get(key) or {}).get("dictionary")) or {}
        if cand.get("url"):
            art = cand
            break
    if not art.get("url"):
        return None

    return {
        "imageTemplate": art["url"],
        "bgColor": art.get("bgColor"),
        "textColor1": art.get("textColor1"),
        "textColor2": art.get("textColor2"),
        "textColor3": art.get("textColor3"),
        "textColor4": art.get("textColor4"),
        "width": art.get("width"),
        "height": art.get("height"),
        "appleName": item.get("title"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="取得済みも引き直す")
    args = parser.parse_args()

    rows = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    total = len(rows)
    got = 0
    missing: list[str] = []

    for i, row in enumerate(rows, 1):
        if row.get("artistHeader") and not args.force:
            got += 1
            continue
        target = artist_page_url(row)
        if not target:
            missing.append(row["name"])
            continue
        url, expect_id = target
        print(f"[{i}/{total}] {row['name']}")
        info = scrape(url, expect_id)
        if info:
            row["artistHeader"] = info
            got += 1
            print(f"    ✓ {info['appleName']} bg=#{info['bgColor']}")
        else:
            missing.append(row["name"])
            print("    - 取得できず（ジャケットで代用）")

        # 旧実装の残骸を掃除する
        row.pop("artistImageTemplate", None)

    CANDIDATES.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n取得できた: {got} / {total}")
    if missing:
        print(f"取得できず ({len(missing)}): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
