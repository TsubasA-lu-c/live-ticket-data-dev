"""収集バッチ本体。

  取得 → 正規化 → 差分 → 機械解析 → 既存突き合わせ → （必要な分だけ）AIキュー

AIをこの中から呼ぶことはない。**AIに渡す材料をファイルに書き出すだけ**で、
実際の構造化はAI実行側が cache/ai_queue.json を読んで行う
（親 `AGENTS.md` のコスト制約に従い、従量課金APIは使わない）。
"""
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import diff as diffmod
from . import extract as extractmod
from . import merge as mergemod
from . import normalize as normmod
from .fetcher import Fetcher, FetchResult
from .venues import VenueMaster
from .targets import FETCH_BROWSER, FETCH_SKIP, Target, build_targets, refresh_month_query

STATE_FILE = Path("cache/collect_state.json")
# 収集が終わっていない分の指紋。確定は --accept で行う
PENDING_STATE_FILE = Path("cache/collect_state.pending.json")
SNAPSHOT_DIR = Path("cache/normalized")
PENDING_SNAPSHOT_DIR = Path("cache/normalized_pending")
AI_QUEUE_FILE = Path("cache/ai_queue.json")
METRICS_FILE = Path("cache/collect_metrics.jsonl")
ARTIST_DIR = Path("data/artist")

# 静的HTMLからこの文字数しか取れない場合、JSレンダリングを疑う
JS_SUSPECT_CHARS = 200

# AIへ渡すテキストの上限。ここを超えるならそもそも渡し方を見直すべき
MAX_AI_CHARS_PER_ARTIST = 9000
MAX_AI_BLOCKS = 25
# 機械抽出済みの公演は参考として載せるだけ。全量を積むと本末転倒になる
MAX_PARSED_EVENTS_IN_QUEUE = 30

# 変更行から辿る公式詳細ページ。無制限クロールにしない。
MAX_FOLLOW_DEPTH = 2
MAX_FOLLOW_PAGES_PER_ARTIST = 4
FOLLOW_RELEVANCE = re.compile(
    r"(live|tour|event|concert|ticket|schedule|festival|\bfes\b|"
    r"公演|ライブ|ツアー|チケット|フェス|追加公演|開催)",
    re.I,
)
FOLLOW_STRONG = re.compile(
    r"(tour|concert|ticket|schedule|event|festival|\bfes\b|"
    r"公演|ツアー|チケット|イベント|フェス|開催決定|追加公演)",
    re.I,
)
FOLLOW_EXCLUDE = re.compile(
    r"(facebook|twitter|x\.com|instagram|youtube|line\.me|share|sns|"
    r"privacy|policy|terms|company|corporate|contact|規約|会社|プライバシー|問い合わせ)",
    re.I,
)
FOLLOW_MEDIA_EXCLUDE = re.compile(
    r"(cdtv|music station|ミュージックステーション|テレビ|tv|ラジオ|radio|"
    r"blu-?ray|dvd|映像|video|配信リリース|streaming|download|リスニングパーティー|"
    r"goods|グッズ|物販|通販|会場販売|販売のお知らせ)",
    re.I,
)
EVIDENCE_PRIORITY = re.compile(
    r"(受付終了|update|new|追加公演|開催決定|情報更新|受付開始)", re.I
)

# トークン概算。日本語主体のテキストは1文字≒0.9トークンで見積もる
TOKENS_PER_CHAR = 0.9


def _load_json_object(path: Path) -> Dict:
    if not Path(path).exists():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_json_object(path: Path, value: Dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_chars(len(text))


def estimate_tokens_from_chars(chars: int) -> int:
    return int(chars * TOKENS_PER_CHAR)


@dataclass
class PageOutcome:
    url: str
    fetched_at: Optional[str] = None
    final_url: Optional[str] = None
    elapsed_ms: int = 0
    status: Optional[int] = None
    error: Optional[str] = None
    diff_status: str = ""
    content_hash: str = ""
    text_chars: int = 0
    raw_chars: int = 0
    added_lines: List[str] = field(default_factory=list)
    events: List[extractmod.ExtractedEvent] = field(default_factory=list)
    lottery_blocks: List[str] = field(default_factory=list)
    unresolved_dates: List[str] = field(default_factory=list)
    js_suspect: bool = False
    normalized_text: str = ""
    links: List[Tuple[str, str]] = field(default_factory=list)
    link_contexts: List[Dict[str, str]] = field(default_factory=list)
    depth: int = 0
    discovered_from: Optional[str] = None
    discovery_evidence: Optional[str] = None
    page_title: Optional[str] = None
    headings: List[str] = field(default_factory=list)
    live_related_added: List[str] = field(default_factory=list)
    ignored_added: List[str] = field(default_factory=list)
    relevant_links: List[Tuple[str, str]] = field(default_factory=list)
    category_warning: Optional[str] = None
    artist_scoped: bool = True


@dataclass
class ArtistOutcome:
    artist_id: str
    artist_name: str
    pages: List[PageOutcome] = field(default_factory=list)
    fetch_ok: bool = False
    changed: bool = False
    parser_ok: bool = False
    ai_reason: Optional[str] = None
    ai_chars: int = 0
    statuses: List[mergemod.EventStatus] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    artist_aliases: List[str] = field(default_factory=list)
    official_url: str = ""

    def log_lines(self) -> List[str]:
        lines = [self.artist_name or self.artist_id]
        lines.append("FETCH_OK" if self.fetch_ok else "FETCH_FAILED")
        if not self.fetch_ok:
            lines.extend(self.errors[:2])
            return lines
        lines.append("CONTENT_CHANGED" if self.changed else "NO_CHANGE")
        if not self.changed:
            lines.append("AI_NOT_USED")
            return lines
        lines.append("PARSER_SUCCESS" if self.parser_ok else "PARSER_FAILED")
        counts = mergemod.summarize(self.statuses)
        lines.append(f"NEW_EVENTS={counts[mergemod.NEW]}")
        if counts[mergemod.UPDATED]:
            lines.append(f"UPDATED_EVENTS={counts[mergemod.UPDATED]}")
        if counts[mergemod.REMOVED]:
            lines.append(f"MISSING_ON_SITE={counts[mergemod.REMOVED]}")
        if self.ai_reason:
            lines.append(f"AI_FALLBACK_USED reason={self.ai_reason}")
            lines.append(f"INPUT_TOKENS≈{estimate_tokens_from_chars(self.ai_chars)}")
        else:
            lines.append("AI_NOT_USED")
        return lines


class Pipeline:
    def __init__(self, fetcher: Optional[Fetcher] = None,
                 state_file: Path = STATE_FILE,
                 snapshot_dir: Path = SNAPSHOT_DIR,
                 artist_dir: Path = ARTIST_DIR,
                 today: Optional[_date] = None,
                 update_cache: bool = True,
                 pending_state_file: Optional[Path] = None,
                 pending_snapshot_dir: Optional[Path] = None):
        self.fetcher = fetcher or Fetcher()
        self.state_file = Path(state_file)
        self.pending_state_file = Path(
            pending_state_file if pending_state_file is not None
            else Path(state_file).with_suffix(".pending.json")
        )
        self.store = diffmod.SnapshotStore(Path(snapshot_dir))
        self.pending_store = diffmod.SnapshotStore(Path(
            pending_snapshot_dir if pending_snapshot_dir is not None
            else Path(snapshot_dir).parent / (Path(snapshot_dir).name + "_pending")
        ))
        self.artist_dir = Path(artist_dir)
        self.today = today or _date.today()
        self.update_cache = update_cache
        # 抽出した会場名は、既存データに書く前にマスタの正式表記へ寄せる。
        # 表記ゆれを発生源で潰すのが狙い（マスタに無い会場は素通し）
        self.venue_master = VenueMaster.load()
        self.state: Dict = _load_json_object(self.state_file)
        self.pending: Dict = _load_json_object(self.pending_state_file)

    def save_state(self) -> None:
        if not self.update_cache:
            return
        _write_json_object(self.state_file, self.state)
        _write_json_object(self.pending_state_file, self.pending)

    # -- 1アーティスト ------------------------------------------------

    def run_artist(self, target: Target) -> ArtistOutcome:
        outcome = ArtistOutcome(
            artist_id=target.artist_id,
            artist_name=target.artist_name,
            artist_aliases=list(target.aliases),
            official_url=target.official_url,
        )

        if target.fetch_type == FETCH_SKIP:
            outcome.errors.append("skip指定のため取得しません")
            return outcome

        urls = target.urls(today=self.today)
        if not urls:
            outcome.errors.append("監視URLがありません（sourceUrl未設定）")
            return outcome

        artist_data = self._load_artist_data(target.artist_id)
        url_records: Dict[str, Dict] = {}

        for url in urls:
            page = self._run_page(target, url)
            outcome.pages.append(page)

            if page.error:
                outcome.errors.append(f"{url}: {page.error}")
            else:
                outcome.fetch_ok = True
                if page.category_warning:
                    outcome.warnings.append(f"{url}: {page.category_warning}")
                if page.diff_status not in (diffmod.NO_CHANGE, diffmod.VOLATILE):
                    outcome.changed = True

            url_records[url] = self._page_record(page)

        if not outcome.fetch_ok:
            self._record(target.artist_id, url_records, outcome, "FETCH_FAILED", confirmed=False)
            return outcome

        if not outcome.changed:
            self._record(target.artist_id, url_records, outcome, diffmod.NO_CHANGE, confirmed=True)
            return outcome

        # 一覧の変更行に結び付く、HTML内に実在する公式リンクだけを限定追跡する。
        # LLMにはURLを選ばせず、同一ホスト・深さ・ページ数をここで固定する。
        followed = self._follow_relevant_links(target, outcome.pages, set(urls))
        for page in followed:
            outcome.pages.append(page)
            url_records[page.url] = self._page_record(page)
            if page.error:
                outcome.errors.append(f"{page.url}: {page.error}")
            if page.category_warning:
                outcome.warnings.append(f"{page.url}: {page.category_warning}")

        events: List[extractmod.ExtractedEvent] = []
        for page in outcome.pages:
            events.extend(
                event for event in page.events
                if _page_text_relevant(outcome, page, event.source_text)
                # 年の無い日付と低信頼の会場抽出は、公式テキストとして
                # evidenceに残しても構造化候補にはしない。日付推測を防ぐ。
                and not event.year_inferred
                and event.confidence >= 0.6
            )
        events = _dedupe_events(events)

        outcome.parser_ok = any(e.confidence >= 0.6 for e in events)
        # 詳細リンクで見つけた一部日程と、既存全公演を比較して「削除」とはしない。
        # REMOVEDは監視中のLIVE一覧そのものが十分に構造化できた場合だけ報告する。
        known_future_count = sum(
            1 for performance in (artist_data.get("performances") or [])
            if (mergemod._date_of(performance) or "") >= self.today.isoformat()
        )
        schedule_coverage_ok = (
            known_future_count == 0
            or len(events) >= max(3, int(known_future_count * 0.6))
        )
        authoritative_schedule = schedule_coverage_ok and any(
            page.depth == 0 and target.live_url
            and page.url == refresh_month_query(target.live_url, self.today)
            and page.events and not page.category_warning
            for page in outcome.pages
        )
        outcome.statuses = mergemod.diff_events(
            events,
            artist_data,
            today=self.today,
            include_removed=authoritative_schedule,
        )
        outcome.ai_reason = self._ai_reason(outcome)

        result = (
            "AI_QUEUED" if outcome.ai_reason else
            ("PARSER_SUCCESS" if outcome.parser_ok else "NO_EVENTS")
        )
        # AIに回した分は、収集とvalidateが済むまで指紋を確定しない。
        # ここで確定すると「変更は検知したが未収集」のまま次回 NO_CHANGE になり、
        # 更新が黙って消える（旧 check_updates.py と同じ pending 方式に揃える）。
        #
        # ただし JS_RENDERED は例外。**本文が読めていないので保留する中身が無く**、
        # 保留にすると毎回キューに載り続けてAI呼び出しが減らない。
        # サイト側の対応が要る事案として要確認ポイントに出し、指紋は確定させる。
        self._record(target.artist_id, url_records, outcome, result,
                     confirmed=outcome.ai_reason in (None, "JS_RENDERED"),
                     parser_events=len(events))
        return outcome

    def _page_record(self, page: PageOutcome) -> Dict:
        return {
            "lastFetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "httpStatus": page.status,
            "contentHash": page.content_hash,
            "normalizedChars": page.text_chars,
            "diffStatus": page.diff_status,
            "jsSuspect": page.js_suspect,
            "error": page.error,
            "depth": page.depth,
            "discoveredFrom": page.discovered_from,
        }

    def _follow_relevant_links(
        self, target: Target, seed_pages: List[PageOutcome], seen: set
    ) -> List[PageOutcome]:
        followed: List[PageOutcome] = []
        frontier = list(seed_pages)
        allowed_hosts = {
            _host_key(page.url) for page in seed_pages if _host_key(page.url)
        }

        while frontier and len(followed) < MAX_FOLLOW_PAGES_PER_ARTIST:
            parent = frontier.pop(0)
            if parent.error or parent.depth >= MAX_FOLLOW_DEPTH:
                continue
            parent.relevant_links = _rank_follow_links(parent)
            for label, url in parent.relevant_links:
                if len(followed) >= MAX_FOLLOW_PAGES_PER_ARTIST:
                    break
                if url in seen or _host_key(url) not in allowed_hosts:
                    continue
                seen.add(url)
                page = self._run_page(
                    target,
                    url,
                    depth=parent.depth + 1,
                    discovered_from=parent.url,
                    discovery_evidence=label,
                )
                followed.append(page)
                if not page.error:
                    frontier.append(page)
        return followed

    # -- 指紋の確定・保留 ----------------------------------------------

    def _record(self, artist_id: str, url_records: Dict[str, Dict],
                outcome: "ArtistOutcome", result: str, confirmed: bool,
                parser_events: Optional[int] = None) -> None:
        entry = {
            "urls": url_records,
            "lastRunAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "lastResult": result,
            "aiFallback": outcome.ai_reason,
        }
        if parser_events is not None:
            entry["parserEvents"] = parser_events

        if confirmed:
            self.state[artist_id] = entry
            self.pending.pop(artist_id, None)
            if self.update_cache:
                for page in outcome.pages:
                    if page.normalized_text:
                        self.store.save(artist_id, page.url, page.normalized_text)
                self.pending_store.drop(artist_id)
            return

        # 未確定。取得結果は pending に置き、確定済みの指紋は触らない
        self.pending[artist_id] = entry
        confirmed_entry = self.state.setdefault(artist_id, {"urls": {}})
        confirmed_entry["lastRunAt"] = entry["lastRunAt"]
        confirmed_entry["pendingResult"] = result
        if self.update_cache:
            for page in outcome.pages:
                if page.normalized_text:
                    self.pending_store.save(artist_id, page.url, page.normalized_text)

    def accept(self, artist_ids: List[str]) -> List[str]:
        """収集とvalidateが済んだアーティストの指紋を確定する。"""
        accepted: List[str] = []
        for aid in artist_ids:
            entry = self.pending.pop(aid, None)
            if entry is None:
                continue
            self.state[aid] = entry
            self.state[aid].pop("pendingResult", None)
            self.pending_store.promote(aid, self.store)
            accepted.append(aid)
        return accepted

    def _run_page(
        self,
        target: Target,
        url: str,
        depth: int = 0,
        discovered_from: Optional[str] = None,
        discovery_evidence: Optional[str] = None,
    ) -> PageOutcome:
        page = PageOutcome(
            url=url,
            depth=depth,
            discovered_from=discovered_from,
            discovery_evidence=discovery_evidence,
        )

        if target.fetch_type == FETCH_BROWSER:
            result = self._fetch_via_browser(target, url)
        else:
            result = self.fetcher.fetch(url)

        page.fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
        page.final_url = result.final_url or url
        page.elapsed_ms = result.elapsed_ms
        page.status = result.status
        page.raw_chars = len(result.text)
        if not result.ok:
            page.error = result.error or f"http_{result.status}"
            return page

        text, blocks = normmod.normalize_html(result.text, base_url=result.final_url or url)
        page.page_title = _html_title(result.text)
        page.headings = _headings(blocks)
        page.text_chars = len(text)
        page.js_suspect = len(text) < JS_SUSPECT_CHARS
        page.normalized_text = text
        page.links = [link for block in blocks for link in block.links]
        page.link_contexts = [
            {
                "label": label,
                "url": link_url,
                "blockText": block.text,
                "blockPath": block.path,
            }
            for block in blocks for label, link_url in block.links
        ]
        page.artist_scoped = _is_artist_scoped_page(target, url)
        if depth == 0 and target.live_url \
                and url == refresh_month_query(target.live_url, self.today):
            page.category_warning = _category_mismatch(page)

        previous = self.store.load(target.artist_id, url)
        d = diffmod.compare(text, previous)

        if previous is None:
            # スナップショットを失っても、state のハッシュが一致すれば変化なしと判定する。
            # cache/normalized/ を消しただけで全組がAI行きになるのは割に合わない
            known_hash = (
                self.state.get(target.artist_id, {}).get("urls", {}).get(url, {}).get("contentHash")
            )
            if known_hash and known_hash == d.content_hash:
                d = diffmod.DiffResult(status=diffmod.NO_CHANGE, content_hash=d.content_hash)

        if d.status == diffmod.CHANGED and not _meaningful(d):
            # トップページに毎回変わる要素があるサイトは、静止していてもハッシュが動く。
            # Mrs. GREEN APPLE のトップは3回取得して3回とも別ハッシュだった。
            # 日付にも抽選にも触れない差分は、更新ではなく揺れとして扱う
            d = diffmod.DiffResult(status=diffmod.VOLATILE, content_hash=d.content_hash)

        page.diff_status = d.status
        page.content_hash = d.content_hash

        if d.status in (diffmod.NO_CHANGE, diffmod.VOLATILE):
            return page

        page.added_lines = d.added
        for line in d.added:
            if (extractmod.find_dates(line) or extractmod.LOTTERY_KEYWORDS.search(line)
                    or FOLLOW_RELEVANCE.search(line)):
                page.live_related_added.append(line)
            else:
                page.ignored_added.append(line)

        extracted = extractmod.extract(result.text, blocks, url, today=self.today)
        for ev in extracted.events:
            ev.venue = self.venue_master.canonical(ev.venue)
            ev.prefecture = ev.prefecture or self.venue_master.prefecture(ev.venue)
        page.events = extracted.events
        page.unresolved_dates = extracted.date_lines_without_venue[:10]
        # 抽選テキストは**変化した行に載っているものだけ**をAI候補にする
        added_join = "\n".join(d.added)
        page.lottery_blocks = [
            b for b in extracted.lottery_blocks
            if any(line and line in b for line in d.added[:400]) or b in added_join
        ][:MAX_AI_BLOCKS]
        return page

    def _fetch_via_browser(self, target: Target, url: str) -> FetchResult:
        """ヘッドレスブラウザは設定されている場合のみ使う（既定では使わない）。

        config の browserCommand に `{url}` を含むコマンドを書くと、その標準出力を
        レンダリング済みHTMLとして扱う。未設定なら未対応として返す。
        """
        command = (target.note or "")
        if not command.startswith("browserCommand:"):
            return FetchResult(url=url, error="browser_not_configured")
        cmd = command[len("browserCommand:"):].strip().replace("{url}", url)
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=90)
            if proc.returncode != 0:
                return FetchResult(url=url, error=f"browser_exit_{proc.returncode}")
            return FetchResult(url=url, final_url=url, status=200,
                               text=proc.stdout.decode("utf-8", errors="replace"))
        except Exception as e:
            return FetchResult(url=url, error=f"browser_{type(e).__name__}")

    def _ai_reason(self, outcome: ArtistOutcome) -> Optional[str]:
        """AIに回す理由を決める。無ければ None（＝AIを呼ばない）。"""
        has_lottery = any(
            _page_text_relevant(outcome, page, block)
            for page in outcome.pages for block in page.lottery_blocks
        )
        has_unresolved = any(
            _page_text_relevant(outcome, page, line)
            for page in outcome.pages for line in page.unresolved_dates
        )
        js_only = all(p.js_suspect for p in outcome.pages if not p.error)
        has_new_or_updated = any(
            status.status in (mergemod.NEW, mergemod.UPDATED)
            for status in outcome.statuses
        )

        if has_lottery:
            return "LOTTERY_TEXT"
        if has_new_or_updated:
            return "NEW_OR_UPDATED_EVENTS"
        if not outcome.parser_ok and has_unresolved:
            return "PARSER_FAILED"
        if not outcome.parser_ok and js_only:
            return "JS_RENDERED"
        return None

    def _load_artist_data(self, artist_id: str) -> Dict:
        path = self.artist_dir / f"{artist_id}.json"
        if not path.exists():
            return {"artistId": artist_id, "tours": [], "performances": [], "lotteries": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"artistId": artist_id, "tours": [], "performances": [], "lotteries": []}


def _meaningful(d: diffmod.DiffResult) -> bool:
    """差分に日付か抽選の記述が含まれるか。

    このアプリが見たいのは公演日と申込期限だけなので、
    それに触れない差分は「サイトが更新された」と数えない。
    """
    for line in d.added + d.removed:
        if (extractmod.find_dates(line) or extractmod.LOTTERY_KEYWORDS.search(line)
                or FOLLOW_RELEVANCE.search(line)):
            return True
    return False


def _html_title(html: str) -> Optional[str]:
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.I | re.S)
    if not match:
        return None
    value = normmod.clean_text(unescape(re.sub(r"<[^>]+>", " ", match.group(1))))
    return value[:500] if value else None


def _headings(blocks: List[normmod.Block]) -> List[str]:
    values: List[str] = []
    for block in blocks:
        if block.path not in {"h1", "h2", "h3", "h4"}:
            continue
        value = normmod.clean_text(block.text).replace("\n", " ")
        if value and value not in values:
            values.append(value[:500])
        if len(values) >= 20:
            break
    return values


def _host_key(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def _category_mismatch(page: PageOutcome) -> Optional[str]:
    header = " ".join(
        [page.page_title or "", *page.headings[:5], *page.normalized_text.splitlines()[:8]]
    )
    positive = re.search(
        r"\b(?:live|event|tour|concert|schedule)\b|ライブ|イベント|ツアー|公演",
        header, re.I,
    )
    negative = re.search(
        r"\b(?:media|profile|goods|discography|biography)\b|"
        r"メディア|プロフィール|グッズ|ディスコグラフィ|バイオグラフィ",
        header, re.I,
    )
    if negative and not positive:
        return "TARGET_CATEGORY_MISMATCH: liveUrlの見出しがLIVE/EVENTではありません"
    return None


def _identity_tokens(artist_id: str, artist_name: str, aliases: List[str]) -> List[str]:
    values = [artist_id, artist_name, *aliases]
    tokens = []
    for value in values:
        token = re.sub(r"[^a-z0-9一-龥ぁ-んァ-ヶ]", "", value.lower())
        if len(token) >= 2 and token not in tokens:
            tokens.append(token)
    return tokens


def _is_artist_scoped_page(target: Target, url: str) -> bool:
    official = urlparse(target.official_url)
    page = urlparse(url)
    if _host_key(target.official_url) != _host_key(url):
        return False
    identity = _identity_tokens(target.artist_id, target.artist_name, target.aliases)
    segments = [segment.lower() for segment in official.path.split("/") if segment]
    if segments:
        first_key = re.sub(r"[^a-z0-9]", "", segments[0])
        ascii_identity = {
            re.sub(r"[^a-z0-9]", "", token) for token in identity
            if re.search(r"[a-z0-9]", token)
        }
        if first_key and first_key in ascii_identity:
            return page.path.lower().startswith("/" + segments[0] + "/") \
                or page.path.lower() == "/" + segments[0]
    # 独自公式ホストは、そのホスト全体を対象アーティスト領域とみなす。
    return True


def _mentions_artist(outcome: ArtistOutcome, text: str) -> bool:
    compact = re.sub(
        r"[^a-z0-9一-龥ぁ-んァ-ヶ]", "", normmod.clean_text(text).lower()
    )
    return any(
        token in compact
        for token in _identity_tokens(
            outcome.artist_id, outcome.artist_name, outcome.artist_aliases
        )
    )


def _page_text_relevant(
    outcome: ArtistOutcome, page: PageOutcome, text: str
) -> bool:
    return page.artist_scoped or _mentions_artist(outcome, text)


def _title_terms(text: str) -> set:
    value = normmod.clean_text(text).lower()
    value = re.sub(r"20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?", " ", value)
    value = re.sub(r"\d{1,2}[月./-]\d{1,2}日?", " ", value)
    value = re.sub(
        r"出演決定|開催決定|詳細はこちら|お知らせ|更新|new|"
        r"[「」『』【】\[\]()（）!！?？:：/／]", " ", value, flags=re.I,
    )
    return {
        token for token in re.findall(r"[a-z0-9]{2,}|[一-龥ぁ-んァ-ヶー]{2,}", value)
        if token not in {"live", "tour", "event", "ticket", "ライブ", "ツアー", "イベント"}
    }


def _rank_follow_links(page: PageOutcome) -> List[Tuple[str, str]]:
    """変更DOMブロックと記事タイトルの近さを優先して公式リンクを返す。"""
    best_by_url: Dict[str, Tuple[int, int, str, str]] = {}
    changed_lines = [line.strip() for line in page.added_lines if line.strip()]
    contexts = page.link_contexts or [
        {"label": label, "url": url, "blockText": label, "blockPath": ""}
        for label, url in page.links
    ]
    for context in contexts:
        label, url = context.get("label", ""), context.get("url", "")
        block_text = context.get("blockText", "")
        haystack = f"{label} {url}"
        if FOLLOW_EXCLUDE.search(haystack) or FOLLOW_MEDIA_EXCLUDE.search(haystack):
            continue
        path = urlparse(url).path
        score = 0
        for line in changed_lines:
            if len(line) < 4 or FOLLOW_MEDIA_EXCLUDE.search(line):
                continue
            if label and (line in label or label in line):
                score = max(score, 180)
            if line in block_text or block_text in line:
                score = max(score, 160)
            common = _title_terms(line) & _title_terms(f"{label} {block_text}")
            if common:
                score = max(score, 60 + min(80, 25 * len(common)))
        if re.search(r"/(?:news|info|topics?)/(?:detail/)?[^/]+", path, re.I):
            score += 45
        # 初回取得では一覧全件がaddedになり、新旧すべての記事が
        # タイトル完全一致で同点になる。実日程へ迎りやすい一次告知を
        # チケットや周辺案内より優先する。文言のみを使いURLは生成しない。
        if re.search(r"開催決定|追加公演", block_text, re.I):
            score += 100
        elif re.search(r"出演決定|出演が決定", block_text, re.I):
            score += 70
        elif re.search(r"抽選|先行|受付|チケット", block_text, re.I):
            score += 50
        if FOLLOW_RELEVANCE.search(haystack):
            score += 20
        elif FOLLOW_STRONG.search(block_text):
            score += 10
        if not score:
            continue
        # CHANGEDページでは今回の差分と結び付かない過去記事を辿らない。
        if page.diff_status == diffmod.CHANGED and changed_lines and score < 60:
            continue
        current = best_by_url.get(url)
        published = _publication_ordinal(f"{label} {block_text}")
        value = (score, published, label, url)
        if current is None or value[0] > current[0]:
            best_by_url[url] = value
    ranked = sorted(best_by_url.values(), key=lambda value: (-value[0], -value[1], value[3]))
    return [(label, url) for _, _, label, url in ranked]


def _publication_ordinal(text: str) -> int:
    """一覧に明記された公開日だけを新しさの同点解消に使う。"""
    match = re.search(r"(20\d{2})[./年](\d{1,2})[./月](\d{1,2})", text)
    if not match:
        return 0
    try:
        return _date(int(match.group(1)), int(match.group(2)), int(match.group(3))).toordinal()
    except ValueError:
        return 0


def _dedupe_events(events: List[extractmod.ExtractedEvent]) -> List[extractmod.ExtractedEvent]:
    best: Dict = {}
    for ev in events:
        key = ev.key()
        if key not in best or ev.confidence > best[key].confidence:
            best[key] = ev
    return sorted(best.values(), key=lambda e: (e.date, e.venue))


# ---------------------------------------------------------------- AIキュー

def build_queue_item(outcome: ArtistOutcome, artist_data: Dict) -> Dict:
    """AIへ渡す最小限の材料を組み立てる。

    渡すのは「アーティスト名・変わった部分・機械抽出済みの公演・既存の要約」だけ。
    ページ全文も既存の全ライブ情報も渡さない。
    """
    lottery_blocks: List[str] = []
    unresolved: List[str] = []
    sources: List[Dict] = []
    evidence_blocks: List[Dict] = []
    lottery_sources: Dict[str, str] = {}

    for page in outcome.pages:
        if page.error or page.diff_status == diffmod.NO_CHANGE:
            continue
        page_actionable = bool(
            page.events or page.lottery_blocks or page.unresolved_dates
        )
        sources.append({
            "url": page.url,
            "diff": page.diff_status,
            "depth": page.depth,
            "discoveredFrom": page.discovered_from,
        })
        if (page_actionable and page.discovery_evidence and page.discovered_from
                and _page_text_relevant(outcome, page, page.discovery_evidence)):
            _append_evidence(
                evidence_blocks, page.discovered_from, page.discovery_evidence
            )
        if (page.depth > 0 and page_actionable and not page.events
                and page.normalized_text
                and _page_text_relevant(outcome, page, page.normalized_text)):
            # 詳細ページ冒頭の種別・期間・タイトル。公演行とは別にtour候補の
            # 根拠になる。最大3行だけで本文全量は送らない。
            header = " ".join(page.normalized_text.splitlines()[:3])
            _append_evidence(evidence_blocks, page.url, header)
        if page.depth == 0:
            added_evidence_count = 0
            ordered_added = sorted(
                enumerate(page.added_lines),
                key=lambda value: (
                    0 if EVIDENCE_PRIORITY.search(value[1]) else 1,
                    value[0],
                ),
            )
            for _, line in ordered_added:
                if (FOLLOW_RELEVANCE.search(line)
                        or extractmod.LOTTERY_KEYWORDS.search(line)) \
                        and _page_text_relevant(outcome, page, line) \
                        and not FOLLOW_MEDIA_EXCLUDE.search(line):
                    _append_evidence(evidence_blocks, page.url, line)
                    added_evidence_count += 1
                    if added_evidence_count >= 10:
                        break
        for b in page.lottery_blocks:
            # 詳細追跡先で公演が1件も確認できない場合、受付日だけを別tourへ
            # 誤帰属しない。根拠不足として安全棄却する。
            if page.depth > 0 and not page.events:
                continue
            if not _page_text_relevant(outcome, page, b):
                continue
            if b not in lottery_blocks:
                lottery_blocks.append(b)
                lottery_sources[b] = page.url
        if outcome.ai_reason == "PARSER_FAILED":
            for u in page.unresolved_dates:
                if not _page_text_relevant(outcome, page, u):
                    continue
                if u not in unresolved:
                    unresolved.append(u)
                    _append_evidence(evidence_blocks, page.url, u)

    lottery_blocks = _trim(lottery_blocks, MAX_AI_CHARS_PER_ARTIST * 2 // 3)
    unresolved = _trim(unresolved, MAX_AI_CHARS_PER_ARTIST // 3)
    for block in lottery_blocks:
        _append_evidence(evidence_blocks, lottery_sources.get(block, ""), block)

    # ページごとには上限を掛けていても、複数URL・追跡ページを合算すると
    # 後段validatorの12,000文字上限を越え得る。優先順を保ったまま全体も
    # MAX_AI_BLOCKSへ制限し、LLM実行前にqueue全体が拒否されるのを防ぐ。
    evidence_blocks = evidence_blocks[:MAX_AI_BLOCKS]

    status_by_key = {
        status.event.key(): status
        for status in outcome.statuses
        if status.event is not None
    }
    parsed_events = []
    seen_event_keys = set()
    for page in outcome.pages:
        for event in page.events:
            if not _page_text_relevant(outcome, page, event.source_text):
                continue
            status = status_by_key.get(event.key())
            if status is None or status.status not in (
                mergemod.NEW, mergemod.UPDATED, mergemod.UNCHANGED
            ):
                continue
            if event.key() in seen_event_keys:
                continue
            seen_event_keys.add(event.key())
            parsed_events.append({
                "status": status.status,
                "existingId": status.existing_id,
                "date": event.date,
                "venue": event.venue,
                "startTime": event.start_time,
                "openTime": event.open_time,
                "title": event.title,
                "eventKind": "fes" if event.external_appearance else None,
                "sourceUrl": event.source_url or page.url,
                "evidence": event.source_text,
            })
            if len(parsed_events) >= MAX_PARSED_EVENTS_IN_QUEUE:
                break
        if len(parsed_events) >= MAX_PARSED_EVENTS_IN_QUEUE:
            break

    existing_summary = {
        "tours": [
            {"id": t.get("id"), "title": t.get("title")}
            for t in (artist_data.get("tours") or [])
        ][:12],
        "lotteryIds": [l.get("id") for l in (artist_data.get("lotteries") or [])][:30],
        "knownPerformanceKeys": [
            f"{mergemod._date_of(p)}|{p.get('venue')}"
            for p in (artist_data.get("performances") or [])
        ][:40],
    }

    evidence_source_urls = {
        block["sourceUrl"] for block in evidence_blocks
    } | {
        event["sourceUrl"] for event in parsed_events
    }
    sources = [source for source in sources if source["url"] in evidence_source_urls]

    item = {
        "artistId": outcome.artist_id,
        "artistName": outcome.artist_name,
        "reason": outcome.ai_reason,
        "sources": sources,
        # 新consumerは構造化済みparsedEvents/evidenceBlocksを使う。旧キーは
        # schema互換のため残すが、同じ文字列を二重送信しない。
        "parsedEventKeys": [],
        "parsedEvents": parsed_events,
        "evidenceBlocks": evidence_blocks,
        "existingSummary": existing_summary,
        "changedLotteryText": [],
        "unparsedDateLines": [],
        "linkAudit": {
            "relatedLinksFound": sum(len(page.relevant_links) for page in outcome.pages),
            "relatedLinksFollowed": sum(1 for page in outcome.pages if page.depth > 0),
            "detailFetchFailed": sum(
                1 for page in outcome.pages if page.depth > 0 and page.error
            ),
        },
    }
    # JSON構造分を含めた後段の安全上限(12,000文字)を確実に守る。
    # 機械抽出済みparsedEventsを優先し、重複しやすい低優先度の
    # evidenceBlocks末尾からだけ削る。
    while item["evidenceBlocks"] and len(json.dumps(item, ensure_ascii=False)) > 9000:
        item["evidenceBlocks"].pop()
    item["approxChars"] = len(json.dumps(item, ensure_ascii=False))
    item["estimatedInputTokens"] = estimate_tokens_from_chars(item["approxChars"])
    return item


def _append_evidence(items: List[Dict], source_url: str, text: str) -> None:
    cleaned = normmod.clean_text(text).replace("\n", " ").strip()
    if not cleaned or not source_url:
        return
    value = {"sourceUrl": source_url, "text": cleaned}
    if value not in items:
        items.append(value)


def _trim(lines: List[str], budget: int) -> List[str]:
    out, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return out


# ---------------------------------------------------------------- 実行

def run(artist_ids: Optional[List[str]] = None,
        limit: Optional[int] = None,
        update_cache: bool = True,
        fetcher: Optional[Fetcher] = None,
        today: Optional[_date] = None,
        verbose: bool = True,
        cache_root: Optional[Path] = None) -> Dict:
    """バッチ1回分を実行して統計を返す。1組の失敗で全体を止めない。"""
    targets = build_targets(artist_ids)
    if limit:
        targets = targets[:limit]

    root = Path(cache_root) if cache_root is not None else None
    pipeline = Pipeline(
        fetcher=fetcher,
        today=today,
        update_cache=update_cache,
        state_file=(root / "collect_state.json") if root else STATE_FILE,
        pending_state_file=(root / "collect_state.pending.json") if root else PENDING_STATE_FILE,
        snapshot_dir=(root / "normalized") if root else SNAPSHOT_DIR,
        pending_snapshot_dir=(root / "normalized_pending") if root else PENDING_SNAPSHOT_DIR,
    )
    started = time.time()

    queue_items: List[Dict] = []
    outcomes: List[ArtistOutcome] = []
    errors: List[Dict] = []

    for i, target in enumerate(targets, 1):
        try:
            outcome = pipeline.run_artist(target)
        except Exception as e:  # 1組の異常でバッチを止めない
            outcome = ArtistOutcome(artist_id=target.artist_id, artist_name=target.artist_name)
            outcome.errors.append(f"{type(e).__name__}: {e}")
        outcomes.append(outcome)

        if outcome.errors:
            errors.append({"artistId": outcome.artist_id, "errors": outcome.errors})

        if outcome.ai_reason:
            item = build_queue_item(outcome, pipeline._load_artist_data(target.artist_id))
            outcome.ai_chars = item["approxChars"]
            queue_items.append(item)

        if verbose:
            print(f"[{i}/{len(targets)}] " + " / ".join(outcome.log_lines()), flush=True)

    pipeline.save_state()

    metrics = _metrics(outcomes, queue_items, elapsed_sec=time.time() - started)

    if update_cache:
        queue_file = (root / "ai_queue.json") if root else AI_QUEUE_FILE
        metrics_file = (root / "collect_metrics.jsonl") if root else METRICS_FILE
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_text(json.dumps({
            "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "instructions": (
                "Web検索・Web巡回は禁止。ここにあるテキストだけを根拠にJSONへ変換する。"
                "根拠が無い日程は null にする。"
            ),
            "items": queue_items,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        metrics_file.parent.mkdir(parents=True, exist_ok=True)
        with metrics_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    else:
        queue_file = None

    return {
        "metrics": metrics,
        "errors": errors,
        "queue": queue_items,
        "outcomes": outcomes,
        "queuePath": queue_file,
    }


def _metrics(outcomes: List[ArtistOutcome], queue_items: List[Dict],
             elapsed_sec: float) -> Dict:
    changed = [o for o in outcomes if o.changed]
    ai_chars = sum(item["approxChars"] for item in queue_items)
    ai_tokens = sum(item["estimatedInputTokens"] for item in queue_items)

    # 旧方式の概算: 1組あたり ルール類(約68KB) + 平均5ページの本文取得。
    # 実測（2026-08, 40ページ）の平均本文4,000字を1ページあたりの取り込み量とする
    legacy_per_artist_chars = 68_000 + 5 * 4_000
    legacy_tokens = estimate_tokens_from_chars(legacy_per_artist_chars * len(changed))

    total_new = sum(mergemod.summarize(o.statuses)[mergemod.NEW] for o in outcomes)

    return {
        "runAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsedSec": round(elapsed_sec, 1),
        "artistsProcessed": len(outcomes),
        "fetchFailed": sum(1 for o in outcomes if not o.fetch_ok),
        "sitesChanged": len(changed),
        "noChange": sum(1 for o in outcomes if o.fetch_ok and not o.changed),
        "parserSuccess": sum(1 for o in outcomes if o.parser_ok),
        "aiFallbackCount": len(queue_items),
        "aiFallbackArtists": [item["artistId"] for item in queue_items],
        "aiInputChars": ai_chars,
        "aiInputTokensEstimated": ai_tokens,
        "aiOutputTokensEstimated": None,  # AI実行後に record_ai_usage で埋める
        "newEventCandidates": total_new,
        "aiFreeRatio": round(1 - len(queue_items) / len(outcomes), 3) if outcomes else None,
        "legacyTokensEstimated": legacy_tokens,
        "estimatedTokenReduction": (
            round(1 - ai_tokens / legacy_tokens, 3) if legacy_tokens else None
        ),
    }
