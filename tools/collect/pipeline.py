"""収集バッチ本体。

  取得 → 正規化 → 差分 → 機械解析 → 既存突き合わせ → （必要な分だけ）AIキュー

AIをこの中から呼ぶことはない。**AIに渡す材料をファイルに書き出すだけ**で、
実際の構造化は Claude Code 側が cache/ai_queue.json を読んで行う
（親CLAUDE.md §4 のコスト制約に従い、従量課金APIは使わない）。
"""
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import diff as diffmod
from . import extract as extractmod
from . import merge as mergemod
from . import normalize as normmod
from .fetcher import Fetcher, FetchResult
from .targets import FETCH_BROWSER, FETCH_SKIP, Target, build_targets

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
MAX_AI_CHARS_PER_ARTIST = 6000
MAX_AI_BLOCKS = 25
# 機械抽出済みの公演は参考として載せるだけ。全量を積むと本末転倒になる
MAX_PARSED_EVENTS_IN_QUEUE = 30

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
        self.state: Dict = _load_json_object(self.state_file)
        self.pending: Dict = _load_json_object(self.pending_state_file)

    def save_state(self) -> None:
        if not self.update_cache:
            return
        _write_json_object(self.state_file, self.state)
        _write_json_object(self.pending_state_file, self.pending)

    # -- 1アーティスト ------------------------------------------------

    def run_artist(self, target: Target) -> ArtistOutcome:
        outcome = ArtistOutcome(artist_id=target.artist_id, artist_name=target.artist_name)

        if target.fetch_type == FETCH_SKIP:
            outcome.errors.append("skip指定のため取得しません")
            return outcome

        urls = target.urls()
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
                if page.diff_status not in (diffmod.NO_CHANGE, diffmod.VOLATILE):
                    outcome.changed = True

            url_records[url] = {
                "lastFetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "httpStatus": page.status,
                "contentHash": page.content_hash,
                "normalizedChars": page.text_chars,
                "diffStatus": page.diff_status,
                "jsSuspect": page.js_suspect,
                "error": page.error,
            }

        if not outcome.fetch_ok:
            self._record(target.artist_id, url_records, outcome, "FETCH_FAILED", confirmed=False)
            return outcome

        if not outcome.changed:
            self._record(target.artist_id, url_records, outcome, diffmod.NO_CHANGE, confirmed=True)
            return outcome

        events: List[extractmod.ExtractedEvent] = []
        for page in outcome.pages:
            events.extend(page.events)
        events = _dedupe_events(events)

        outcome.parser_ok = any(e.confidence >= 0.6 for e in events)
        outcome.statuses = mergemod.diff_events(events, artist_data, today=self.today)
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

    def _run_page(self, target: Target, url: str) -> PageOutcome:
        page = PageOutcome(url=url)

        if target.fetch_type == FETCH_BROWSER:
            result = self._fetch_via_browser(target, url)
        else:
            result = self.fetcher.fetch(url)

        page.status = result.status
        page.raw_chars = len(result.text)
        if not result.ok:
            page.error = result.error or f"http_{result.status}"
            return page

        text, blocks = normmod.normalize_html(result.text, base_url=result.final_url or url)
        page.text_chars = len(text)
        page.js_suspect = len(text) < JS_SUSPECT_CHARS
        page.normalized_text = text

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

        extracted = extractmod.extract(result.text, blocks, url, today=self.today)
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
        has_lottery = any(p.lottery_blocks for p in outcome.pages)
        has_unresolved = any(p.unresolved_dates for p in outcome.pages)
        js_only = all(p.js_suspect for p in outcome.pages if not p.error)

        if has_lottery:
            return "LOTTERY_TEXT"
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
        if extractmod.find_dates(line) or extractmod.LOTTERY_KEYWORDS.search(line):
            return True
    return False


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

    for page in outcome.pages:
        if page.error or page.diff_status == diffmod.NO_CHANGE:
            continue
        sources.append({"url": page.url, "diff": page.diff_status})
        for b in page.lottery_blocks:
            if b not in lottery_blocks:
                lottery_blocks.append(b)
        for u in page.unresolved_dates:
            if u not in unresolved:
                unresolved.append(u)

    lottery_blocks = _trim(lottery_blocks, MAX_AI_CHARS_PER_ARTIST * 2 // 3)
    unresolved = _trim(unresolved, MAX_AI_CHARS_PER_ARTIST // 3)

    # 機械抽出済みの公演はAIに再解釈させないので、対応付けに要る最小の形だけ渡す。
    # 完全な内容はレポート（artists[].newEvents）にあり、そちらが反映元になる
    parsed = []
    for s in outcome.statuses:
        if s.status not in (mergemod.NEW, mergemod.UPDATED) or not s.event:
            continue
        parsed.append(f"{s.event.date}|{s.event.venue}|{s.event.title or ''}")
        if len(parsed) >= MAX_PARSED_EVENTS_IN_QUEUE:
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

    item = {
        "artistId": outcome.artist_id,
        "artistName": outcome.artist_name,
        "reason": outcome.ai_reason,
        "sources": sources,
        "parsedEventKeys": parsed,
        "existingSummary": existing_summary,
        "changedLotteryText": lottery_blocks,
        "unparsedDateLines": unresolved,
    }
    item["approxChars"] = len(json.dumps(item, ensure_ascii=False))
    item["estimatedInputTokens"] = estimate_tokens_from_chars(item["approxChars"])
    return item


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
        verbose: bool = True) -> Dict:
    """バッチ1回分を実行して統計を返す。1組の失敗で全体を止めない。"""
    targets = build_targets(artist_ids)
    if limit:
        targets = targets[:limit]

    pipeline = Pipeline(fetcher=fetcher, today=today, update_cache=update_cache)
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
        AI_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        AI_QUEUE_FILE.write_text(json.dumps({
            "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "instructions": (
                "Web検索・Web巡回は禁止。ここにあるテキストだけを根拠にJSONへ変換する。"
                "根拠が無い日程は null にする。"
            ),
            "items": queue_items,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")

    return {"metrics": metrics, "errors": errors, "queue": queue_items, "outcomes": outcomes}


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
