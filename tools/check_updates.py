#!/usr/bin/env python3
"""
各アーティストのソースURLを確認し、前回から変更があったアーティストIDを stdout に1行ずつ出力する。
cache/source_hashes.json にフィンガープリントを保存して差分検出に使う。

使い方:
  python3 tools/check_updates.py              # 全アーティストをチェック
  python3 tools/check_updates.py yuzu milk    # 指定アーティストのみ
  python3 tools/check_updates.py --no-cache   # cache更新しない（テスト用）
"""
import argparse, hashlib, json, re, sys, time
import urllib.error, urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional

CACHE_FILE = Path("cache/source_hashes.json")
ARTISTS_FILE = Path("data/artists.json")
ARTIST_DIR = Path("data/artist")


class _TextExtractor(HTMLParser):
    """HTMLからテキストを抽出（script/style/noscriptタグを除外）"""

    def __init__(self):
        super().__init__()
        self._skip = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _content_hash(html_bytes: bytes) -> str:
    """HTMLのテキストコンテンツだけを抽出してMD5ハッシュを返す。
    script/styleタグと10桁以上の数字（タイムスタンプ）を除去して安定化させる。"""
    try:
        html = html_bytes.decode("utf-8", errors="replace")
        parser = _TextExtractor()
        parser.feed(html)
        text = parser.get_text()
        text = re.sub(r"\b\d{10,}\b", "", text)
    except Exception:
        text = html_bytes.decode("utf-8", errors="replace")
    return hashlib.md5(text.encode()).hexdigest()


def _fetch_fingerprint(url: str) -> Optional[Dict]:
    """URLのコンテンツハッシュを返す。取得失敗時はNone。
    ETag は多くのSSRフレームワークでリクエストごとに動的生成されるため信頼できない。
    常にGETしてテキスト抽出ハッシュで比較する。"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; live-ticket-data/1.0)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read(512 * 1024)  # 最大512KB
            return {"type": "hash", "hash": _content_hash(content)}
    except Exception as e:
        print(f"  [WARN] fetch失敗 {url}: {e}", file=sys.stderr)
        return None


def _fingerprint_changed(new_fp: Dict, old_fp: Optional[Dict]) -> bool:
    """フィンガープリントが変化したかを判定。old_fp なし（初回）は変化あり扱い。"""
    if old_fp is None:
        return True
    if new_fp.get("type") == "hash" and old_fp.get("type") == "hash":
        return new_fp.get("hash") != old_fp.get("hash")
    # 判定不能 → 安全側に倒して変化あり扱い
    return True


def _get_artist_urls(artist_id: str, artists_json: List) -> List[str]:
    """アーティストの監視対象URLを返す。
    artists.json の sourceUrl（公式サイト）のみを使用する。
    個別イベントページは公式発表後ほぼ変化せず、サードパーティURL（livefans等）は
    動的生成で毎回ハッシュが変わるため、公式サイト1本に絞る。"""
    for a in artists_json:
        if a["id"] == artist_id and a.get("sourceUrl"):
            return [a["sourceUrl"]]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="ソースURL差分チェック")
    parser.add_argument("artist_ids", nargs="*", help="対象アーティストID（省略時は全件）")
    parser.add_argument("--no-cache", action="store_true", help="cache/source_hashes.json を更新しない")
    args = parser.parse_args()

    artists_json: list = json.loads(ARTISTS_FILE.read_text())
    cache: dict = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    target_ids = args.artist_ids if args.artist_ids else [a["id"] for a in artists_json]

    changed: List[str] = []
    new_cache = dict(cache)

    print(f"=== {len(target_ids)}組をチェック中 ===", file=sys.stderr)

    for i, aid in enumerate(target_ids, 1):
        urls = _get_artist_urls(aid, artists_json)
        artist_changed = False

        print(f"  [{i}/{len(target_ids)}] {aid} ({len(urls)}URL) ...", file=sys.stderr, end=" ")

        for url in urls:
            fp = _fetch_fingerprint(url)
            old_fp = cache.get(aid, {}).get(url)

            if fp is None:
                # 取得失敗 → 安全のため変化あり扱い
                artist_changed = True
            elif _fingerprint_changed(fp, old_fp):
                artist_changed = True
                if not args.no_cache:
                    new_cache.setdefault(aid, {})[url] = fp

        status = "変更あり" if artist_changed else "変化なし"
        print(status, file=sys.stderr)

        if artist_changed:
            changed.append(aid)

        time.sleep(0.3)  # サーバー負荷軽減

    # 変化ありアーティストを stdout に出力（メインエージェントが使う）
    for aid in changed:
        print(aid)

    # cache更新
    if not args.no_cache:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2))
        print(f"\n[INFO] cache/source_hashes.json を更新しました", file=sys.stderr)

    print(
        f"\n=== 完了: 変更あり {len(changed)}/{len(target_ids)}組 ===",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
