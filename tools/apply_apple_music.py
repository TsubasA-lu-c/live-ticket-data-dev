#!/usr/bin/env python3
"""照合結果を配信JSONへ書き込む。

`cache/apple_music_candidates.json` の確定値を、以下の2か所へ反映する。

    data/artists.json          … appleMusic ブロック（artistId・画像URL・配色）
    data/artist/{id}.json      … appleMusicTracks（代表曲とプレビューURL）

形式は `COLLECTION_RULES.md` §3.6 および
`live_ticket_app/docs/design/data-model.md` を正とする。

該当が無いアーティストにはキーごと付けない（null を並べない）。
アプリ側は キーが無い = 頭文字アバターにフォールバック、と解釈する。

使い方:
    python3 tools/apply_apple_music.py --dry-run   # 差分だけ表示
    python3 tools/apply_apple_music.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = REPO / "cache" / "apple_music_candidates.json"
ARTISTS_JSON = REPO / "data" / "artists.json"
ARTIST_DIR = REPO / "data" / "artist"

COLOR_KEYS = ("bgColor", "textColor1", "textColor2", "textColor3", "textColor4")


def build_block(row: dict) -> dict | None:
    """artists.json に載せる appleMusic ブロックを作る。"""
    aid = row.get("finalArtistId")
    if not aid:
        return None
    head = row.get("artistHeader") or {}
    block: dict = {"artistId": str(aid)}
    if head.get("imageTemplate"):
        block["imageUrl"] = head["imageTemplate"]
    for key in COLOR_KEYS:
        if head.get(key):
            block[key] = head[key]
    return block


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rows}

    artists = json.loads(ARTISTS_JSON.read_text(encoding="utf-8"))

    n_block = n_img = n_color = n_tracks = 0
    skipped: list[str] = []
    missing_file: list[str] = []

    for entry in artists:
        row = by_id.get(entry["id"])
        if not row:
            continue
        block = build_block(row)
        if not block:
            skipped.append(entry["name"])
            entry.pop("appleMusic", None)
            continue
        entry["appleMusic"] = block
        n_block += 1
        if "imageUrl" in block:
            n_img += 1
        if "bgColor" in block:
            n_color += 1

    # 代表曲は詳細ファイル側へ
    track_updates: list[tuple[Path, dict]] = []
    for row in rows:
        previews = row.get("previews") or []
        path = ARTIST_DIR / f"{row['id']}.json"
        if not path.exists():
            if previews:
                missing_file.append(row["id"])
            continue
        detail = json.loads(path.read_text(encoding="utf-8"))
        if previews:
            detail["appleMusicTracks"] = previews
            n_tracks += 1
        else:
            detail.pop("appleMusicTracks", None)
        track_updates.append((path, detail))

    print(f"appleMusic ブロック: {n_block} 組")
    print(f"  うち本人画像あり: {n_img}")
    print(f"  うち配色あり:     {n_color}")
    print(f"appleMusicTracks:   {n_tracks} 組")
    if skipped:
        print(f"該当なし（キーを付けない）: {', '.join(skipped)}")
    if missing_file:
        print(f"詳細ファイルが無い: {', '.join(missing_file)}", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run のため書き込んでいない")
        return 0

    ARTISTS_JSON.write_text(
        json.dumps(artists, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for path, detail in track_updates:
        path.write_text(
            json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(f"\n書き込み完了: artists.json と artist/*.json {len(track_updates)} ファイル")
    print("次に実行すること: python3 tools/validate.py && python3 tools/update_manifest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
