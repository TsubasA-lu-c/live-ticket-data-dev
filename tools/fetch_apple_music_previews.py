#!/usr/bin/env python3
"""確定した artistId から代表曲の30秒プレビューURLを取得する。

プレビュー再生は Apple Music ガイドライン 4.5.2(ii) の「音楽再生」の根拠として
必須（設計: `live_ticket_app/docs/design/2026-08-direction-change.md` §2.3）。
`previewUrl` は iTunes Search API から認証なしで取得でき、再生自体も認証不要。

楽曲IDを併せて保存する。将来 Apple Music 加入者向けのプレイリスト保存機能を
追加する際、これがあれば画面1つ分の追加で済む。

使い方:
    python3 tools/fetch_apple_music_previews.py
    python3 tools/fetch_apple_music_previews.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = REPO / "cache" / "apple_music_candidates.json"

SLEEP_SEC = 3.0
TIMEOUT_SEC = 20
TRACK_LIMIT = 3
ARTWORK_SIZE = "{w}x{h}bb.jpg"


def lookup(artist_id: int) -> list[dict]:
    params = {
        "id": str(artist_id),
        "entity": "song",
        "limit": str(TRACK_LIMIT + 1),  # 1件目はアーティスト自身のレコード
        "country": "jp",
    }
    url = f"https://itunes.apple.com/lookup?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "chikenote-matcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        print(f"    ! 取得失敗: {exc}", file=sys.stderr)
        return []
    finally:
        time.sleep(SLEEP_SEC)

    tracks: list[dict] = []
    for r in payload.get("results", []):
        if r.get("wrapperType") != "track" or not r.get("previewUrl"):
            continue
        art = r.get("artworkUrl100") or ""
        tracks.append(
            {
                "songId": str(r.get("trackId")),
                "title": r.get("trackName"),
                "albumTitle": r.get("collectionName"),
                "previewUrl": r["previewUrl"],
                # サイズを差し替えられるテンプレート形式に揃える
                "artworkUrl": art.replace("100x100bb.jpg", ARTWORK_SIZE) or None,
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
        print(f"[{i}/{total}] {row['name']}")
        tracks = lookup(aid)
        if tracks:
            row["previews"] = tracks
            got += 1
            print(f"    ✓ {len(tracks)}曲: {' / '.join(t['title'] for t in tracks)}")
        else:
            missing.append(row["name"])
            print("    - プレビューなし")

    CANDIDATES.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n取得できた: {got}")
    if missing:
        print(f"取得できず ({len(missing)}): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
