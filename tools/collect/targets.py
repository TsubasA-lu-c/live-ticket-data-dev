"""アーティストごとの取得設定。

**最初から50組分の専用パーサーは作らない。** 基本は汎用で、うまく取れない
サイトだけ config/collect_targets.json に個別設定を足して吸収する。

初回は artists.json の sourceUrl と cache/watch_urls.json（既存の発見結果）から
LIVE/NEWS ページを仕分けして保存し、次回以降はそのURLを直接取りに行く。
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

CONFIG_FILE = Path("config/collect_targets.json")
ARTISTS_FILE = Path("data/artists.json")
WATCH_URLS_FILE = Path("cache/watch_urls.json")

FETCH_HTTP = "http"
FETCH_BROWSER = "browser"
FETCH_SKIP = "skip"

_LIVE_RE = re.compile(r"(live|tour|schedule|concert|event|gig|公演|ライブ|スケジュール)", re.I)
_NEWS_RE = re.compile(r"(news|topics|info|information|blog|お知らせ|ニュース)", re.I)
_FEED_RE = re.compile(r"(rss|atom|feed|\.xml$)", re.I)
# 末尾が数字のパスは個別記事。監視先としてすぐ陳腐化する
_ARTICLE_RE = re.compile(r"/\d+/?$")


@dataclass
class Target:
    artist_id: str
    artist_name: str = ""
    official_url: str = ""
    live_url: Optional[str] = None
    news_url: Optional[str] = None
    feed_url: Optional[str] = None
    extra_urls: List[str] = field(default_factory=list)
    fetch_type: str = FETCH_HTTP
    parser_type: str = "generic"
    selector: Optional[str] = None
    note: Optional[str] = None
    discovered: bool = False

    def urls(self) -> List[str]:
        """取得順。**ライブ情報が濃い順**に並べる。"""
        ordered = [self.live_url, self.feed_url, self.news_url, self.official_url]
        ordered += self.extra_urls
        seen, out = set(), []
        for u in ordered:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def to_dict(self) -> Dict:
        return {
            "liveUrl": self.live_url,
            "newsUrl": self.news_url,
            "feedUrl": self.feed_url,
            "extraUrls": self.extra_urls,
            "fetchType": self.fetch_type,
            "parserType": self.parser_type,
            "selector": self.selector,
            "note": self.note,
        }


def classify(urls: List[str], official_url: str) -> Dict[str, object]:
    """URL群を live / news / feed に仕分ける。

    一覧ページを個別記事より優先する。個別記事は新しい記事が出れば
    リンクから外れ、監視先として使えなくなるため。
    """
    live: Optional[str] = None
    news: Optional[str] = None
    feed: Optional[str] = None
    extra: List[str] = []

    def better(current: Optional[str], candidate: str) -> str:
        if current is None:
            return candidate
        cur_article = bool(_ARTICLE_RE.search(urlparse(current).path))
        new_article = bool(_ARTICLE_RE.search(urlparse(candidate).path))
        if cur_article and not new_article:
            return candidate
        return current

    for url in urls:
        if not url or url == official_url:
            continue
        path = urlparse(url).path + "?" + (urlparse(url).query or "")
        if _FEED_RE.search(path):
            feed = feed or url
        elif _LIVE_RE.search(path):
            live = better(live, url)
        elif _NEWS_RE.search(path):
            news = better(news, url)
        elif url not in extra:
            extra.append(url)

    return {"live": live, "news": news, "feed": feed, "extra": extra[:2]}


def load_config(path: Path = CONFIG_FILE) -> Dict:
    if not path.exists():
        return {"defaults": {"fetchType": FETCH_HTTP, "parserType": "generic"}, "artists": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"defaults": {"fetchType": FETCH_HTTP, "parserType": "generic"}, "artists": {}}
    data.setdefault("defaults", {"fetchType": FETCH_HTTP, "parserType": "generic"})
    data.setdefault("artists", {})
    return data


def save_config(config: Dict, path: Path = CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_targets(artist_ids: Optional[List[str]] = None,
                  artists_file: Path = ARTISTS_FILE,
                  watch_file: Path = WATCH_URLS_FILE,
                  config_file: Path = CONFIG_FILE) -> List[Target]:
    """設定と既存データから取得対象を組み立てる。設定が無い分は自動で仕分ける。"""
    artists = json.loads(Path(artists_file).read_text(encoding="utf-8"))
    watch: Dict[str, List[str]] = {}
    if Path(watch_file).exists():
        try:
            watch = json.loads(Path(watch_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            watch = {}
    config = load_config(Path(config_file))
    entries: Dict[str, Dict] = config["artists"]

    wanted = set(artist_ids) if artist_ids else None
    targets: List[Target] = []

    for artist in artists:
        aid = artist["id"]
        if wanted is not None and aid not in wanted:
            continue

        entry = entries.get(aid) or {}
        target = Target(
            artist_id=aid,
            artist_name=artist.get("name", aid),
            official_url=artist.get("sourceUrl") or "",
            live_url=entry.get("liveUrl"),
            news_url=entry.get("newsUrl"),
            feed_url=entry.get("feedUrl"),
            extra_urls=list(entry.get("extraUrls") or []),
            fetch_type=entry.get("fetchType") or config["defaults"].get("fetchType", FETCH_HTTP),
            parser_type=entry.get("parserType") or config["defaults"].get("parserType", "generic"),
            selector=entry.get("selector"),
            note=entry.get("note"),
        )

        if not entry:
            # 初回のみ既存の監視URLから仕分けて保存する
            found = classify(watch.get(aid, []), target.official_url)
            target.live_url = found["live"]
            target.news_url = found["news"]
            target.feed_url = found["feed"]
            target.extra_urls = list(found["extra"])
            target.discovered = True
            entries[aid] = target.to_dict()

        targets.append(target)

    if any(t.discovered for t in targets):
        save_config(config, Path(config_file))

    return targets
