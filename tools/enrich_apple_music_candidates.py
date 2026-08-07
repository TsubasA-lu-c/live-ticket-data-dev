#!/usr/bin/env python3
"""照合候補に代表曲とアートワークURLを補い、確認用シートを出力する。

`match_apple_music.py` が作った candidates JSON を読み、まだ代表曲を引いていない
行（auto 判定のもの）を補完する。人間が102組を1画面で確認できるようにするのが目的。

アートワークはアーティスト本人の画像ではなく**アルバムのジャケット**を使う。
iTunes Search API はアーティスト画像を返さないため（設計:
`live_ticket_app/docs/design/2026-08-v2-design-decisions.md` §4.3 の修正）。

使い方:
    python3 tools/enrich_apple_music_candidates.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = REPO / "cache" / "apple_music_candidates.json"
OUT_HTML = REPO / "cache" / "apple_music_review.html"

SLEEP_SEC = 3.0
TIMEOUT_SEC = 20
ARTWORK_SIZE = "300x300bb.jpg"


def lookup_songs(artist_id: int, limit: int = 3) -> tuple[list[str], str | None]:
    """代表曲名とアルバムアートワークURLを返す。"""
    params = {
        "id": str(artist_id),
        "entity": "song",
        "limit": str(limit + 1),  # 1件目はアーティスト自身のレコード
        "country": "jp",
    }
    url = f"https://itunes.apple.com/lookup?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "chikenote-matcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        print(f"    ! 取得失敗: {exc}", file=sys.stderr)
        return [], None
    finally:
        time.sleep(SLEEP_SEC)

    tracks: list[str] = []
    art: str | None = None
    for r in payload.get("results", []):
        if r.get("wrapperType") != "track":
            continue
        if r.get("trackName"):
            tracks.append(r["trackName"])
        if art is None and r.get("artworkUrl100"):
            # 100x100 で返るので高解像度に差し替える（URL規約上のサイズ指定）
            art = r["artworkUrl100"].replace("100x100bb.jpg", ARTWORK_SIZE)
    return tracks[:limit], art


def proposed(row: dict) -> dict | None:
    """このアーティストに対して採用を提案するIDを返す。

    auto / likely は matched、review は検索1位（関連度順の先頭）。
    """
    if row.get("matched"):
        return row["matched"]
    if row.get("candidates"):
        c = row["candidates"][0]
        return {"artistId": c["artistId"], "artistName": c["artistName"]}
    return None


def main() -> int:
    rows = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    total = len(rows)

    for i, row in enumerate(rows, 1):
        pick = proposed(row)
        if not pick:
            continue
        if row.get("topTracks") and row.get("artworkUrl"):
            continue
        print(f"[{i}/{total}] {row['name']}")
        tracks, art = lookup_songs(pick["artistId"])
        if tracks:
            row["topTracks"] = tracks
        if art:
            row["artworkUrl"] = art

    CANDIDATES.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ── 確認用HTML ───────────────────────────────────────────────
    def esc(s: str) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    order = {"review": 0, "likely": 1, "auto": 2, "none": 3}
    rows_sorted = sorted(rows, key=lambda r: (order.get(r["status"], 9), r["name"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    cards: list[str] = []
    for r in rows_sorted:
        pick = proposed(r)
        art = r.get("artworkUrl")
        tracks = " / ".join(r.get("topTracks") or []) or "（取得できず）"
        alt = ""
        if r["status"] == "review" and len(r.get("candidates") or []) > 1:
            others = " ".join(
                f"<code>{esc(c['artistId'])}</code> {esc(c['artistName'])}"
                for c in r["candidates"][1:4]
            )
            alt = f'<div class="alt">他の候補: {others}</div>'

        thumb = (
            f'<img class="aw" src="{esc(art)}" alt="" loading="lazy">'
            if art
            else '<div class="aw ph"></div>'
        )
        cards.append(
            f"""<label class="card {r['status']}">
  <input type="checkbox" checked>
  {thumb}
  <div class="body">
    <div class="nm">{esc(r['name'])}</div>
    <div class="am">{esc(pick['artistName']) if pick else '—'} ・ <code>{esc(pick['artistId']) if pick else '—'}</code></div>
    <div class="tr">{esc(tracks)}</div>
    {alt}
  </div>
  <span class="tag">{r['status']}</span>
</label>"""
        )

    html = f"""<!doctype html>
<meta charset="utf-8">
<title>Apple Music アーティストID 照合確認</title>
<style>
 body {{ margin:0; background:#101015; color:#EFEBE2; font-family:-apple-system,
   BlinkMacSystemFont,"Hiragino Sans",sans-serif; line-height:1.5; }}
 .wrap {{ max-width:920px; margin:0 auto; padding:40px 20px 80px; }}
 h1 {{ font-size:26px; margin:0 0 8px; letter-spacing:-.02em; }}
 .sum {{ color:#9A948A; font-size:14px; margin:0 0 24px; }}
 .sum b {{ color:#EFEBE2; }}
 .card {{ display:flex; gap:14px; align-items:center; padding:12px 14px;
   border:1px solid rgba(255,255,255,.1); border-radius:12px; margin-bottom:8px;
   background:#17171E; cursor:pointer; }}
 .card:hover {{ background:#1D1D26; }}
 .card input {{ width:18px; height:18px; flex:none; accent-color:#5FD888; }}
 .aw {{ width:52px; height:52px; border-radius:8px; flex:none; object-fit:cover; }}
 .aw.ph {{ background:#2A2A34; }}
 .body {{ flex:1; min-width:0; }}
 .nm {{ font-weight:700; font-size:15px; }}
 .am {{ font-size:12px; color:#9A948A; margin-top:1px; }}
 .tr {{ font-size:12px; color:#C4BEB2; margin-top:3px; overflow:hidden;
   text-overflow:ellipsis; white-space:nowrap; }}
 .alt {{ font-size:11px; color:#7E7A72; margin-top:4px; }}
 code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; }}
 .tag {{ flex:none; font-size:10px; font-weight:800; letter-spacing:.08em;
   padding:3px 8px; border-radius:5px; }}
 .review .tag {{ background:rgba(255,111,94,.18); color:#FF6F5E; }}
 .likely .tag {{ background:rgba(245,194,74,.18); color:#F5C24A; }}
 .auto .tag {{ background:rgba(95,216,136,.16); color:#5FD888; }}
</style>
<div class="wrap">
<h1>Apple Music アーティストID 照合確認</h1>
<p class="sum">全 <b>{total}</b> 組 ・ review <b>{counts.get('review',0)}</b> ／
 likely <b>{counts.get('likely',0)}</b> ／ auto <b>{counts.get('auto',0)}</b><br>
 要確認のものから順に並べています。ジャケットと代表曲を見て、違うものだけチェックを外してください。</p>
{"".join(cards)}
</div>
"""
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"\n確認用シート: {OUT_HTML.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
