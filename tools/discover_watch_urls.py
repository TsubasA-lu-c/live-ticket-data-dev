#!/usr/bin/env python3
"""
discover_watch_urls.py — 各アーティストの公式TOPページから NEWS/LIVE系ページへの
実在リンクを抽出し、cache/watch_urls.json に追加監視URLとして登録する。

check_updates.py はデフォルトで artists.json の sourceUrl（公式TOPページ）1本しか
監視しないため、TOPページ自体に変化がない別ページ（news一覧・スケジュールページ等）
の更新を見落とすことがある。本スクリプトは、その見落としを減らすために
TOPページ内の実在リンク（<a href>）から関連ページを発見して登録する。

RSS/Atomフィード（<link rel="alternate" type="application/rss+xml|atom+xml">）も
抽出して**最優先**で登録する。JSレンダリングのサイトは静的HTMLがほぼ変化せず
更新検知から漏れるため、サーバー生成のフィードを監視できると検知精度が上がる。

**URLのパス推測は絶対禁止。** 必ず取得したHTML内の <a href> / <link href> から
抽出したURLのみを使う。

使い方:
  python3 tools/discover_watch_urls.py                 # 全アーティストを対象
  python3 tools/discover_watch_urls.py yuzu milk        # 指定アーティストのみ
  python3 tools/discover_watch_urls.py --dry-run        # ファイル更新なし（確認用）
"""
import argparse, json, sys, time
import urllib.error, urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

ARTISTS_FILE = Path("data/artists.json")
WATCH_URLS_FILE = Path("cache/watch_urls.json")

USER_AGENT = "Mozilla/5.0 (compatible; live-ticket-data/1.0)"
FETCH_TIMEOUT_SEC = 20
MAX_BYTES = 512 * 1024
SLEEP_SEC = 0.3
MAX_URLS_PER_ARTIST = 3

# NEWS/LIVE系ページ判定キーワード（優先度: TIER0 > TIER1 > TIER2）
KEYWORDS_TIER0 = ["news", "ニュース"]
KEYWORDS_TIER1 = ["live", "tour", "schedule", "ライブ", "スケジュール"]
KEYWORDS_TIER2 = ["event", "ticket", "information", "チケット", "公演"]

# 同一ホストでも監視対象から除外するSNS等のホストキーワード
SNS_HOST_KEYWORDS = [
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "youtube.com", "youtu.be", "tiktok.com", "line.me", "threads.net",
]


class _LinkExtractor(HTMLParser):
    """<a href> とそのリンクテキストのペア、および
    <link rel="alternate"> の RSS/Atom フィードURLを抽出する"""

    def __init__(self):
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        self.feeds: List[str] = []
        self._current_href: Optional[str] = None
        self._current_text_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._current_href = href
                self._current_text_parts = []
        elif tag == "link":
            d = dict(attrs)
            rel = (d.get("rel") or "").lower()
            link_type = (d.get("type") or "").lower()
            href = d.get("href")
            if (
                href
                and "alternate" in rel.split()
                and ("rss+xml" in link_type or "atom+xml" in link_type)
            ):
                self.feeds.append(href)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            self.links.append((self._current_href, "".join(self._current_text_parts)))
            self._current_href = None
            self._current_text_parts = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text_parts.append(data)


def _fetch_html(url: str) -> Optional[str]:
    """URLのHTMLをテキストとして取得する。取得失敗時はNone。"""
    headers = {"User-Agent": USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            content = resp.read(MAX_BYTES)
            return content.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] fetch失敗 {url}: {e}", file=sys.stderr)
        return None


def _feed_alive(url: str) -> bool:
    """フィードURLが実際に取得可能（HTTP 200）か確認する。
    <link rel="alternate"> で宣言されていても実体が404のサイトがあり、
    死んだURLを登録すると check_updates.py が取得失敗を「変化あり」と
    扱うため恒久的な誤検知になる。登録前に必ず生存確認する。"""
    headers = {"User-Agent": USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            resp.read(1024)  # 先頭だけ読めれば十分
            return resp.status == 200
    except Exception:
        return False


def _is_excluded_scheme(href: str) -> bool:
    low = href.strip().lower()
    return low.startswith("mailto:") or low.startswith("tel:") or low.startswith("javascript:") or low == "#" or low.startswith("#")


def _normalize_same_host(href: str, source_url: str, source_host: str) -> Optional[str]:
    """href を絶対URL化し、同一ホスト（サブドメイン含む）の http(s) URLなら
    フラグメント除去済みURLを返す。対象外なら None。"""
    href = href.strip()
    if not href or _is_excluded_scheme(href):
        return None

    abs_url = urljoin(source_url, href)
    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None

    host = parsed.netloc.lower()
    if host != source_host and not host.endswith("." + source_host):
        return None
    if any(sns in host for sns in SNS_HOST_KEYWORDS):
        return None

    return parsed._replace(fragment="").geturl()


def extract_candidates(html: str, source_url: str, feed_validator=None) -> List[str]:
    """HTML内の<a href>・<link rel="alternate">からNEWS/LIVE系ページ・RSS/Atomフィードの
    候補URLを抽出する（実在リンクのみ、推測なし）。
    同一ホストまたはそのサブドメインに限定し、優先度順
    （RSS/Atomフィード > news > live/tour/schedule > その他）で
    最大 MAX_URLS_PER_ARTIST 件に絞って返す。
    feed_validator: フィードURLの生存確認コールバック（url -> bool）。
    None の場合は確認しない（ユニットテスト用）。"""
    parser = _LinkExtractor()
    parser.feed(html)

    source_host = urlparse(source_url).netloc.lower()
    source_norm = source_url.rstrip("/")

    candidates: List[Tuple[int, str]] = []
    seen = set()

    # RSS/Atomフィードは最優先（tier -1）。JSレンダリングサイトでも
    # サーバー生成のフィードなら更新を検知できるため
    for href in parser.feeds:
        norm_url = _normalize_same_host(href, source_url, source_host)
        if norm_url is None or norm_url.rstrip("/") == source_norm or norm_url in seen:
            continue
        # 宣言されていても実体が404のフィードがあるため、登録前に生存確認する
        if feed_validator is not None and not feed_validator(norm_url):
            print(f"    [WARN] フィード宣言はあるが取得不可のため除外: {norm_url}", file=sys.stderr)
            seen.add(norm_url)
            continue
        seen.add(norm_url)
        candidates.append((-1, norm_url))

    for href, text in parser.links:
        norm_url = _normalize_same_host(href, source_url, source_host)
        if norm_url is None or norm_url.rstrip("/") == source_norm or norm_url in seen:
            continue

        haystack = (urlparse(norm_url).path + " " + text).lower()
        if any(k in haystack for k in KEYWORDS_TIER0):
            tier = 0
        elif any(k in haystack for k in KEYWORDS_TIER1):
            tier = 1
        elif any(k in haystack for k in KEYWORDS_TIER2):
            tier = 2
        else:
            continue

        seen.add(norm_url)
        candidates.append((tier, norm_url))

    # 優先度（tier昇順）でソートし、発見順を保つ（Pythonのsortは安定ソート）
    candidates.sort(key=lambda c: c[0])
    return [url for _, url in candidates[:MAX_URLS_PER_ARTIST]]


def discover_for_artist(source_url: str) -> List[str]:
    html = _fetch_html(source_url)
    if html is None:
        return []
    return extract_candidates(html, source_url, feed_validator=_feed_alive)


def main() -> None:
    parser = argparse.ArgumentParser(description="NEWS/LIVE系監視URLの発見・登録")
    parser.add_argument("artist_ids", nargs="*", help="対象アーティストID（省略時は全件）")
    parser.add_argument("--dry-run", action="store_true", help="cache/watch_urls.json を更新しない（確認用）")
    args = parser.parse_args()

    artists_json: list = json.loads(ARTISTS_FILE.read_text())
    existing: Dict[str, List[str]] = json.loads(WATCH_URLS_FILE.read_text()) if WATCH_URLS_FILE.exists() else {}

    if args.artist_ids:
        target_ids = args.artist_ids
    else:
        target_ids = [a["id"] for a in artists_json if a.get("sourceUrl")]

    updated: Dict[str, List[str]] = {k: list(v) for k, v in existing.items()}
    artists_with_new_urls = 0
    total_new_urls = 0

    print(f"=== {len(target_ids)}組を対象に発見処理を実行 ===", file=sys.stderr)

    for i, aid in enumerate(target_ids, 1):
        entry = next((a for a in artists_json if a["id"] == aid), None)
        if entry is None or not entry.get("sourceUrl"):
            print(f"  [{i}/{len(target_ids)}] {aid}: sourceUrlが無いためスキップ", file=sys.stderr)
            continue

        source_url = entry["sourceUrl"]
        print(f"  [{i}/{len(target_ids)}] {aid} ({source_url}) ...", file=sys.stderr, end=" ")

        discovered = discover_for_artist(source_url)
        current = existing.get(aid, [])
        added = [u for u in discovered if u not in current]

        if added:
            updated[aid] = current + added
            artists_with_new_urls += 1
            total_new_urls += len(added)
            print(f"追加 {len(added)}件", file=sys.stderr)
            for u in added:
                print(f"    + {u}", file=sys.stderr)
        else:
            print("追加なし", file=sys.stderr)

        time.sleep(SLEEP_SEC)

    if args.dry_run:
        print("\n[INFO] --dry-run のため cache/watch_urls.json は更新していません", file=sys.stderr)
    else:
        WATCH_URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATCH_URLS_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
        print(f"\n[INFO] cache/watch_urls.json を更新しました", file=sys.stderr)

    total_urls = sum(len(v) for v in updated.values())
    print(
        f"\n=== 完了: URL追加 {artists_with_new_urls}組（新規{total_new_urls}件）"
        f" / 登録済みアーティスト数 {len(updated)} / 総URL数 {total_urls} ===",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
