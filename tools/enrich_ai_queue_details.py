#!/usr/bin/env python3
"""
AI queue の「見出しまでは取れたが詳細本文まで追えていない」ケースを、
公式サイト内リンクだけで補完する非破壊enricher。

- 入力 queue は変更しない
- 出力は local_llm/ 配下の別JSON
- 同一サイト内リンクのみ
- javascript/mailto/tel/image等は除外
- 見出しとanchor textの一致度でdetail linkを選ぶ
- LLMは使用しない
"""

import argparse
import copy
import difflib
import html
import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 local-live-ticket-enricher/1.0"
LIVE_HINT = re.compile(
    r"LIVE|Live|live|ライブ|公演|ツアー|TOUR|Tour|tour|出演|フェス|FES|Festival|コンサート|開催決定"
)
DATE_HINT = re.compile(
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日"
)
BAD_SCHEME = ("javascript:", "mailto:", "tel:", "data:")
BAD_EXT = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4|mp3)(?:$|\?)", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", html.unescape(str(s or ""))).lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[「」『』【】〖〗\[\]()（）<>＜＞!！?？'\"“”‘’・,:：;；~〜～_-]", "", s)
    return s


def date_tokens(s: str) -> set:
    return set(DATE_HINT.findall(str(s or "")))


def site_key(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # 日本の一般的なco.jp等だけ少し広く扱う。
    if parts[-2:] in (["co", "jp"], ["ne", "jp"], ["or", "jp"]):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    return bool(site_key(a)) and site_key(a) == site_key(b)


def valid_href(base: str, href: str) -> Optional[str]:
    href = html.unescape((href or "").strip())
    if not href or href.startswith("#") or href.lower().startswith(BAD_SCHEME):
        return None
    abs_url = urllib.parse.urljoin(base, href)
    p = urllib.parse.urlparse(abs_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    if BAD_EXT.search(abs_url):
        return None
    if not same_site(base, abs_url):
        return None
    # fragmentは不要
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: List[Tuple[str, str]] = []
        self._href = None
        self._anchor_parts: List[str] = []
        self._skip = 0
        self._text_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "a":
            if self._href:
                text = " ".join(x for x in self._anchor_parts if x).strip()
                self.anchors.append((self._href, text))
            self._href = None
            self._anchor_parts = []

    def handle_data(self, data):
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data or "").strip()
        if not text:
            return
        self._text_parts.append(text)
        if self._href is not None:
            self._anchor_parts.append(text)

    @property
    def text(self):
        return " ".join(self._text_parts)


def fetch_page(url: str, timeout: int, max_bytes: int) -> Tuple[str, PageParser]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        ctype = (res.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            raise ValueError(f"HTMLではありません: {ctype}")
        raw = res.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        charset = res.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    parser = PageParser()
    parser.feed(text)
    return text, parser


def score_anchor(reference: str, anchor_text: str, href: str) -> float:
    r = norm(reference)
    a = norm(anchor_text)
    if len(a) < 4:
        return -999
    score = difflib.SequenceMatcher(None, r, a).ratio() * 100
    if len(a) >= 8 and (a in r or r in a):
        score += 180
    common_dates = date_tokens(reference) & date_tokens(anchor_text)
    score += 35 * len(common_dates)
    if LIVE_HINT.search(anchor_text):
        score += 15
    path = urllib.parse.urlparse(href).path.lower()
    if any(x in path for x in ("/detail/", "/news/", "/live/", "/event/", "/contents/")):
        score += 20
    if any(x in path for x in ("/tag/", "/category/", "/archive/", "/page/")):
        score -= 10
    return score


def relevant_block(text: str) -> bool:
    text = str(text or "")
    return len(norm(text)) >= 10 and (LIVE_HINT.search(text) or DATE_HINT.search(text))


def trim_detail(text: str, reference: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    ref_words = [x for x in re.split(r"\s+", reference) if len(x) >= 4]
    pos = -1
    for w in ref_words:
        pos = text.find(w)
        if pos >= 0:
            break
    if pos < 0:
        return text[:limit]
    half = limit // 2
    start = max(0, pos - half)
    return text[start:start + limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="AI queue公式detail補完")
    ap.add_argument("--queue", type=Path, default=Path("cache/ai_queue.json"))
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("local_llm/enriched_queue/ai_queue_enriched.json"),
    )
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--max-bytes", type=int, default=2_000_000)
    ap.add_argument("--detail-chars", type=int, default=2500)
    ap.add_argument("--score-threshold", type=float, default=120.0)
    ap.add_argument("--per-block", type=int, default=1)
    ap.add_argument("--max-details-per-artist", type=int, default=4)
    ap.add_argument("--max-source-pages", type=int, default=8)
    ap.add_argument("artist_ids", nargs="*")
    args = ap.parse_args()

    data = json.loads(args.queue.read_text(encoding="utf-8"))
    out = copy.deepcopy(data)
    requested = set(args.artist_ids)
    report = []
    page_cache: Dict[str, Tuple[str, PageParser]] = {}

    for item in out.get("items", []):
        aid = item.get("artistId")
        if requested and aid not in requested:
            continue

        blocks = [b for b in item.get("evidenceBlocks", []) or [] if relevant_block(b.get("text", ""))]
        if not blocks:
            continue

        source_urls = []
        for src in item.get("sources", []) or []:
            url = src.get("url")
            if url and url not in source_urls:
                source_urls.append(url)
        for b in blocks:
            url = b.get("sourceUrl")
            if url and url not in source_urls:
                source_urls.append(url)

        # depth0/list/homeを優先。多すぎる場合は上限をかける。
        source_urls = source_urls[:args.max_source_pages]
        all_anchors: List[Tuple[str, str, str]] = []
        source_errors = []

        for url in source_urls:
            try:
                if url not in page_cache:
                    page_cache[url] = fetch_page(url, args.timeout, args.max_bytes)
                _, parser = page_cache[url]
                for href, anchor_text in parser.anchors:
                    abs_url = valid_href(url, href)
                    if abs_url:
                        all_anchors.append((abs_url, anchor_text, url))
            except Exception as exc:
                source_errors.append({"url": url, "error": str(exc)})

        existing_urls = {b.get("sourceUrl") for b in item.get("evidenceBlocks", []) or []}
        added = []
        seen_detail_urls = set()

        proposals = {}
        for block in blocks:
            reference = block.get("text", "")
            ranked = []
            for abs_url, anchor_text, discovered_from in all_anchors:
                sc = score_anchor(reference, anchor_text, abs_url)
                if sc >= args.score_threshold:
                    ranked.append((sc, abs_url, anchor_text, discovered_from, reference))
            ranked.sort(key=lambda x: (-x[0], len(x[1])))

            for proposal in ranked[:args.per_block]:
                sc, detail_url, anchor_text, discovered_from, reference = proposal
                prev = proposals.get(detail_url)
                if prev is None or sc > prev[0]:
                    proposals[detail_url] = proposal

        selected = sorted(
            proposals.values(),
            key=lambda x: (-x[0], len(x[1]))
        )[:args.max_details_per_artist]

        for sc, detail_url, anchor_text, discovered_from, reference in selected:
            if detail_url in seen_detail_urls:
                continue
            seen_detail_urls.add(detail_url)

            try:
                if detail_url not in page_cache:
                    page_cache[detail_url] = fetch_page(
                        detail_url, args.timeout, args.max_bytes
                    )
                _, detail_parser = page_cache[detail_url]
                detail_text = trim_detail(
                    detail_parser.text,
                    reference=anchor_text or reference,
                    limit=args.detail_chars,
                )
                if len(norm(detail_text)) < 40:
                    continue

                duplicate_text = any(
                    norm(detail_text) == norm(x.get("text", ""))
                    for x in item.get("evidenceBlocks", []) or []
                )
                if duplicate_text:
                    continue

                new_block = {
                    "sourceUrl": detail_url,
                    "text": detail_text,
                    "kind": "detail_enriched",
                    "discoveredFrom": discovered_from,
                    "matchedAnchorText": anchor_text,
                    "matchedEvidenceText": reference,
                    "linkScore": round(sc, 2),
                }
                item.setdefault("evidenceBlocks", []).append(new_block)
                added.append(new_block)
            except Exception as exc:
                source_errors.append({"url": detail_url, "error": str(exc)})

        report.append({
            "artistId": aid,
            "artistName": item.get("artistName"),
            "candidateBlocks": len(blocks),
            "anchorsConsidered": len(all_anchors),
            "addedDetails": len(added),
            "added": [
                {
                    "sourceUrl": x["sourceUrl"],
                    "matchedAnchorText": x["matchedAnchorText"],
                    "linkScore": x["linkScore"],
                }
                for x in added
            ],
            "errors": source_errors,
        })

        print(
            f"{item.get('artistName') or aid}: "
            f"detail追加={len(added)} / anchors={len(all_anchors)}"
        )

    out.setdefault("enrichment", {})
    out["enrichment"]["detailLinkEnricher"] = {
        "schemaVersion": 1,
        "report": report,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
