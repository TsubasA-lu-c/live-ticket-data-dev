#!/usr/bin/env python3
"""確定した artistId から代表曲（トップソング）を取得する。

プレビュー再生は Apple Music ガイドライン 4.5.2(ii) の「音楽再生」の根拠として
必須（設計: `live_ticket_app/docs/design/2026-08-direction-change.md` §2.3）。

## どこから取るか

**Apple Music のアーティストページに埋まっている JSON から、
「トップソング」欄の上位3曲をその順序のまま採る。** Developer Token は要らない。

以前は iTunes Search API の `lookup?entity=song` を使っていたが、これは
**発売が新しい順**で返すため、代表曲としてマイナーな曲が並んでいた
（実例: ゴールデンボンバーで「女々しくて」が3番目になる）。

ページの JSON には順序・楽曲ID・プレビューURL・アルバム名・アートワークが
すべて入っているので、追加のAPI呼び出しも要らない。

セクションは位置ではなく、項目の `sectionName == "topSongs"` で見つける。
ページの構成が変わっても壊れにくくするため。

使い方:
    python3 tools/fetch_apple_music_previews.py
    python3 tools/fetch_apple_music_previews.py --force
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

SLEEP_SEC = 2.0
TIMEOUT_SEC = 25
TRACK_LIMIT = 3

# ブラウザのUAでないと Apple 側が簡易ページを返す
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"

SERVER_DATA = re.compile(r'id="serialized-server-data">(.*?)</script>', re.S)


def fetch_page(artist_id: str) -> dict | None:
    url = f"https://music.apple.com/jp/artist/{artist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            html = res.read().decode("utf-8")
    except Exception as exc:
        print(f"    ! 取得失敗: {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(SLEEP_SEC)

    match = SERVER_DATA.search(html)
    if not match:
        print("    ! ページ内のJSONが見つからない", file=sys.stderr)
        return None
    try:
        return json.loads(match.group(1))
    except Exception as exc:
        print(f"    ! JSONを読めない: {exc}", file=sys.stderr)
        return None


def top_songs_section(data: object) -> list[dict]:
    """項目の sectionName が topSongs のセクションを探す。"""
    node = data["data"][0] if isinstance(data, dict) else data[0]
    for section in node.get("data", {}).get("sections", []):
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            name = (
                item.get("playAction", {})
                .get("actionMetrics", {})
                .get("custom", {})
                .get("sectionName")
            )
            if name == "topSongs":
                return section.get("items", [])
    return []


def tracks_for(artist_id: str) -> list[dict]:
    data = fetch_page(artist_id)
    if data is None:
        return []
    try:
        items = top_songs_section(data)
    except Exception as exc:
        print(f"    ! 解析できない: {exc}", file=sys.stderr)
        return []

    tracks: list[dict] = []
    for item in items:
        preview = item.get("previewUrl")
        song_id = (
            item.get("contentDescriptor", {})
            .get("identifiers", {})
            .get("storeAdamID")
        )
        if not preview or not song_id:
            continue
        subtitle = item.get("subtitleLinks") or []
        album = subtitle[0].get("title") if subtitle else None
        if album:
            # 「アルバム名\u202f·\u202f2009年」から発売年を落とす。
            # 区切りは中黒(U+00B7)で、前後は**通常の空白ではなく U+202F**。
            # 年は末尾なので右から切る（アルバム名自体に中黒が入ることがある）
            album = album.rsplit("\u00b7", 1)[0].strip(" \u202f\u00a0") or None
        artwork = (item.get("artwork") or {}).get("dictionary", {}).get("url")
        tracks.append(
            {
                "songId": str(song_id),
                "title": item.get("title"),
                "albumTitle": album,
                "previewUrl": preview,
                "artworkUrl": artwork,
            }
        )
        if len(tracks) >= TRACK_LIMIT:
            break
    return tracks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    rows = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    total = len(rows)
    got = 0
    missing: list[str] = []

    for i, row in enumerate(rows, 1):
        aid = row.get("finalArtistId")
        if not aid:
            continue
        if row.get("previews") and not args.force:
            got += 1
            continue
        print(f"[{i}/{total}] {row['name']}", flush=True)
        tracks = tracks_for(str(aid))
        if tracks:
            row["previews"] = tracks
            got += 1
            print(f"    ✓ {len(tracks)}曲: {' / '.join(t['title'] for t in tracks)}", flush=True)
        else:
            missing.append(row["name"])
            print("    - トップソングなし", flush=True)

        CANDIDATES.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(f"\n取得できた: {got}")
    if missing:
        print(f"取得できず ({len(missing)}): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
