"""HTTP取得層。ブラウザもAIも使わず、素のHTTPで取れるものは素で取る。

COLLECTION_RULES.md §2.55「取得の作法」に従い、同一ホストへは間隔をあけて
1本ずつ投げる。503は「サイトが落ちている」ではなく「投げ方が速すぎる」ことが
多いため、間隔を倍にして再試行する。
"""
import gzip
import re
import ssl
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

USER_AGENT = "Mozilla/5.0 (compatible; live-ticket-data/1.0; +https://live-ticket-data.pages.dev)"
TIMEOUT_SEC = 20
MAX_BYTES = 2 * 1024 * 1024

# 同一ホストへの連続アクセス間隔（COLLECTION_RULES §2.55 は3秒以上を要求）
HOST_INTERVAL_SEC = 3.0

# 再試行する一時エラー。恒久エラー（404等）は再試行しない
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 2

_CHARSET_IN_HEADER = re.compile(r"charset\s*=\s*[\"']?([\w\-]+)", re.I)
_CHARSET_IN_META = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""", re.I
)
_CHARSET_IN_XML = re.compile(rb"""<\?xml[^>]+encoding\s*=\s*["']([\w\-]+)""", re.I)

# Python が知らない別名。日本語サイトで実際に見かけるもの
_CHARSET_ALIASES = {
    "shift-jis": "cp932",
    "shift_jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "windows-31j": "cp932",
    "euc_jp": "euc_jp",
    "euc-jp": "euc_jp",
}


@dataclass
class FetchResult:
    """1URLの取得結果。失敗しても例外を投げず、この型で理由を返す。"""

    url: str
    final_url: str = ""
    status: Optional[int] = None
    text: str = ""
    encoding: Optional[str] = None
    elapsed_ms: int = 0
    error: Optional[str] = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and bool(self.text)


class Fetcher:
    """ホスト単位でアクセス間隔と robots.txt を管理する取得器。

    1インスタンスをバッチ全体で使い回すことで、別アーティストが同じホスト
    （レーベル・事務所サイト）に乗っていても間隔が守られる。
    """

    def __init__(
        self,
        user_agent: str = USER_AGENT,
        host_interval_sec: float = HOST_INTERVAL_SEC,
        timeout_sec: int = TIMEOUT_SEC,
        respect_robots: bool = True,
    ):
        self.user_agent = user_agent
        self.host_interval_sec = host_interval_sec
        self.timeout_sec = timeout_sec
        self.respect_robots = respect_robots
        self._last_hit: Dict[str, float] = {}
        self._robots: Dict[str, Optional[RobotFileParser]] = {}
        self._sleep = time.sleep

    # -- ホスト間隔 ---------------------------------------------------

    def _pace(self, url: str, multiplier: float = 1.0) -> None:
        host = urlparse(url).netloc.lower()
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.host_interval_sec * multiplier - (time.time() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = time.time()

    # -- robots.txt ---------------------------------------------------

    def _robots_for(self, url: str) -> Optional[RobotFileParser]:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in self._robots:
            return self._robots[host_key]

        rp = RobotFileParser()
        rp.set_url(host_key + "/robots.txt")
        try:
            req = urllib.request.Request(
                host_key + "/robots.txt", headers={"User-Agent": self.user_agent}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read(256 * 1024).decode("utf-8", errors="replace")
            rp.parse(body.splitlines())
        except Exception:
            # robots.txt が無い／読めないサイトは「制限なし」として扱う。
            # 取得できないこと自体を禁止と解釈すると大半のサイトが落ちる
            rp = None

        self._robots[host_key] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # -- 取得 ---------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """1URLを取得する。例外は投げず FetchResult.error に理由を入れる。"""
        if not urlparse(url).scheme in ("http", "https"):
            return FetchResult(url=url, error="unsupported_scheme")

        if not self.allowed(url):
            return FetchResult(url=url, error="robots_disallow")

        attempt = 0
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        while attempt <= MAX_RETRIES:
            # 再試行時は間隔を倍にする（§2.55）
            self._pace(url, multiplier=2.0 ** attempt)
            started = time.time()
            try:
                result = self._fetch_once(url)
                result.elapsed_ms = int((time.time() - started) * 1000)
                if result.status is not None and result.status in RETRY_STATUSES:
                    last_status, last_error = result.status, f"http_{result.status}"
                    attempt += 1
                    continue
                return result
            except urllib.error.HTTPError as e:
                last_status = e.code
                last_error = f"http_{e.code}"
                if e.code not in RETRY_STATUSES:
                    return FetchResult(
                        url=url,
                        status=e.code,
                        error=last_error,
                        elapsed_ms=int((time.time() - started) * 1000),
                    )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"[:200]
            attempt += 1

        return FetchResult(url=url, status=last_status, error=last_error or "unknown")

    def _fetch_once(self, url: str) -> FetchResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=self.timeout_sec, context=ctx) as resp:
            raw = resp.read(MAX_BYTES + 1)
            truncated = len(raw) > MAX_BYTES
            raw = raw[:MAX_BYTES]
            raw = _decompress(raw, resp.headers.get("Content-Encoding"))
            charset = _charset_from_header(resp.headers.get("Content-Type"))
            text, encoding = decode_body(raw, charset)
            return FetchResult(
                url=url,
                final_url=resp.geturl(),
                status=getattr(resp, "status", resp.getcode()),
                text=text,
                encoding=encoding,
                truncated=truncated,
            )


def _decompress(raw: bytes, content_encoding: Optional[str]) -> bytes:
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        # 途中で切れた圧縮データは展開できない。生のまま返してデコードに任せる
        return raw
    return raw


def _charset_from_header(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    m = _CHARSET_IN_HEADER.search(content_type)
    return m.group(1) if m else None


def _normalize_charset(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    # Content-Type ヘッダ丸ごと渡されても動くようにしておく
    if "charset" in name.lower():
        extracted = _charset_from_header(name)
        name = extracted or name
    key = name.strip().strip("\"'").lower()
    return _CHARSET_ALIASES.get(key, key)


def decode_body(raw: bytes, header_charset: Optional[str] = None):
    """バイト列を文字列にする。ヘッダ → meta/xml宣言 → utf-8 の順で試す。

    日本語サイトには cp932 / euc-jp が残っているため、utf-8 決め打ちにしない。
    """
    candidates = []
    for name in (
        _normalize_charset(header_charset),
        _normalize_charset(_sniff_meta_charset(raw)),
        "utf-8",
    ):
        if name and name not in candidates:
            candidates.append(name)

    for name in candidates:
        try:
            return raw.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _sniff_meta_charset(raw: bytes) -> Optional[str]:
    head = raw[:4096]
    m = _CHARSET_IN_META.search(head) or _CHARSET_IN_XML.search(head)
    if m:
        try:
            return m.group(1).decode("ascii")
        except Exception:
            return None
    return None
