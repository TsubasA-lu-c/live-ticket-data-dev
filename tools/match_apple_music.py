#!/usr/bin/env python3
"""artists.json の各アーティストを Apple Music のアーティストIDに照合する。

iTunes Search API（認証不要・無料）を使う。ここで得られる artistId は
Apple Music カタログのアーティストIDと同一で、`music.apple.com/jp/artist/…/{id}`
の末尾の数値と一致する。

名前で毎回検索する運用は同名アーティストで誤爆するため、この照合は一度だけ行い、
確定した artistId を artists.json に保存して以降はIDで引く（設計:
`live_ticket_app/docs/design/2026-08-v2-design-decisions.md` §4.2）。

段階1（本スクリプト）: 候補を集めて確認用リストを作る。JSONは書き換えない。
段階2（別途）: 確定したIDでアートワーク・プレビューを取得する。

使い方:
    python3 tools/match_apple_music.py                  # 全件
    python3 tools/match_apple_music.py --limit 10       # 先頭10件で試す
    python3 tools/match_apple_music.py --only ado,kingu # 特定IDのみ
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTISTS_JSON = REPO / "data" / "artists.json"
OUT_JSON = REPO / "cache" / "apple_music_candidates.json"
OUT_MD = REPO / "cache" / "apple_music_review.md"

SEARCH_URL = "https://itunes.apple.com/search"
# iTunes Search API は公称のレート制限が非公開だが、概ね毎分20リクエスト程度で
# 429 を返す。1件あたり最大2回検索するため、余裕をみて待機する。
SLEEP_SEC = 3.0
TIMEOUT_SEC = 20


def normalize(name: str) -> str:
    """比較用の正規化。全角/半角・大小・記号・空白の揺れを吸収する。

    「ONE OK ROCK」と「ONE　OK　ROCK」、「Ellegarden」と「ELLEGARDEN」を
    同一視するのが目的。
    """
    s = unicodedata.normalize("NFKC", name)
    s = s.casefold()
    # 中黒・ハイフン類・空白・記号を除去（"Mrs. GREEN APPLE" 等の揺れ対策）
    s = re.sub(r"[\s・･\-‐‑‒–—―ー_.,'’\"“”!！?？&＆()（）\[\]【】/／]", "", s)
    return s


def fetch(term: str, entity: str = "musicArtist", limit: int = 8) -> list[dict]:
    params = {
        "term": term,
        "country": "jp",
        "entity": entity,
        "limit": str(limit),
        "lang": "ja_jp",
    }
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "chikenote-matcher/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
        payload = json.loads(res.read().decode("utf-8"))
    return payload.get("results", [])


def search_artist(artist: dict) -> tuple[list[dict], list[str]]:
    """名前→エイリアスの順に検索し、候補と実際に使った検索語を返す。"""
    terms: list[str] = [artist["name"]]
    terms += [a for a in (artist.get("aliases") or []) if a]

    used: list[str] = []
    results: list[dict] = []
    seen_ids: set[int] = set()

    for term in terms:
        used.append(term)
        try:
            hits = fetch(term)
        except Exception as exc:  # ネットワーク断・429 等
            print(f"    ! 検索失敗 ({term}): {exc}", file=sys.stderr)
            time.sleep(SLEEP_SEC)
            continue
        for hit in hits:
            aid = hit.get("artistId")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                results.append(hit)
        time.sleep(SLEEP_SEC)
        # 1語目で完全一致が取れたらエイリアスは引かない（リクエスト節約）
        if any(normalize(h.get("artistName", "")) == normalize(term) for h in hits):
            break

    return results, used


def top_tracks(artist_id: int, limit: int = 3) -> list[str]:
    """artistId の代表曲名を返す。人間が同名別人を見分けるための判断材料。

    Apple Music の日本ストアフロントはアーティスト名をローマ字で返すことが多く
    （米津玄師 → Kenshi Yonezu）、名前だけでは本人か確認できない。曲名は
    日本語のまま返るため、こちらの方が判別に効く。
    """
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
    except Exception:
        return []
    finally:
        time.sleep(SLEEP_SEC)

    names: list[str] = []
    for r in payload.get("results", []):
        if r.get("wrapperType") == "track" and r.get("trackName"):
            names.append(r["trackName"])
    return names[:limit]


def judge(artist: dict, candidates: list[dict]) -> tuple[str, dict | None]:
    """自動確定できるかを判定する。

    戻り値の状態:
        auto    … 名前が完全一致する候補がちょうど1件。人手確認は省略可
        likely  … 完全一致は無いが検索1位。日本語名がローマ字で返る場合はここに落ちる
        review  … 完全一致が複数、または候補が割れている。人間が選ぶ必要あり
        none    … 候補ゼロ。Apple Music に無いか、名前が違う
    """
    if not candidates:
        return "none", None

    primary = normalize(artist["name"])
    alias_keys = {normalize(a) for a in (artist.get("aliases") or []) if a}

    # 正式名称との完全一致を最優先する。
    by_primary = [c for c in candidates if normalize(c.get("artistName", "")) == primary]
    if len(by_primary) == 1:
        return "auto", by_primary[0]
    if len(by_primary) > 1:
        return "review", None

    # 別名だけが一致したケースは自動確定してはいけない。
    # 「浦島坂田船」の別名「USSS」が無関係の "Usss" と一致した実例がある。
    # 別名一致が検索1位でもない場合は、関連度が低い＝誤爆の可能性が高い。
    by_alias = [c for c in candidates if normalize(c.get("artistName", "")) in alias_keys]
    if by_alias:
        if len(by_alias) == 1 and by_alias[0] is candidates[0]:
            return "auto", by_alias[0]
        return "review", None

    # 完全一致なし。iTunes Search API は関連度順に返すため、1位は本人であることが多い。
    # ただし断定はできないので likely 止まりにし、代表曲を添えて人間に確認させる。
    return "likely", candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="先頭N件のみ処理")
    parser.add_argument("--only", type=str, default="", help="カンマ区切りのアーティストID")
    args = parser.parse_args()

    data = json.loads(ARTISTS_JSON.read_text(encoding="utf-8"))
    artists = data["artists"] if isinstance(data, dict) else data

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        artists = [a for a in artists if a["id"] in wanted]
    if args.limit:
        artists = artists[: args.limit]

    total = len(artists)
    print(f"照合対象: {total} 組\n")

    out: list[dict] = []
    counts = {"auto": 0, "likely": 0, "review": 0, "none": 0}

    for i, artist in enumerate(artists, 1):
        print(f"[{i}/{total}] {artist['name']}")
        candidates, used_terms = search_artist(artist)
        status, chosen = judge(artist, candidates)
        counts[status] += 1

        # 確認が要るものだけ代表曲を引く（auto は名前が完全一致しているため不要）
        tracks: list[str] = []
        if status in ("likely", "review") and candidates:
            tracks = top_tracks(candidates[0]["artistId"])

        slim = [
            {
                "artistId": c.get("artistId"),
                "artistName": c.get("artistName"),
                "genre": c.get("primaryGenreName"),
                "url": c.get("artistLinkUrl"),
            }
            for c in candidates
        ]

        out.append(
            {
                "id": artist["id"],
                "name": artist["name"],
                "aliases": artist.get("aliases") or [],
                "genre": artist.get("genre"),
                "status": status,
                "searchTerms": used_terms,
                "topTracks": tracks,
                "matched": (
                    {"artistId": chosen.get("artistId"), "artistName": chosen.get("artistName")}
                    if chosen
                    else None
                ),
                "candidates": slim,
            }
        )

        mark = {"auto": "✓", "likely": "~", "review": "?", "none": "×"}[status]
        detail = f"{chosen['artistName']} ({chosen['artistId']})" if chosen else f"候補 {len(slim)} 件"
        if tracks:
            detail += f" — {' / '.join(tracks)}"
        print(f"    {mark} {status}: {detail}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 人間が確認するための Markdown。review と none だけを上に集める。
    lines: list[str] = [
        "# Apple Music アーティストID 照合結果",
        "",
        f"- 対象: {total} 組",
        f"- 自動確定 `auto`: {counts['auto']} 組",
        f"- **1位候補で要承認 `likely`: {counts['likely']} 組**",
        f"- **要選択 `review`: {counts['review']} 組**",
        f"- **候補なし `none`: {counts['none']} 組**",
        "",
        "`auto` は名前が完全一致する候補がちょうど1件だったもの。そのまま採用してよい。",
        "",
        "`likely` は検索1位だが名前が完全一致しなかったもの。日本ストアフロントは",
        "アーティスト名をローマ字で返すため（米津玄師 → Kenshi Yonezu）、日本語名の",
        "アーティストはほぼここに落ちる。**代表曲を見て本人か判断する**。",
        "",
        "`review` は候補が割れているもの。IDを選ぶ必要がある。",
        "",
        "---",
        "",
        "## 1位候補で要承認（likely）",
        "",
        "代表曲に見覚えがあればそのまま採用。違えば下の候補表から選ぶ。",
        "",
        "| アーティスト | artistId | Apple Music 上の名前 | 代表曲 |",
        "|---|---|---|---|",
    ]

    for row in out:
        if row["status"] != "likely":
            continue
        m = row["matched"]
        tr = " / ".join(row["topTracks"]) if row["topTracks"] else "（取得できず）"
        lines.append(f"| {row['name']} | `{m['artistId']}` | {m['artistName']} | {tr} |")

    lines += ["", "---", "", "## 要選択（review）・候補なし（none）", ""]

    for row in out:
        if row["status"] not in ("review", "none"):
            continue
        lines.append(f"### {row['name']}  `{row['id']}`")
        if row["aliases"]:
            lines.append(f"別名: {' / '.join(row['aliases'])}")
        lines.append(f"ジャンル: {row['genre']}　検索語: {' , '.join(row['searchTerms'])}")
        if row["topTracks"]:
            lines.append(f"1位候補の代表曲: {' / '.join(row['topTracks'])}")
        lines.append("")
        if not row["candidates"]:
            lines.append("**候補なし**")
        else:
            lines.append("| # | artistId | 名前 | ジャンル | URL |")
            lines.append("|---|---|---|---|---|")
            for n, c in enumerate(row["candidates"], 1):
                lines.append(
                    f"| {n} | `{c['artistId']}` | {c['artistName']} | {c['genre']} | {c['url']} |"
                )
        lines.append("")

    lines += ["---", "", "## 自動確定", "", "| アーティスト | artistId | Apple Music 上の名前 |", "|---|---|---|"]
    for row in out:
        if row["status"] != "auto":
            continue
        m = row["matched"]
        lines.append(f"| {row['name']} | `{m['artistId']}` | {m['artistName']} |")
    lines.append("")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n完了: auto {counts['auto']} / review {counts['review']} / none {counts['none']}")
    print(f"  候補データ: {OUT_JSON.relative_to(REPO)}")
    print(f"  確認用リスト: {OUT_MD.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
