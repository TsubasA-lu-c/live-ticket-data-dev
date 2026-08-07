#!/usr/bin/env python3
"""照合結果の確認シート（単一HTML）を作る。

画像は base64 で埋め込み、外部リクエストを一切しないHTMLにする。
Artifact として公開する際、外部ホストへの画像リクエストは CSP で遮断されるため。

表示に使うのは**アーティスト本人の画像**（`fetch_artist_images.py` が取得したもの）。
ジャケットでは本人か判別できないため、取得できなかった場合のみジャケットで代用する。

Apple Music のページが返す名前は日本語のままなので、配信中の名前と突き合わせて
一致しないものを先頭に並べる。これが誤照合の最も強い検出手段になる
（実例: 「テニスの王子様 Musical」が「ミュージカル『刀剣乱舞』」に紐づいていた）。

使い方:
    python3 tools/build_review_sheet.py
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANDIDATES = REPO / "cache" / "apple_music_candidates.json"
OUT = REPO / "cache" / "apple_music_review_sheet.html"

THUMB_W = 160
SLEEP_SEC = 0.15
TIMEOUT_SEC = 20


def esc(s) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name)).casefold()
    return re.sub(r"[\s・･\-‐‑‒–—―~〜_.,'’\"“”!！?？&＆()（）\[\]【】/／]", "", s)


def thumb_url(row: dict) -> str | None:
    """本人画像のサムネURL。無ければジャケットで代用する。"""
    head = row.get("artistHeader")
    if head and head.get("imageTemplate"):
        return (
            head["imageTemplate"]
            .replace("{w}", str(THUMB_W))
            .replace("{h}", str(THUMB_W))
            .replace("{c}", "bb")
            .replace("{f}", "jpg")
        )
    art = row.get("artworkUrl")
    if art:
        return art.replace("300x300bb.jpg", f"{THUMB_W}x{THUMB_W}bb.jpg")
    return None


def fetch_data_uri(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "chikenote-matcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            raw = res.read()
    except Exception as exc:
        print(f"    ! 画像取得失敗: {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(SLEEP_SEC)
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def picked(row: dict) -> dict | None:
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
        url = thumb_url(row)
        if not url:
            continue
        if row.get("thumb") and row.get("thumbSrc") == url:
            continue
        print(f"[{i}/{total}] {row['name']}")
        data = fetch_data_uri(url)
        if data:
            row["thumb"] = data
            row["thumbSrc"] = url

    CANDIDATES.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ── 並び順: 要確認度の高い順 ──────────────────────────────
    def flags(row: dict) -> tuple[bool, bool]:
        head = row.get("artistHeader")
        no_head = head is None
        mismatch = bool(
            head
            and head.get("appleName")
            and normalize(head["appleName"]) != normalize(row["name"])
        )
        return mismatch, no_head

    status_order = {"review": 0, "likely": 1, "auto": 2, "none": 3}

    def sort_key(row: dict):
        mismatch, no_head = flags(row)
        return (0 if mismatch else 1, 0 if no_head else 1,
                status_order.get(row["status"], 9), row["name"])

    rows_sorted = sorted(rows, key=sort_key)

    n_mismatch = sum(1 for r in rows if flags(r)[0])
    n_nohead = sum(1 for r in rows if flags(r)[1])
    n_ok = total - n_mismatch - n_nohead

    cards: list[str] = []
    for row in rows_sorted:
        mismatch, no_head = flags(row)
        pick = picked(row)
        head = row.get("artistHeader") or {}
        tracks = " / ".join(row.get("topTracks") or []) or "（代表曲を取得できず）"

        if mismatch:
            cls, tag = "bad", "名前が違う"
        elif no_head:
            cls, tag = "warn", "本人画像なし"
        else:
            cls, tag = "ok", "一致"

        thumb = (
            f'<img class="aw" src="{row["thumb"]}" alt="">'
            if row.get("thumb")
            else '<div class="aw ph"></div>'
        )

        bg = head.get("bgColor")
        swatch = (
            f'<span class="bg" style="background:#{esc(bg)}" title="bgColor #{esc(bg)}"></span>'
            if bg
            else ""
        )

        apple_name = head.get("appleName") or (pick["artistName"] if pick else "—")
        name_line = (
            f'<span class="x">{esc(apple_name)}</span>'
            if mismatch
            else esc(apple_name)
        )

        alt = ""
        if row["status"] == "review" and len(row.get("candidates") or []) > 1:
            items = "".join(
                f'<span class="oc"><code>{esc(c["artistId"])}</code> {esc(c["artistName"])}'
                f' <em>{esc(c["genre"])}</em></span>'
                for c in row["candidates"][1:4]
            )
            alt = f'<div class="alt">他の候補: {items}</div>'

        cards.append(
            f"""<div class="card {cls}">
  {thumb}
  <div class="body">
    <div class="nm">{esc(row['name'])}{swatch}</div>
    <div class="am">{name_line} <code>{esc(pick['artistId']) if pick else '—'}</code></div>
    <div class="tr">{esc(tracks)}</div>{alt}
  </div>
  <span class="tag">{tag}</span>
</div>"""
        )

    html = f"""<title>Apple Music アーティスト照合確認</title>
<style>
 :root {{
   --bg:#101015; --fg:#EFEBE2; --sub:#9A948A; --card:#17171E;
   --rule:rgba(255,255,255,.10);
   --ok:#5FD888; --warn:#F5C24A; --hot:#FF6F5E;
 }}
 :root[data-theme="light"] {{
   --bg:#EEEBE4; --fg:#1C1A17; --sub:#6A6459; --card:#F8F6F1;
   --rule:rgba(0,0,0,.12); --ok:#2F8A55; --warn:#8F6A12; --hot:#C1402F;
 }}
 @media (prefers-color-scheme: light) {{
   :root:not([data-theme="dark"]) {{
     --bg:#EEEBE4; --fg:#1C1A17; --sub:#6A6459; --card:#F8F6F1;
     --rule:rgba(0,0,0,.12); --ok:#2F8A55; --warn:#8F6A12; --hot:#C1402F;
   }}
 }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; background:var(--bg); color:var(--fg); line-height:1.5;
   font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",sans-serif;
   -webkit-font-smoothing:antialiased; }}
 .wrap {{ max-width:900px; margin:0 auto; padding:44px 20px 90px; }}
 h1 {{ font-size:27px; margin:0 0 10px; letter-spacing:-.02em; }}
 .lede {{ color:var(--sub); font-size:14px; margin:0 0 8px; max-width:64ch; }}
 .lede b {{ color:var(--fg); }}
 .counts {{ display:flex; flex-wrap:wrap; gap:8px; margin:20px 0 26px; }}
 .cnt {{ border:1px solid var(--rule); border-radius:999px; padding:5px 13px;
   font-size:12.5px; background:var(--card); }}
 .cnt b {{ font-variant-numeric:tabular-nums; }}
 .card {{ display:flex; gap:14px; align-items:center; padding:11px 14px;
   border:1px solid var(--rule); border-radius:12px; margin-bottom:7px;
   background:var(--card); }}
 .card.bad {{ border-color:var(--hot); }}
 .card.warn {{ border-color:color-mix(in srgb, var(--warn) 55%, transparent); }}
 .aw {{ width:56px; height:56px; border-radius:8px; flex:none; object-fit:cover;
   background:var(--rule); }}
 .aw.ph {{ background:var(--rule); }}
 .body {{ flex:1; min-width:0; }}
 .nm {{ font-weight:700; font-size:15px; display:flex; align-items:center; gap:7px; }}
 .bg {{ width:11px; height:11px; border-radius:3px; flex:none;
   border:1px solid var(--rule); }}
 .am {{ font-size:12px; color:var(--sub); margin-top:1px; }}
 .am .x {{ color:var(--hot); font-weight:700; }}
 .tr {{ font-size:12px; margin-top:3px; overflow:hidden; text-overflow:ellipsis;
   white-space:nowrap; }}
 .alt {{ font-size:11px; color:var(--sub); margin-top:5px; display:flex;
   flex-wrap:wrap; gap:4px 10px; }}
 .oc em {{ font-style:normal; opacity:.7; }}
 code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px;
   font-variant-numeric:tabular-nums; }}
 .tag {{ flex:none; font-size:10px; font-weight:800; letter-spacing:.06em;
   padding:3px 8px; border-radius:5px; white-space:nowrap; }}
 .bad .tag {{ background:color-mix(in srgb, var(--hot) 22%, transparent); color:var(--hot); }}
 .warn .tag {{ background:color-mix(in srgb, var(--warn) 22%, transparent); color:var(--warn); }}
 .ok .tag {{ background:color-mix(in srgb, var(--ok) 18%, transparent); color:var(--ok); }}
 @media (max-width:560px) {{ .tr {{ white-space:normal; }} }}
</style>
<div class="wrap">
<h1>Apple Music アーティスト照合確認</h1>
<p class="lede">配信中の {total} 組を Apple Music のアーティストに突き合わせた結果。
表示しているのは<b>アーティスト本人の画像</b>で、名前の右の小さな四角は
Apple が抽出した背景色（<code>bgColor</code>）。</p>
<p class="lede">Apple Music 側は日本語名を返すため、<b>配信中の名前と一致するか</b>で
検証している。一致しなかったものを先頭に並べているので、
<b>上から数件だけ</b>見れば済む。</p>
<div class="counts">
 <span class="cnt">全 <b>{total}</b> 組</span>
 <span class="cnt">名前が違う <b>{n_mismatch}</b></span>
 <span class="cnt">本人画像なし <b>{n_nohead}</b></span>
 <span class="cnt">一致 <b>{n_ok}</b></span>
</div>
{"".join(cards)}
</div>
"""
    OUT.write_text(html, encoding="utf-8")
    size = OUT.stat().st_size / 1024 / 1024
    print(f"\n確認シート: {OUT.relative_to(REPO)} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
