"""既存の配信データとの突き合わせ・重複判定・AI出力の検証。

**サイトから消えたことを理由に既存データを消さない。** 掲載期間が終わっただけの
ことが多く、ユーザーは終了済み公演の当落・入金状況を見返すため（§5.1）。
REMOVED は報告するだけにとどめる。
"""
import re
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional, Tuple

from .extract import ExtractedEvent, normalize_venue

NEW = "NEW"
UPDATED = "UPDATED"
UNCHANGED = "UNCHANGED"
REMOVED = "REMOVED"
# サイトには載っているが日付が過去のもの。アーカイブ欄を拾っただけのことが多い
PAST = "PAST"

_ISO_DATE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_URL_RE = re.compile(r"^https?://[^\s\"'<>]+$")

# タイトルの微修正で別イベント扱いにしないため、比較前に落とす飾り
_TITLE_NOISE = re.compile(
    r"[\s　]|[\[\]【】「」『』（）()\"'“”‘’~〜\-−–—_/／・,、.。!！?？]|"
    r"(?i:tour|live|ライブ|ツアー|公演|開催決定|追加公演)"
)


@dataclass
class EventStatus:
    status: str
    event: Optional[ExtractedEvent] = None
    existing_id: Optional[str] = None
    changes: Dict[str, Tuple] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "existingId": self.existing_id,
            "event": self.event.to_dict() if self.event else None,
            "changes": {k: list(v) for k, v in self.changes.items()},
        }


def _date_of(performance: Dict) -> str:
    at = str(performance.get("performanceAt") or "")
    m = _ISO_DATE.match(at)
    return m.group(0) if m else ""


def _time_of(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = re.search(r"T([01]\d|2[0-3]):([0-5]\d)", str(value))
    return f"{m.group(1)}:{m.group(2)}" if m else None


def existing_index(artist_data: Dict) -> Dict[Tuple[str, str], Dict]:
    """既存 performances を (日付, 正規化会場) で引ける形にする。"""
    index: Dict[Tuple[str, str], Dict] = {}
    for perf in artist_data.get("performances") or []:
        key = (_date_of(perf), normalize_venue(perf.get("venue", "")))
        if key[0]:
            index[key] = perf
    return index


def title_key(title: Optional[str]) -> str:
    """タイトル比較用のゆるいキー。表記の揺れで別物にしない。"""
    return _TITLE_NOISE.sub("", (title or "")).lower()


def diff_events(extracted: List[ExtractedEvent], artist_data: Dict,
                today: Optional[_date] = None,
                include_removed: bool = True) -> List[EventStatus]:
    """抽出結果と既存データを突き合わせて状態を判定する。"""
    today = today or _date.today()
    index = existing_index(artist_data)
    seen_keys = set()
    statuses: List[EventStatus] = []

    for ev in extracted:
        key = ev.key()
        if key in seen_keys:
            continue  # 同一公演の重複抽出
        seen_keys.add(key)

        existing = index.get(key)
        if existing is None:
            # 既存に無い過去公演は「新規」ではない。掲載済みアーカイブを拾った側とみなす
            status = PAST if ev.date < today.isoformat() else NEW
            statuses.append(EventStatus(status=status, event=ev))
            continue

        changes: Dict[str, Tuple] = {}
        old_open, old_start = _time_of(existing.get("doorOpenAt")), _time_of(existing.get("performanceAt"))
        if ev.open_time and old_open and ev.open_time != old_open:
            changes["openTime"] = (old_open, ev.open_time)
        if ev.start_time and old_start and ev.start_time != old_start:
            changes["startTime"] = (old_start, ev.start_time)
        if ev.venue and existing.get("venue") and ev.venue != existing["venue"]:
            if normalize_venue(ev.venue) == normalize_venue(existing["venue"]):
                pass  # 表記ゆれのみ。変更とみなさない
            else:
                changes["venue"] = (existing["venue"], ev.venue)

        statuses.append(EventStatus(
            status=UPDATED if changes else UNCHANGED,
            event=ev,
            existing_id=existing.get("id"),
            changes=changes,
        ))

    # サイト側に見当たらなかった未来公演。削除はせず報告だけする
    if include_removed:
        for key, perf in index.items():
            if key in seen_keys or key[0] < today.isoformat():
                continue
            statuses.append(EventStatus(status=REMOVED, existing_id=perf.get("id")))

    return statuses


def summarize(statuses: List[EventStatus]) -> Dict[str, int]:
    counts = {NEW: 0, UPDATED: 0, UNCHANGED: 0, REMOVED: 0, PAST: 0}
    for s in statuses:
        counts[s.status] = counts.get(s.status, 0) + 1
    return counts


# ---------------------------------------------------------------- AI出力の検証

@dataclass
class ValidationIssue:
    level: str      # error / warn
    code: str
    detail: str


def validate_ai_events(events: List[Dict], artist_name: str,
                       today: Optional[_date] = None,
                       allowed_hosts: Optional[List[str]] = None
                       ) -> Tuple[List[Dict], List[ValidationIssue]]:
    """AIが返したイベント配列を検証する。通ったものだけを返す。

    AIの出力をそのまま配信データに入れない。日付の創作・会場の取り違え・
    アーティスト違いは、このアプリでは直接ユーザーの損害になる。
    """
    today = today or _date.today()
    issues: List[ValidationIssue] = []
    accepted: List[Dict] = []
    seen: set = set()

    for i, ev in enumerate(events or []):
        label = f"events[{i}]"
        if not isinstance(ev, dict):
            issues.append(ValidationIssue("error", "NOT_OBJECT", f"{label} がオブジェクトではありません"))
            continue

        date_str = str(ev.get("date") or "").strip()
        if not _ISO_DATE.match(date_str) or not _valid_date(date_str):
            issues.append(ValidationIssue("error", "BAD_DATE", f"{label} date='{date_str}'"))
            continue

        venue = str(ev.get("venue") or "").strip()
        if not venue:
            issues.append(ValidationIssue("error", "EMPTY_VENUE", f"{label} venue が空です"))
            continue
        if len(venue) > 60:
            issues.append(ValidationIssue("error", "VENUE_TOO_LONG", f"{label} venue={venue[:30]}..."))
            continue

        title = ev.get("title")
        if title is not None:
            title = str(title).strip()
            if len(title) > 120 or "\n" in title:
                issues.append(ValidationIssue("error", "BAD_TITLE", f"{label} title が文章化しています"))
                continue
            if _looks_like_prose(title):
                issues.append(ValidationIssue("warn", "PROSE_TITLE", f"{label} title='{title[:40]}'"))

        for field_name in ("openTime", "startTime"):
            value = ev.get(field_name)
            if value not in (None, "") and not _TIME_RE.match(str(value)):
                issues.append(ValidationIssue("error", "BAD_TIME", f"{label} {field_name}='{value}'"))
                ev[field_name] = None

        url = ev.get("detailUrl")
        if url not in (None, ""):
            if not _URL_RE.match(str(url)):
                issues.append(ValidationIssue("warn", "BAD_URL", f"{label} detailUrl='{url}'"))
                ev["detailUrl"] = None
            elif allowed_hosts and not _host_allowed(str(url), allowed_hosts):
                issues.append(ValidationIssue("warn", "FOREIGN_URL", f"{label} detailUrl='{url}'"))

        if date_str < today.isoformat():
            issues.append(ValidationIssue("warn", "PAST_EVENT", f"{label} {date_str} は過去日です"))

        artist_field = ev.get("artistName")
        if artist_field and title_key(str(artist_field)) != title_key(artist_name):
            issues.append(ValidationIssue(
                "error", "ARTIST_MISMATCH",
                f"{label} artistName='{artist_field}' が対象 '{artist_name}' と異なります"))
            continue

        key = (date_str, normalize_venue(venue))
        if key in seen:
            issues.append(ValidationIssue("warn", "DUPLICATE", f"{label} {date_str} {venue}"))
            continue
        seen.add(key)
        accepted.append(ev)

    return accepted, issues


def _valid_date(date_str: str) -> bool:
    try:
        _date.fromisoformat(date_str[:10])
        return True
    except ValueError:
        return False


def _looks_like_prose(title: str) -> bool:
    return bool(re.search(r"(です|ます|ください|いたします|となります|について)", title))


def _host_allowed(url: str, allowed_hosts: List[str]) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)
