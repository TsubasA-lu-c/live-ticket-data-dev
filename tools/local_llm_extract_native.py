#!/usr/bin/env python3
"""
Ollama native API を使う「事実抽出専用」ローカルLLMランナー。

目的:
- cache/ai_queue.json の evidenceBlocks / changedLotteryText / unparsedDateLines だけを根拠にする
- LLMには新規/既存/重複/add/update判定をさせない
- 本番 data/ は一切変更しない
- GPT-OSS 20B の native /api/chat + think=low に対応
- Qwen系も利用可能
- 抽出結果は local_llm/ 配下だけに保存

Python 3.9+ / 標準ライブラリのみ。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_QUEUE = Path("cache/ai_queue.json")
DEFAULT_OUT_ROOT = Path("local_llm/extract_native")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


SYSTEM_PROMPT = r"""
あなたはライブ・イベント情報の「事実抽出器」です。
判断・補完・推測・新規判定・重複判定は禁止です。

入力に含まれる evidenceBlocks / changedLotteryText / unparsedDateLines だけを事実根拠として扱ってください。
parsedEvents や existingSummary は入力しません。機械推定や既存データに引っ張られないためです。

重要ルール:
1. 書かれていない日付、時刻、会場を推測しない。
2. イベント全体の開催日と、対象アーティスト/メンバーの出演日を混同しない。
3. 「イベントは2日開催、本人は1日だけ出演」の場合、本人出演が明示された日だけ event にする。
4. 対象アーティスト/メンバーと日付の関連が明示されない場合は events にせず uncertain に入れる。
5. 同日2公演など複数の開演時刻が明示されている場合は、原則として別 event に分ける。
6. 配信視聴チケット、映像配信開始日は、実ライブ公演として events にしない。
7. ticketWindows は「抽選」「先行」「受付」「一般発売」「当日券」等の販売/受付情報が明示されている場合だけ抽出する。
8. 根拠のない lottery / ticket window を作らない。
9. sourceUrl は入力 evidence の sourceUrl から選ぶ。
10. evidence と relationEvidence は入力中の文章をそのまま短く抜き出す。創作しない。
11. relationEvidence は、その日付と対象アーティスト/メンバーの関係が分かる「最小の文または節」にする。
12. relationEvidence が複数日を含み、どの日に本人が出演するか曖昧なら event にせず uncertain にする。
13. evidenceBlocks に kind="detail_enriched" の詳細本文がある場合、一覧・見出しより必ず詳細本文を優先する。
14. sourceUrl も、根拠となる日付・会場・時刻が実際に載っている詳細本文のURLを優先する。
15. グループの公式サイトでは、メンバー個人のライブ/イベントも、メンバー名と出演・開催が明示されていれば抽出対象とする。
16. グッズ販売、会場物販、整理券、受注販売、商品予約、通販の日時をライブ公演日時やticketWindowとして扱わない。
17. ニュース掲載日や「企画決定」の日付をライブ公演日として扱わない。
18. event/ticketWindow が対象グループではなくメンバー個人の活動なら、subjectName に本文で明示されたメンバー名を入れる。対象グループ本体または明示がない場合は null。subjectName は推測しない。
19. 「販売期間」だけではticketWindowにしない。チケット、先行、抽選、受付、一般発売、当日券、追加席販売など、入場券の販売だと分かる文脈が必要。
20. 入力に focusSubject がある場合、そのメンバーについて明示されたライブ・イベント・入場券受付を漏らさず抽出する。親グループ本体の情報へ読み替えない。
21. focusTask="events_only" の場合は events だけを抽出し、ticketWindows は必ず空配列にする。
22. focusTask="tickets_only" の場合は ticketWindows だけを抽出し、events は必ず空配列にする。
23. JSON以外は出力しない。

出力形式:
{
  "artistId": "string",
  "events": [
    {
      "title": "string or null",
      "subjectName": "string or null",
      "date": "YYYY-MM-DD",
      "venue": "string or null",
      "openTime": "HH:MM or null",
      "startTime": "HH:MM or null",
      "sourceUrl": "string",
      "relationEvidence": "対象アーティスト/メンバーとこの日付を直接結びつける最小の根拠文",
      "evidence": "日付・会場・時刻等の根拠文"
    }
  ],
  "ticketWindows": [
    {
      "name": "string or null",
      "subjectName": "string or null",
      "startAt": "YYYY-MM-DDTHH:MM or YYYY-MM-DD or null",
      "endAt": "YYYY-MM-DDTHH:MM or YYYY-MM-DD or null",
      "sourceUrl": "string",
      "evidence": "string"
    }
  ],
  "uncertain": [
    {
      "text": "string",
      "reason": "string",
      "sourceUrl": "string or null"
    }
  ]
}
""".strip()


def _native_base_url() -> str:
    explicit = os.getenv("LOCAL_OLLAMA_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    base = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def _default_model() -> str:
    return os.getenv("LOCAL_LLM_MODEL", "gpt-oss:20b")


def _think_value(model: str, requested: str):
    if requested != "auto":
        if requested == "false":
            return False
        if requested == "true":
            return True
        return requested
    if model.lower().startswith("gpt-oss"):
        return "low"
    return False


def _load_queue(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("AI queue形式が不正です: items 配列がありません")
    return data


def _material(item: Dict[str, Any]) -> Dict[str, Any]:
    # LLMに既存データや機械判定は渡さない。
    return {
        "artistId": item.get("artistId"),
        "artistName": item.get("artistName"),
        "reason": item.get("reason"),
        "sources": item.get("sources", []),
        "evidenceBlocks": item.get("evidenceBlocks", []),
        "changedLotteryText": item.get("changedLotteryText", []),
        "unparsedDateLines": item.get("unparsedDateLines", []),
    }



def _split_long_block(block: Dict[str, Any], max_block_chars: int, overlap: int = 300) -> List[Dict[str, Any]]:
    text = str(block.get("text") or "")
    if len(text) <= max_block_chars:
        return [dict(block)]
    out = []
    step = max(1, max_block_chars - overlap)
    for idx, start in enumerate(range(0, len(text), step)):
        piece = text[start:start + max_block_chars]
        if not piece:
            break
        b = dict(block)
        b["text"] = piece
        b["segmentIndex"] = idx
        b["segmentStart"] = start
        out.append(b)
        if start + max_block_chars >= len(text):
            break
    return out


def _material_chunks(
    item: Dict[str, Any],
    max_evidence_chars: int,
    max_block_chars: int,
) -> List[Dict[str, Any]]:
    blocks = []
    for block in item.get("evidenceBlocks", []) or []:
        blocks.extend(_split_long_block(block, max_block_chars=max_block_chars))

    chunks: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    current_chars = 0

    def flush():
        nonlocal current, current_chars
        if not current:
            return
        chunks.append({
            "artistId": item.get("artistId"),
            "artistName": item.get("artistName"),
            "reason": item.get("reason"),
            "sources": item.get("sources", []),
            "evidenceBlocks": current,
            "changedLotteryText": [],
            "unparsedDateLines": [],
        })
        current = []
        current_chars = 0

    for block in blocks:
        block_chars = len(str(block.get("text") or ""))
        if current and current_chars + block_chars > max_evidence_chars:
            flush()
        current.append(block)
        current_chars += block_chars
    flush()

    aux_changed = item.get("changedLotteryText", []) or []
    aux_unparsed = item.get("unparsedDateLines", []) or []
    if aux_changed or aux_unparsed:
        chunks.append({
            "artistId": item.get("artistId"),
            "artistName": item.get("artistName"),
            "reason": item.get("reason"),
            "sources": item.get("sources", []),
            "evidenceBlocks": [],
            "changedLotteryText": aux_changed,
            "unparsedDateLines": aux_unparsed,
        })

    if not chunks:
        chunks = [_material(item)]
    return chunks


def _date_section_for_event(text: str, date: str, venue: Optional[str] = None, radius_before: int = 80, max_after: int = 1200) -> str:
    # 対象日付から「次の日付が始まる直前」までに限定する。
    # Billboardのように複数会場の日程が連続するページで隣日のstageを混ぜない。
    idx = -1
    matched = ""
    for variant in _date_variants(date):
        idx = text.find(variant)
        if idx >= 0:
            matched = variant
            break
    if idx < 0:
        return ""

    venue_idx = text.rfind(str(venue), 0, idx) if venue else -1
    start = venue_idx if venue_idx >= 0 else max(0, idx - radius_before)
    tail_start = idx + len(matched)
    tail = text[tail_start: min(len(text), tail_start + max_after)]
    next_date = re.search(
        r"(?:20\d{2}(?:年|[./-])\s*)?\d{1,2}(?:月|[./-])\s*\d{1,2}(?:日)?",
        tail,
    )
    if next_date:
        end = tail_start + next_date.start()
    else:
        end = min(len(text), tail_start + max_after)
    return text[start:end]


def _explicit_stage_entries_for_event(
    item: Dict[str, Any],
    ev: Dict[str, Any],
) -> List[tuple]:
    date = str(ev.get("date") or "")
    venue = ev.get("venue")
    source_url = ev.get("sourceUrl")
    if not date or not venue or not source_url:
        return []

    entries = []
    seen = set()
    for text in _source_texts(item, source_url):
        section = _date_section_for_event(text, date, venue=venue)
        if not section or not _scalar_semantic_present(venue, [section]):
            continue
        for m in re.finditer(
            r"(?:<|＜)?(?:1st|2nd|3rd|第[123一二三])\s*(?:stage|ステージ|部)?(?:>|＞)?"
            r".{0,80}?(?:Open|OPEN|開場)\s*[:：]?\s*(\d{1,2}:\d{2})"
            r".{0,40}?(?:Start|START|開演)\s*[:：]?\s*(\d{1,2}:\d{2})",
            section,
            re.I | re.S,
        ):
            open_time = m.group(1)
            start_time = m.group(2)
            key = (open_time, start_time)
            if key in seen:
                continue
            seen.add(key)
            snippet = re.sub(r"\s+", " ", m.group(0)).strip()
            entries.append((open_time, start_time, snippet))
    return entries


def _explicit_stage_pairs_for_event(
    item: Dict[str, Any],
    ev: Dict[str, Any],
) -> List[tuple]:
    return [
        (open_time, start_time)
        for open_time, start_time, _ in _explicit_stage_entries_for_event(item, ev)
    ]


def _expand_explicit_same_day_stages(
    item: Dict[str, Any],
    ev: Dict[str, Any],
) -> List[Dict[str, Any]]:
    entries = _explicit_stage_entries_for_event(item, ev)
    if len(entries) < 2:
        return [ev]

    out = []
    for open_time, start_time, snippet in entries:
        clone = dict(ev)
        clone["openTime"] = open_time
        clone["startTime"] = start_time
        clone["evidence"] = snippet
        clone["_expandedFromExplicitStages"] = True
        clone["_stageEvidenceReplaced"] = True
        out.append(clone)
    return out


def _event_cross_pass_key(ev: Dict[str, Any]) -> Optional[tuple]:
    if not ev.get("date") or not ev.get("venue") or not ev.get("sourceUrl"):
        return None
    return (
        ev.get("date"),
        _semantic_compact(ev.get("venue")),
        ev.get("startTime"),
        ev.get("sourceUrl"),
    )


def _event_specificity(ev: Dict[str, Any]) -> int:
    score = 0
    if ev.get("subjectName"):
        score += 50
    if ev.get("title"):
        score += min(40, len(_semantic_compact(ev.get("title"))))
    if ev.get("openTime"):
        score += 10
    if ev.get("startTime"):
        score += 10
    return score


def _merge_event_pair(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    preferred, other = (a, b)
    if _event_specificity(b) > _event_specificity(a):
        preferred, other = b, a
    out = dict(preferred)
    for key in ("title", "subjectName", "openTime", "startTime", "relationEvidence", "evidence"):
        if not out.get(key) and other.get(key):
            out[key] = other.get(key)
    if out.get("openTime") and out.get("startTime") and out.get("openTime") == out.get("startTime"):
        if not other.get("openTime"):
            out["openTime"] = None
    return out


def _dedupe_cross_pass_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    index_by_key = {}
    for ev in events:
        key = _event_cross_pass_key(ev)
        if key is None:
            out.append(ev)
            continue
        idx = index_by_key.get(key)
        if idx is None:
            index_by_key[key] = len(out)
            out.append(ev)
            continue
        existing = out[idx]
        if existing.get("_originPass") == ev.get("_originPass"):
            out.append(ev)
            continue
        out[idx] = _merge_event_pair(existing, ev)
    return out


def _event_key(ev: Dict[str, Any]) -> tuple:
    return (
        _compact(ev.get("title")),
        ev.get("date"),
        _compact(ev.get("venue")),
        ev.get("openTime"),
        ev.get("startTime"),
        ev.get("sourceUrl"),
    )


def _ticket_key(tw: Dict[str, Any]) -> tuple:
    return (
        _compact(tw.get("name")),
        tw.get("startAt"),
        tw.get("endAt"),
        tw.get("sourceUrl"),
    )



def _subject_norm(value: Optional[str]) -> str:
    return _compact(value).lower()


def _ticket_cross_pass_key(tw: Dict[str, Any]) -> Optional[tuple]:
    subject = _subject_norm(tw.get("subjectName"))
    if not subject:
        return None
    if not tw.get("startAt"):
        return None
    return (
        subject,
        tw.get("startAt"),
        tw.get("endAt"),
        tw.get("sourceUrl"),
    )


def _ticket_name_specificity(tw: Dict[str, Any]) -> int:
    name = str(tw.get("name") or "")
    score = len(_compact(name))
    if re.search(r"ファンクラブ|CÉLUXE|抽選|先行|Club\s*BBL|一般発売|機材席|ステージバック", name, re.I):
        score += 40
    if re.search(r"ticket\s*sale|pre.?sale|general\s*sale", name, re.I):
        score += 5
    if re.fullmatch(r"(一般発売|販売期間|受付期間|販売|受付)", name.strip(), re.I):
        score -= 20
    return score


def _dedupe_cross_pass_tickets(tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    index_by_key = {}

    for tw in tickets:
        key = _ticket_cross_pass_key(tw)
        if key is None:
            out.append(tw)
            continue

        existing_idx = index_by_key.get(key)
        if existing_idx is None:
            index_by_key[key] = len(out)
            out.append(tw)
            continue

        existing = out[existing_idx]
        if existing.get("_originPass") == tw.get("_originPass"):
            # 同じpass内の同時刻別受付は潰さない。
            out.append(tw)
            continue

        if _ticket_name_specificity(tw) > _ticket_name_specificity(existing):
            out[existing_idx] = tw

    return out


def _merge_valid_facts(parts: List[Dict[str, Any]], artist_id: str, artist_name: str) -> Dict[str, Any]:
    events = []
    tickets = []
    uncertain = []
    seen_e = set()
    seen_t = set()
    seen_u = set()

    for part in parts:
        for ev in part.get("events", []) or []:
            k = _event_key(ev)
            if k not in seen_e:
                seen_e.add(k)
                events.append(ev)

        for tw in part.get("ticketWindows", []) or []:
            k = _ticket_key(tw)
            if k not in seen_t:
                seen_t.add(k)
                tickets.append(tw)

        for u in part.get("uncertain", []) or []:
            k = (
                _compact(u.get("text")),
                _compact(u.get("reason")),
                u.get("sourceUrl"),
            )
            if k not in seen_u:
                seen_u.add(k)
                uncertain.append(u)

    events = _dedupe_cross_pass_events(events)
    tickets = _dedupe_cross_pass_tickets(tickets)

    return {
        "artistId": artist_id,
        "artistName": artist_name,
        "events": events,
        "ticketWindows": tickets,
        "uncertain": uncertain,
    }


def _allowed_urls(item: Dict[str, Any]) -> set:
    urls = set()
    for block in item.get("evidenceBlocks", []) or []:
        if block.get("sourceUrl"):
            urls.add(block["sourceUrl"])
    for src in item.get("sources", []) or []:
        if src.get("url"):
            urls.add(src["url"])
    return urls


def _evidence_texts(item: Dict[str, Any]) -> List[str]:
    out = []
    for block in item.get("evidenceBlocks", []) or []:
        if block.get("text"):
            out.append(str(block["text"]))
    for text in item.get("changedLotteryText", []) or []:
        if text:
            out.append(str(text))
    for text in item.get("unparsedDateLines", []) or []:
        if text:
            out.append(str(text))
    return out


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _date_variants(date_str: str) -> List[str]:
    try:
        y, m, d = [int(x) for x in date_str.split("-")]
    except Exception:
        return []
    return [
        f"{y:04d}-{m:02d}-{d:02d}",
        f"{y:04d}/{m:02d}/{d:02d}",
        f"{y:04d}/{m}/{d}",
        f"{y:04d}.{m:02d}.{d:02d}",
        f"{y:04d}.{m}.{d}",
        f"{y}年{m}月{d}日",
        f"{y}年{m}月{d:02d}日",
        f"{y}年{m:02d}月{d:02d}日",
        f"{y}年{m}.{d:02d}",
        f"{y}年{m}.{d}",
        f"{y}年{m:02d}.{d:02d}",
        f"{m}/{d}",
        f"{m:02d}/{d:02d}",
        f"{m}.{d}",
        f"{m:02d}.{d:02d}",
        f"{m}月{d}日",
        f"{m}月{d:02d}日",
    ]

def _date_mentioned(text: str, date_str: str) -> bool:
    try:
        y, m, d = [int(x) for x in date_str.split("-")]
    except Exception:
        return False

    c = _compact(text)
    if any(_compact(v) in c for v in _date_variants(date_str)):
        return True

    # 「2026年 日本武道館 9.08」のように年が見出し側、月日が行側に分離される表記。
    if re.search(rf"(?<!\d){y}(?:年|\b)", text):
        md_patterns = [
            rf"(?<![\d.])0?{m}[./-]0?{d}(?![\d.])",
            rf"(?<!\d)0?{m}月0?{d}日",
        ]
        if any(re.search(p, text) for p in md_patterns):
            return True
    return False


def _explicit_years(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"(?<!\d)(20\d{2})(?:年|[./-])", text or "")]


def _date_year_conflict(
    date_str: str,
    evidence: str,
    source_url: Optional[str],
    source_texts: List[str],
) -> bool:
    try:
        y, m, d = [int(x) for x in date_str.split("-")]
    except Exception:
        return False

    # evidence に年が明記されている場合は最優先。
    years = _explicit_years(evidence)
    if years and y not in years:
        return True

    # 古いtourページのfdate/ldateは強い根拠。例: fdate=2017-04-09
    url_years = [int(x) for x in re.findall(r"(?:fdate|ldate)=((?:19|20)\d{2})[-%]", source_url or "", re.I)]
    if url_years and y not in url_years:
        return True

    # 証拠が月日だけでも、source全体が単一年の古いページならその年を採用する。
    source_years = []
    for text in source_texts:
        source_years.extend(_explicit_years(text))
    unique = sorted(set(source_years))
    if len(unique) == 1 and unique[0] != y:
        md = [f"{m}/{d}", f"{m:02d}/{d:02d}", f"{m}.{d}", f"{m:02d}.{d:02d}", f"{m}月{d}日", f"{m}月{d:02d}日"]
        if any(v in evidence for v in md):
            return True
    return False


def _relation_date_backed_by_atomic_evidence(ev: Dict[str, Any]) -> bool:
    date = str(ev.get("date") or "")
    evidence = str(ev.get("evidence") or "")
    if not date or not _date_mentioned(evidence, date):
        return False

    venue = ev.get("venue")
    if venue and _scalar_semantic_present(venue, [evidence]):
        return True

    for key in ("openTime", "startTime"):
        value = ev.get(key)
        if value and _scalar_present(str(value), [evidence]):
            return True

    # タイトル自体がrelationEvidenceに明記され、日付はevidenceにある分離型。
    title = _semantic_compact(ev.get("title"))
    relation = _semantic_compact(ev.get("relationEvidence"))
    if title and relation and (title in relation or relation in title):
        return True
    return False


def _looks_like_promoter_not_venue(value: Optional[str]) -> bool:
    text = str(value or "")
    return bool(re.search(
        r"^(?:ディスクガレージ|DISK\\s*GARAGE|Livemasters(?:\s*Inc\.)?|"
        r"グリーンズ|YUMEBANCHI|キョードー(?:東京|東海|大阪)?|BEA|G/?I/?P)$",
        text.strip(),
        re.I,
    ))


def _recover_event_title(ev: Dict[str, Any]) -> Dict[str, Any]:
    ev = dict(ev)
    if ev.get("title"):
        title = str(ev["title"])
        text = " ".join(str(ev.get(k) or "") for k in ("relationEvidence", "evidence"))
        if "発売記念リリースイベント" in text and "発売記念リリースイベント" not in title:
            m = re.search(r"[「『](.{2,120}?)[」』]\s*発売記念リリースイベント", text)
            if m:
                ev["title"] = f"「{m.group(1)}」発売記念リリースイベント"
        return ev

    text = " ".join(str(ev.get(k) or "") for k in ("relationEvidence", "evidence"))
    quoted = re.findall(r"[「『](.{2,120}?)[」』]", text)
    for q in quoted:
        if re.search(r"LIVE|TOUR|ライブ|ツアー|FES|フェス|公演|Fan\s*Meeting|ファンミーティング", q, re.I):
            ev["title"] = q.strip()
            ev["_titleRecovered"] = True
            return ev

    m = re.search(r"[「『](.{2,120}?)[」』]\s*イベント(?:開催)?", text)
    if m:
        ev["title"] = f"「{m.group(1)}」イベント"
        ev["_titleRecovered"] = True
    return ev


def _ticket_is_non_admission_raffle(tw: Dict[str, Any], source_texts: List[str]) -> bool:
    direct = " ".join([str(tw.get("name") or ""), str(tw.get("evidence") or "")])
    context = direct + " " + _ticket_context(tw, source_texts)
    giveaway_hits = sum(bool(re.search(p, context, re.I)) for p in (
        r"宝くじ企画",
        r"賞品",
        r"グッズが当たる|オリジナルグッズが当たる",
        r"当選確率",
        r"抽選チケット発行(?:数)?",
        r"当選発表",
    ))
    admission = bool(re.search(r"入場|座席|公演チケット|ライブチケット|会場チケット|観覧チケット", context, re.I))
    return giveaway_hits >= 2 and not admission

def _count_calendar_mentions(text: str) -> int:
    pat = re.compile(
        r"\d{4}[./-]\d{1,2}[./-]\d{1,2}"
        r"|\d{4}年\d{1,2}月\d{1,2}日"
        r"|\d{4}年\d{1,2}\.\d{1,2}"
        r"|(?<![\d.])\d{1,2}[./-]\d{1,2}(?![\d.])"
        r"|\d{1,2}月\d{1,2}日"
        r"|(?<!\d)\d{1,2}日(?:\([^)]*\))?"
    )
    return len(list(pat.finditer(text or "")))

def _time_valid(value: Optional[str]) -> bool:
    if value is None:
        return True
    if not TIME_RE.match(str(value)):
        return False
    h, m = [int(x) for x in value.split(":")]
    return 0 <= h <= 23 and 0 <= m <= 59


def _parse_date(value: str) -> bool:
    if not DATE_RE.match(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False



def _canonical_source_key(url: Optional[str]) -> Optional[tuple]:
    if not url:
        return None
    try:
        p = urlsplit(str(url))
    except Exception:
        return None
    scheme = (p.scheme or "https").lower()
    host = (p.netloc or "").lower()
    path = re.sub(r"/+$", "", p.path or "/") or "/"
    if not host:
        return None
    return (scheme, host, path)


def _same_source_page(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return _canonical_source_key(a) == _canonical_source_key(b)


def _source_url_allowed(source_url: Optional[str], allowed_urls: set) -> bool:
    if not source_url:
        return False
    if source_url in allowed_urls:
        return True
    return any(_same_source_page(source_url, allowed) for allowed in allowed_urls)


def _source_texts(item: Dict[str, Any], source_url: Optional[str]) -> List[str]:
    out = []
    for block in item.get("evidenceBlocks", []) or []:
        if source_url and not _same_source_page(block.get("sourceUrl"), source_url):
            continue
        if block.get("text"):
            out.append(str(block["text"]))
    if not out:
        out = _evidence_texts(item)
    return out


def _text_present(candidate: str, texts: List[str]) -> bool:
    c = _compact(candidate)
    if not c:
        return False
    return any(c in _compact(t) for t in texts)


def _scalar_present(candidate: Optional[str], texts: List[str]) -> bool:
    if candidate in (None, ""):
        return True
    c = _compact(str(candidate))
    return any(c in _compact(t) for t in texts)



def _semantic_compact(value: Optional[str]) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[「」『』【】\[\]()（）<>＜＞・,:：;；~〜～'\"“”‘’]", "", s)
    # venueで混ざりやすい都道府県表記・接頭辞を比較用だけ軽く除去
    s = re.sub(r"(東京都|北海道|大阪府|京都府|神奈川県|千葉県|埼玉県|兵庫県|愛知県)", "", s)
    return s


def _scalar_semantic_present(candidate: Optional[str], texts: List[str]) -> bool:
    if candidate in (None, ""):
        return True
    c = _semantic_compact(candidate)
    if not c:
        return True
    return any(c in _semantic_compact(t) for t in texts)


def _event_source_score(block: Dict[str, Any], ev: Dict[str, Any]) -> int:
    text = str(block.get("text") or "")
    score = 0
    date = str(ev.get("date") or "")
    if date and _date_mentioned(text, date):
        score += 5
    venue = ev.get("venue")
    if venue and _scalar_semantic_present(venue, [text]):
        score += 4
    for key in ("openTime", "startTime"):
        if ev.get(key) and _scalar_present(str(ev[key]), [text]):
            score += 2
    title = ev.get("title")
    if title and _semantic_compact(title) in _semantic_compact(text):
        score += 2
    if block.get("kind") == "detail_enriched":
        score += 2
    return score


def _repair_event_source(item: Dict[str, Any], ev: Dict[str, Any]) -> Dict[str, Any]:
    current = ev.get("sourceUrl")
    current_blocks = [
        b for b in item.get("evidenceBlocks", []) or []
        if b.get("sourceUrl") == current
    ]
    current_score = max((_event_source_score(b, ev) for b in current_blocks), default=-1)

    best = None
    best_score = current_score
    for block in item.get("evidenceBlocks", []) or []:
        url = block.get("sourceUrl")
        if not url:
            continue
        score = _event_source_score(block, ev)
        if score > best_score:
            best_score = score
            best = block

    if best is not None and best_score >= 5 and best.get("sourceUrl") != current:
        ev["_sourceUrlRepairedFrom"] = current
        ev["sourceUrl"] = best.get("sourceUrl")
    return ev


def _strong_nonperformance_event_context(ev: Dict[str, Any]) -> bool:
    text = " ".join(
        str(ev.get(k) or "")
        for k in ("title", "relationEvidence", "evidence")
    )
    commercial = re.search(
        r"グッズ|物販|会場販売|整理券|商品|受注販売|通販|ONLINE\s*STORE|"
        r"プレオーダー|会場受取|販売時間|購入|新ITEM|NEW\s*GOODS",
        text,
        re.I,
    )
    performance = re.search(
        r"開場|開演|出演|ワンマン|ライブ(?:開催|公演)?|コンサート|"
        r"フェス|FES|LIVE\s+(?:2026|TOUR)|公演日程",
        text,
        re.I,
    )
    return bool(commercial and not performance)


def _streaming_only_event(ev: Dict[str, Any]) -> bool:
    if ev.get("venue"):
        return False
    text = " ".join(
        str(ev.get(k) or "")
        for k in ("title", "relationEvidence", "evidence")
    )
    streaming = bool(re.search(
        r"TikTok\s*LIVE|YouTube\s*LIVE|Instagram\s*LIVE|"
        r"オンライン配信|生配信|配信限定|配信ライブ|LIVE\s*Premiere",
        text,
        re.I,
    ))
    physical = bool(re.search(
        r"会場|開場|ホール|アリーナ|ドーム|Zepp|Billboard\s*Live|"
        r"ぴあアリーナ|PIT|スタジアム",
        text,
        re.I,
    ))
    return streaming and not physical


def _weak_news_date_event(ev: Dict[str, Any]) -> bool:
    # ニュース投稿日・更新日を公演日にしたケースを落とす。
    if ev.get("venue") or ev.get("openTime") or ev.get("startTime"):
        return False
    text = " ".join(
        str(ev.get(k) or "")
        for k in ("title", "relationEvidence", "evidence")
    )
    non_event = bool(re.search(
        r"連動企画(?:決定)?|企画決定|グッズ.*発売|新作アイテム.*発売|"
        r"MAP公開|払い戻し|払戻し|お詫び|情報更新|販売決定|公開決定",
        text,
        re.I,
    ))
    performance = bool(re.search(
        r"開催決定|出演決定|公演日(?:程)?|開場|開演|会場[:：]|"
        r"\\d{1,2}月\\d{1,2}日.{0,40}(?:開催|公演)",
        text,
        re.I,
    ))
    return non_event and not performance


def _merch_ticket_context(evidence: str) -> bool:
    merchandise = re.search(
        r"グッズ|ITEMS?|商品|受注販売|ONLINE\s*STORE|通販|"
        r"会場受取|プレオーダー|予約商品|お届け時期|購入",
        evidence or "",
        re.I,
    )
    ticketish = re.search(
        r"チケット(?:先行|受付|トレード|一般発売)|"
        r"ファンクラブ抽選|FC抽選|先行受付|一般発売|当日券",
        evidence or "",
        re.I,
    )
    return bool(merchandise and not ticketish)





def _ticket_source_score(block: Dict[str, Any], tw: Dict[str, Any]) -> int:
    text = str(block.get("text") or "")
    score = 0

    start_at = str(tw.get("startAt") or "")
    end_at = str(tw.get("endAt") or "")
    start_date = start_at[:10] if len(start_at) >= 10 else ""
    end_date = end_at[:10] if len(end_at) >= 10 else ""

    if _parse_date(start_date) and _date_mentioned(text, start_date):
        score += 6
    if _parse_date(end_date) and _date_mentioned(text, end_date):
        score += 5

    start_time = start_at[11:16] if len(start_at) >= 16 else ""
    end_time = end_at[11:16] if len(end_at) >= 16 else ""
    if start_time and _scalar_present(start_time, [text]):
        score += 1
    if end_time and _scalar_present(end_time, [text]):
        score += 1

    name = str(tw.get("name") or "")
    if name and _semantic_compact(name) in _semantic_compact(text):
        score += 3

    evidence = str(tw.get("evidence") or "")
    ec = _semantic_compact(evidence)
    tc = _semantic_compact(text)
    if ec and len(ec) >= 12 and ec in tc:
        score += 3

    if block.get("kind") == "detail_enriched":
        score += 3
    return score


def _repair_ticket_source(item: Dict[str, Any], tw: Dict[str, Any]) -> Dict[str, Any]:
    tw = dict(tw)
    current = tw.get("sourceUrl")
    current_blocks = [
        b for b in item.get("evidenceBlocks", []) or []
        if _same_source_page(b.get("sourceUrl"), current)
    ]
    current_score = max(
        (_ticket_source_score(b, tw) for b in current_blocks),
        default=-1,
    )

    best = None
    best_score = current_score
    for block in item.get("evidenceBlocks", []) or []:
        url = block.get("sourceUrl")
        if not url:
            continue
        score = _ticket_source_score(block, tw)
        if score > best_score:
            best = block
            best_score = score

    # 日付根拠を含む詳細本文を優先する。
    if best is not None and best_score >= 6:
        best_url = best.get("sourceUrl")
        if not _same_source_page(best_url, current):
            tw["_sourceUrlRepairedFrom"] = current
            tw["sourceUrl"] = best_url
    return tw


def _ticket_direct_text(tw: Dict[str, Any]) -> str:
    return " ".join([
        str(tw.get("name") or ""),
        str(tw.get("evidence") or ""),
    ])


def _ticket_direct_is_explicit(tw: Dict[str, Any]) -> bool:
    text = _ticket_direct_text(tw)
    return bool(re.search(
        r"チケット|入場券|抽選|先行|一般発売|当日券|"
        r"機材席|ステージバック席|見切れ席|注釈付き席|追加席|"
        r"ファンクラブ抽選|FC抽選|プレイガイド",
        text,
        re.I,
    ))


def _ticket_refine_context(tw: Dict[str, Any], source_texts: Optional[List[str]] = None) -> str:
    evidence = str(tw.get("evidence") or "")
    source_texts = source_texts or []
    start_at = str(tw.get("startAt") or "")
    date_part = start_at[:10] if len(start_at) >= 10 else ""
    time_part = start_at[11:16] if len(start_at) >= 16 else ""

    contexts = []
    if _parse_date(date_part):
        for text in source_texts:
            for ctx in _date_local_contexts(text, date_part, radius=260):
                if time_part:
                    hh, mm = time_part.split(":")
                    time_variants = {
                        time_part,
                        time_part.replace(":", "："),
                        f"{int(hh)}時{mm}分",
                        f"{int(hh)}時" if mm == "00" else "",
                    }
                    time_variants.discard("")
                    if not any(v in ctx for v in time_variants):
                        continue
                contexts.append(ctx)

    return " ".join([evidence] + contexts)


def _refine_ticket_name(
    tw: Dict[str, Any],
    source_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    tw = dict(tw)
    evidence = str(tw.get("evidence") or "")
    context = _ticket_refine_context(tw, source_texts)
    current = str(tw.get("name") or "").strip()

    generic = (
        not current
        or bool(re.fullmatch(
            r"(一般発売(?:（[^）]+）)?|販売期間|受付期間|販売|受付|先着販売)",
            current,
            re.I,
        ))
    )

    # evidence に明示された単純な受付種別は最優先。
    # LLMが同じ記事内の別受付名を誤って付けた場合もここで修復する。
    direct_patterns = [
        (r"ファンクラブ抽選先行", "ファンクラブ抽選先行"),
        (r"(?:オフィシャル|公式)\s*先行", "オフィシャル先行"),
        (r"チケット一般発売|一般発売", "一般発売"),
    ]
    direct_label = None
    for pattern, label in direct_patterns:
        if re.search(pattern, evidence, re.I):
            direct_label = label
            break

    simple_labels = {
        "ファンクラブ抽選先行",
        "オフィシャル先行",
        "一般発売",
    }
    if direct_label and (generic or current in simple_labels):
        tw["name"] = direct_label
        if current and current != direct_label:
            tw["_nameRefinedFrom"] = current
        current = direct_label
        generic = current == "一般発売"

    # より具体的な複合名は保持する。
    if not generic and current not in simple_labels:
        return tw

    # 機材席・ステージバック席などは、LLM evidence が「一般発売日」だけになることがある。
    # この種別だけは同一日時の公式source局所文脈から補完する。
    special_patterns = [
        (r"機材席.{0,24}?開放.{0,24}?販売", "機材席開放販売"),
        (r"ステージバック席.{0,24}?販売", "ステージバック席追加販売"),
        (r"見切れ席(?:追加)?販売", "見切れ席販売"),
        (r"注釈付き(?:指定)?席(?:追加)?販売", "注釈付き席販売"),
        (r"追加席(?:追加)?販売", "追加席販売"),
    ]
    for pattern, label in special_patterns:
        if re.search(pattern, context, re.I):
            tw["name"] = label
            if current and current != label:
                tw["_nameRefinedFrom"] = current
            return tw

    if direct_label:
        return tw

    # evidence に種別が無い場合のみsource局所文脈へfallback。
    fallback_patterns = [
        (r"ファンクラブ抽選先行", "ファンクラブ抽選先行"),
        (r"(?:オフィシャル|公式)\s*先行", "オフィシャル先行"),
        (r"一般発売", "一般発売"),
    ]
    for pattern, label in fallback_patterns:
        if re.search(pattern, context, re.I):
            tw["name"] = label
            if current and current != label:
                tw["_nameRefinedFrom"] = current
            return tw

    return tw


def _ticket_window_is_explicit(tw: Dict[str, Any], context: str) -> bool:
    text = " ".join([
        str(tw.get("name") or ""),
        str(tw.get("evidence") or ""),
        context or "",
    ])
    return bool(re.search(
        r"チケット|入場券|抽選|先行|受付|一般発売|当日券|先着|"
        r"機材席|ステージバック席|見切れ席|注釈付き席|追加席|追加販売|"
        r"ファンクラブ|FC先行|プレイガイド",
        text,
        re.I,
    ))


def _date_local_contexts(text: str, date_str: str, radius: int = 500) -> List[str]:
    if not text or not date_str:
        return []
    compact_text = _compact(text)
    out = []
    # 元テキスト上では表記揺れを順に探す。最初の数件で十分。
    for variant in _date_variants(date_str):
        start = 0
        for _ in range(3):
            idx = text.find(variant, start)
            if idx < 0:
                break
            lo = max(0, idx - radius)
            hi = min(len(text), idx + len(variant) + radius)
            out.append(text[lo:hi])
            start = idx + len(variant)
        if out:
            break
    if not out and _compact(date_str) in compact_text:
        out.append(text[: min(len(text), radius * 2)])
    return out


def _ticket_context(tw: Dict[str, Any], source_texts: List[str]) -> str:
    snippets = []
    for field in ("startAt", "endAt"):
        val = str(tw.get(field) or "")
        date_part = val[:10] if len(val) >= 10 else ""
        if not _parse_date(date_part):
            continue
        for text in source_texts:
            snippets.extend(_date_local_contexts(text, date_part))
    if not snippets:
        snippets = source_texts[:2]
    return "\n".join(snippets)


def _ticket_is_merchandise(tw: Dict[str, Any], source_texts: List[str]) -> bool:
    direct = _ticket_direct_text(tw)
    context = _ticket_context(tw, source_texts)
    combined = direct + " " + context

    direct_merchandise = bool(re.search(
        r"グッズ|物販|商品|受注販売|ONLINE\s*STORE|通販|"
        r"会場受取|予約商品|お届け|購入|アパレル|Tシャツ|GOODS|ITEMS?",
        direct,
        re.I,
    ))
    context_merchandise = bool(re.search(
        r"グッズ|物販|商品|受注販売|ONLINE\s*STORE|通販|"
        r"会場受取|予約商品|お届け|購入|アパレル|Tシャツ|GOODS|ITEMS?",
        combined,
        re.I,
    ))

    name = str(tw.get("name") or "").strip()
    generic_name = bool(re.fullmatch(
        r"(販売期間|受付期間|販売|受付|先着販売)",
        name,
        re.I,
    ))
    direct_ticket = _ticket_direct_is_explicit(tw)

    # 周辺500文字に別の「チケット」文言があっても救済しない。
    # LLM自身が返した name/evidence に入場券根拠がないgeneric販売は除外。
    if generic_name and not direct_ticket:
        return True
    if direct_merchandise and not direct_ticket:
        return True
    if context_merchandise and re.search(r"販売期間", direct) and not direct_ticket:
        return True
    return False


def _relation_ambiguous_for_date(relation: str, date_str: str) -> bool:
    if not relation or _count_calendar_mentions(relation) <= 1:
        return False
    segments = [
        seg.strip()
        for seg in re.split(r"[\n。；;※●■◆◇]+", relation)
        if seg.strip()
    ]
    target_segments = [seg for seg in segments if _date_mentioned(seg, date_str)]
    if not target_segments:
        return True
    return not any(_count_calendar_mentions(seg) == 1 for seg in target_segments)


def _source_has_unambiguous_date_venue(
    source_texts: List[str],
    date_str: str,
    venue: Optional[str],
) -> bool:
    if not date_str:
        return False
    for text in source_texts:
        for ctx in _date_local_contexts(text, date_str, radius=380):
            if not _date_mentioned(ctx, date_str):
                continue
            if venue and _scalar_semantic_present(venue, [ctx]):
                return True
            if re.search(r"日程|公演|日時|会場|OPEN|START|開場|開演", ctx, re.I):
                if _count_calendar_mentions(ctx) == 1:
                    return True
    return False

def _load_relations(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": 1, "artists": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"schemaVersion": 1, "artists": {}}
    except Exception:
        return {"schemaVersion": 1, "artists": {}}


def _member_alias_in_block(text: str, alias: str, short_requires_subject: bool = False) -> bool:
    if not text or not alias:
        return False
    if short_requires_subject:
        return False
    a = unicodedata.normalize("NFKC", alias).lower().strip()
    t = unicodedata.normalize("NFKC", text).lower()
    if not a:
        return False
    if re.fullmatch(r"[a-z]+", a):
        return bool(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", t))
    return a in t


def _member_focus_materials(
    item: Dict[str, Any],
    relations: Dict[str, Any],
    max_evidence_chars: int,
    max_block_chars: int,
) -> List[Dict[str, Any]]:
    cfg = ((relations.get("artists") or {}).get(item.get("artistId")) or {})
    outputs = []
    if cfg.get("focusExtraction") is False:
        return outputs

    for member in cfg.get("members", []) or []:
        aliases = [a for a in (member.get("aliases") or [member.get("name")]) if a]
        matched = []
        for block in item.get("evidenceBlocks", []) or []:
            text = str(block.get("text") or "")
            if any(
                _member_alias_in_block(
                    text,
                    alias,
                    bool(member.get("shortAliasRequiresSubject")),
                )
                for alias in aliases
            ):
                matched.append(dict(block))

        if not matched:
            continue

        pseudo = dict(item)
        pseudo["reason"] = "MEMBER_FOCUS"
        pseudo["evidenceBlocks"] = matched
        pseudo["changedLotteryText"] = []
        pseudo["unparsedDateLines"] = []

        for material in _material_chunks(
            pseudo,
            max_evidence_chars=max_evidence_chars,
            max_block_chars=max_block_chars,
        ):
            material["focusSubject"] = {
                "memberId": member.get("memberId"),
                "memberName": member.get("name"),
                "aliases": aliases,
            }
            outputs.append(material)

    return outputs



def _parse_isoish_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value[:16] if "H" in fmt or "%M" in fmt else value[:10], fmt)
        except ValueError:
            pass
    return None


def _validate_extraction(
    item: Dict[str, Any],
    parsed: Dict[str, Any],
    reference_date: datetime,
    history_days: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    allowed_urls = _allowed_urls(item)
    valid_events = []
    valid_tickets = []
    rejected = []

    history_cutoff = reference_date.date().toordinal() - history_days

    for idx, original_ev in enumerate(parsed.get("events", []) or []):
        ev = _recover_event_title(original_ev)
        ev = _repair_event_source(item, ev)
        reasons = []

        if _looks_like_promoter_not_venue(ev.get("venue")):
            ev["_venueRemovedFrom"] = ev.get("venue")
            ev["venue"] = None
            reasons.append("VENUE_REMOVED_PROMOTER")

        date = str(ev.get("date") or "")
        evidence = str(ev.get("evidence") or "")
        relation = str(ev.get("relationEvidence") or "")
        source_url = ev.get("sourceUrl")
        source_texts = _source_texts(item, source_url)

        if not _parse_date(date):
            reasons.append("INVALID_DATE")
        else:
            dt = datetime.strptime(date, "%Y-%m-%d")
            if dt.date().toordinal() < history_cutoff:
                reasons.append("STALE_EVENT")

        if not _source_url_allowed(source_url, allowed_urls):
            reasons.append("INVALID_SOURCE_URL")

        if date and not any(_date_mentioned(t, date) for t in source_texts):
            reasons.append("DATE_NOT_IN_SOURCE")

        if not evidence:
            reasons.append("EMPTY_EVIDENCE")

        if ev.get("venue") and not _scalar_semantic_present(ev.get("venue"), source_texts):
            reasons.append("VENUE_NOT_IN_SOURCE")

        title = ev.get("title")
        if title:
            title_compact = _semantic_compact(title)
            if len(title_compact) >= 8 and not any(
                title_compact in _semantic_compact(t) for t in source_texts
            ):
                reasons.append("TITLE_NOT_EXACT_IN_SOURCE")

        if not relation:
            reasons.append("EMPTY_RELATION_EVIDENCE")
        elif date and not _date_mentioned(relation, date):
            if not _relation_date_backed_by_atomic_evidence(ev):
                reasons.append("RELATION_EVIDENCE_MISSING_DATE")

        if relation and _relation_ambiguous_for_date(relation, date):
            if not _source_has_unambiguous_date_venue(source_texts, date, ev.get("venue")):
                reasons.append("AMBIGUOUS_MULTI_DATE_RELATION")

        if _strong_nonperformance_event_context(ev):
            reasons.append("NON_PERFORMANCE_CONTEXT")

        if _weak_news_date_event(ev):
            reasons.append("NEWS_DATE_ONLY")

        if _streaming_only_event(ev):
            reasons.append("STREAMING_EVENT")

        for key in ("openTime", "startTime"):
            if not _time_valid(ev.get(key)):
                reasons.append(f"INVALID_{key.upper()}")
            elif ev.get(key) and not _scalar_present(str(ev[key]), source_texts):
                ev[key] = None
                reasons.append(f"{key.upper()}_REMOVED_NO_EVIDENCE")

        soft = {
            "OPENTIME_REMOVED_NO_EVIDENCE",
            "STARTTIME_REMOVED_NO_EVIDENCE",
            "TITLE_NOT_EXACT_IN_SOURCE",
            "VENUE_REMOVED_PROMOTER",
        }
        hard = [r for r in reasons if r not in soft]

        if hard:
            rejected.append({
                "kind": "event",
                "index": idx,
                "artistId": item.get("artistId"),
                "reasons": reasons,
                "value": ev,
            })
        else:
            ev["_validationNotes"] = reasons
            valid_events.append(ev)

    for idx, original_tw in enumerate(parsed.get("ticketWindows", []) or []):
        tw = _repair_ticket_source(item, original_tw)
        reasons = []
        source_url = tw.get("sourceUrl")
        source_texts = _source_texts(item, source_url)
        tw = _refine_ticket_name(tw, source_texts)
        evidence = str(tw.get("evidence") or "")

        if not _source_url_allowed(source_url, allowed_urls):
            reasons.append("INVALID_SOURCE_URL")
        if not evidence:
            reasons.append("EMPTY_EVIDENCE")
        ticket_context = _ticket_context(tw, source_texts)
        if not _ticket_window_is_explicit(tw, ticket_context):
            reasons.append("NOT_EXPLICIT_TICKET_WINDOW")
        if re.search(r"配信視聴|視聴チケット|配信チケット", evidence + " " + ticket_context):
            reasons.append("STREAMING_TICKET")
        if _ticket_is_merchandise(tw, source_texts):
            reasons.append("MERCHANDISE_SALE")
        if _ticket_is_non_admission_raffle(tw, source_texts):
            reasons.append("NON_ADMISSION_RAFFLE")
        if not tw.get("startAt") and not tw.get("endAt"):
            reasons.append("MISSING_TICKET_DATE")

        start_dt = _parse_isoish_date(tw.get("startAt"))
        end_dt = _parse_isoish_date(tw.get("endAt"))
        effective = end_dt or start_dt
        if effective and effective.date().toordinal() < history_cutoff:
            reasons.append("STALE_TICKET_WINDOW")

        soft_notes = []
        for field in ("startAt", "endAt"):
            val = tw.get(field)
            if not val:
                continue
            date_part = str(val)[:10]
            if not _parse_date(date_part):
                continue
            if _date_year_conflict(date_part, evidence, source_url, source_texts):
                reasons.append(f"{field.upper()}_YEAR_CONFLICT")
            elif not any(_date_mentioned(t, date_part) for t in source_texts):
                reasons.append(f"{field.upper()}_DATE_NOT_IN_SOURCE")

        hard = list(reasons)

        if hard:
            rejected.append({
                "kind": "ticketWindow",
                "index": idx,
                "artistId": item.get("artistId"),
                "reasons": reasons,
                "value": tw,
            })
        else:
            tw["_validationNotes"] = soft_notes
            valid_tickets.append(tw)

    return {
        "artistId": item.get("artistId"),
        "artistName": item.get("artistName"),
        "events": valid_events,
        "ticketWindows": valid_tickets,
        "uncertain": parsed.get("uncertain", []) or [],
    }, rejected


def _call_ollama(
    base_url: str,
    model: str,
    think,
    material: Dict[str, Any],
    timeout: int,
    num_ctx: int,
    num_predict: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(material, ensure_ascii=False, indent=2),
            },
        ],
        "stream": False,
        "think": think,
        "format": "json",
        "options": {
            "temperature": 0,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as res:
        response = json.loads(res.read().decode("utf-8"))
    elapsed = time.time() - started

    content = response.get("message", {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(
            "Ollama message.content が空です "
            f"(done_reason={response.get('done_reason')}, "
            f"thinkingChars={len(response.get('message', {}).get('thinking') or '')})"
        )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON_PARSE_ERROR: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("モデル出力JSONのルートがobjectではありません")
    return parsed, response, elapsed


def _write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ollama native APIによるライブ情報の事実抽出専用ランナー"
    )
    ap.add_argument("artist_ids", nargs="*", help="対象artistId。省略時はqueue全件")
    ap.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    ap.add_argument("--model", default=_default_model())
    ap.add_argument(
        "--think",
        default="auto",
        choices=["auto", "false", "true", "low", "medium", "high"],
    )
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--num-predict", type=int, default=4096)
    ap.add_argument("--history-days", type=int, default=180, help="これより古い公演/受付はstaleとして除外")
    ap.add_argument("--relations", type=Path, default=Path("config/artist_relations.json"), help="メンバー関係設定")
    ap.add_argument("--max-evidence-chars", type=int, default=9000, help="1 LLM callあたりのevidence文字数上限")
    ap.add_argument("--max-block-chars", type=int, default=5000, help="長いevidence blockの分割サイズ")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    base_url = _native_base_url()
    think = _think_value(args.model, args.think)

    if args.check:
        try:
            with urllib.request.urlopen(base_url + "/api/ps", timeout=10) as res:
                data = json.loads(res.read().decode("utf-8"))
            print("Ollama接続OK")
            for m in data.get("models", []):
                print(" ", m.get("name") or m.get("model"))
            return 0
        except Exception as exc:
            print(f"[ERROR] Ollama接続失敗: {exc}", file=sys.stderr)
            return 1

    queue = _load_queue(args.queue)
    relations = _load_relations(args.relations)
    generated_at = queue.get("generatedAt")
    try:
        reference_date = datetime.fromisoformat(str(generated_at)) if generated_at else datetime.now().astimezone()
    except ValueError:
        reference_date = datetime.now().astimezone()
    requested = set(args.artist_ids)
    items = [
        x for x in queue["items"]
        if not requested or x.get("artistId") in requested
    ]
    if requested:
        found = {x.get("artistId") for x in items}
        missing = sorted(requested - found)
        if missing:
            print(
                "[WARN] queueに存在しないartistId: " + ", ".join(missing),
                file=sys.stderr,
            )
    if not items:
        print("[ERROR] 処理対象がありません", file=sys.stderr)
        return 1

    run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    run_dir = args.out_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    input_export = {
        "schemaVersion": 1,
        "runId": run_id,
        "model": args.model,
        "think": think,
        "queueGeneratedAt": queue.get("generatedAt"),
        "items": [_material(x) for x in items],
    }
    _write_json(run_dir / "input.json", input_export)

    facts = []
    rejected = []
    errors = []
    requests = []
    total_started = time.time()

    print(f"model: {args.model}")
    print(f"think: {think}")
    print(f"入力: {len(items)}組")

    for pos, item in enumerate(items, start=1):
        aid = item.get("artistId")
        name = item.get("artistName") or aid
        print(f"[{pos}/{len(items)}] {name}", flush=True)
        started = time.time()
        normal_chunks = _material_chunks(
            item,
            max_evidence_chars=args.max_evidence_chars,
            max_block_chars=args.max_block_chars,
        )
        member_chunks = _member_focus_materials(
            item,
            relations=relations,
            max_evidence_chars=args.max_evidence_chars,
            max_block_chars=args.max_block_chars,
        )
        member_event_jobs = []
        member_ticket_jobs = []
        for material in member_chunks:
            event_material = dict(material)
            event_material["focusTask"] = "events_only"
            member_event_jobs.append((event_material, "member_event_focus"))

            ticket_material = dict(material)
            ticket_material["focusTask"] = "tickets_only"
            member_ticket_jobs.append((ticket_material, "member_ticket_focus"))

        event_jobs = []
        ticket_jobs = []
        for material in normal_chunks:
            event_material = dict(material)
            event_material["focusTask"] = "events_only"
            event_jobs.append((event_material, "event_focus"))

            ticket_material = dict(material)
            ticket_material["focusTask"] = "tickets_only"
            ticket_jobs.append((ticket_material, "ticket_focus"))

        chunk_jobs = (
            member_event_jobs
            + member_ticket_jobs
            + event_jobs
            + ticket_jobs
        )
        chunks = [m for m, _ in chunk_jobs]
        valid_parts = []
        artist_rejected = []
        chunk_errors = []
        artist_elapsed = 0.0

        for chunk_index, (material, pass_type) in enumerate(chunk_jobs, start=1):
            try:
                parsed, response, elapsed = _call_ollama(
                    base_url=base_url,
                    model=args.model,
                    think=think,
                    material=material,
                    timeout=args.timeout,
                    num_ctx=args.num_ctx,
                    num_predict=args.num_predict,
                )
                artist_elapsed += elapsed
                valid_part, rejected_items = _validate_extraction(
                    item,
                    parsed,
                    reference_date=reference_date,
                    history_days=args.history_days,
                )
                expanded_events = []
                for ev in valid_part.get("events", []) or []:
                    for expanded in _expand_explicit_same_day_stages(item, ev):
                        expanded["_originPass"] = pass_type
                        expanded_events.append(expanded)
                valid_part["events"] = expanded_events
                for tw in valid_part.get("ticketWindows", []) or []:
                    tw["_originPass"] = pass_type
                valid_parts.append(valid_part)
                artist_rejected.extend(rejected_items)
                requests.append({
                    "artistId": aid,
                    "chunkIndex": chunk_index,
                    "chunkCount": len(chunks),
                    "passType": pass_type,
                    "evidenceChars": sum(
                        len(str(b.get("text") or ""))
                        for b in material.get("evidenceBlocks", []) or []
                    ),
                    "elapsedSeconds": round(elapsed, 3),
                    "promptEvalCount": response.get("prompt_eval_count"),
                    "evalCount": response.get("eval_count"),
                    "outputChars": len(response.get("message", {}).get("content") or ""),
                    "thinkingChars": len(response.get("message", {}).get("thinking") or ""),
                    "doneReason": response.get("done_reason"),
                })
            except Exception as exc:
                chunk_error = {
                    "artistId": aid,
                    "artistName": name,
                    "chunkIndex": chunk_index,
                    "chunkCount": len(chunks),
                    "passType": pass_type,
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                    "elapsedSeconds": round(time.time() - started, 3),
                }
                errors.append(chunk_error)
                chunk_errors.append(chunk_error)
                print(
                    f"  [chunk {chunk_index}/{len(chunks)} ERROR] {exc}",
                    file=sys.stderr,
                )

        if valid_parts:
            valid = _merge_valid_facts(
                valid_parts,
                artist_id=aid,
                artist_name=name,
            )
            facts.append(valid)
            rejected.extend(artist_rejected)
            print(
                f"  chunks={len(chunks)} "
                f"events={len(valid['events'])} "
                f"tickets={len(valid['ticketWindows'])} "
                f"rejected={len(artist_rejected)} "
                f"chunkErrors={len(chunk_errors)} "
                f"{artist_elapsed:.1f}s"
            )
        elif chunk_errors:
            print(
                f"  [ERROR] 全chunk失敗 ({len(chunk_errors)}/{len(chunks)})",
                file=sys.stderr,
            )

    report = {
        "schemaVersion": 1,
        "runId": run_id,
        "startedAt": datetime.fromtimestamp(
            total_started
        ).astimezone().isoformat(timespec="seconds"),
        "completedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": args.model,
        "think": think,
        "historyDays": args.history_days,
        "referenceDate": reference_date.isoformat(),
        "artistsInput": len(items),
        "artistsSucceeded": len(facts),
        "errorCount": len(errors),
        "artistsWithErrors": len({e.get("artistId") for e in errors}),
        "chunkedMode": True,
        "maxEvidenceChars": args.max_evidence_chars,
        "maxBlockChars": args.max_block_chars,
        "eventFacts": sum(len(x["events"]) for x in facts),
        "ticketFacts": sum(len(x["ticketWindows"]) for x in facts),
        "validationRejected": len(rejected),
        "elapsedSeconds": round(time.time() - total_started, 3),
        "requests": requests,
        "writeBoundary": str(args.out_root) + "/ only",
        "productionDataModified": False,
    }

    _write_json(run_dir / "facts.json", {"schemaVersion": 1, "items": facts})
    _write_json(run_dir / "rejected.json", {"schemaVersion": 1, "items": rejected})
    _write_json(run_dir / "errors.json", {"schemaVersion": 1, "items": errors})
    _write_json(run_dir / "report.json", report)

    print()
    print(f"run: {run_id}")
    print(
        f"成功 {len(facts)}/{len(items)}組 / "
        f"events {report['eventFacts']} / "
        f"tickets {report['ticketFacts']} / "
        f"validator却下 {len(rejected)} / "
        f"エラー {len(errors)}"
    )
    print(f"保存先: {run_dir}")
    return 1 if len(facts) == 0 and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
