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
13. JSON以外は出力しない。

出力形式:
{
  "artistId": "string",
  "events": [
    {
      "title": "string or null",
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
        f"{m}/{d}",
        f"{m:02d}/{d:02d}",
        f"{m}月{d}日",
    ]


def _date_mentioned(text: str, date_str: str) -> bool:
    c = _compact(text)
    return any(_compact(v) in c for v in _date_variants(date_str))


def _count_calendar_mentions(text: str) -> int:
    # 単一のalternationで非重複マッチにし、
    # "2026/08/29" とその部分文字列 "08/29" の二重カウントを防ぐ。
    pat = re.compile(
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}"
        r"|\d{4}年\d{1,2}月\d{1,2}日"
        r"|(?<!\d)\d{1,2}[/-]\d{1,2}(?!\d)"
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


def _source_texts(item: Dict[str, Any], source_url: Optional[str]) -> List[str]:
    out = []
    for block in item.get("evidenceBlocks", []) or []:
        if source_url and block.get("sourceUrl") != source_url:
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
    texts = _evidence_texts(item)
    allowed_urls = _allowed_urls(item)
    valid_events = []
    valid_tickets = []
    rejected = []

    history_cutoff = reference_date.date().toordinal() - history_days

    for idx, ev in enumerate(parsed.get("events", []) or []):
        reasons = []
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

        if source_url not in allowed_urls:
            reasons.append("INVALID_SOURCE_URL")

        if date and not any(_date_mentioned(t, date) for t in source_texts):
            reasons.append("DATE_NOT_IN_SOURCE")

        # LLMのevidence全文一致は要求しない。原文の一部を正規化・再結合しても、
        # 日付/会場/時刻/titleの個別事実が同一sourceに存在すればよい。
        if not evidence:
            reasons.append("EMPTY_EVIDENCE")

        if ev.get("venue") and not _scalar_present(ev.get("venue"), source_texts):
            reasons.append("VENUE_NOT_IN_SOURCE")

        title = ev.get("title")
        if title:
            # titleは記号差を許容するため、完全一致できなくても即rejectしない。
            title_compact = _compact(title)
            if len(title_compact) >= 8 and not any(
                title_compact in _compact(t) for t in source_texts
            ):
                reasons.append("TITLE_NOT_EXACT_IN_SOURCE")

        if not relation:
            reasons.append("EMPTY_RELATION_EVIDENCE")
        elif date and not _date_mentioned(relation, date):
            reasons.append("RELATION_EVIDENCE_MISSING_DATE")

        # 複数日が1つのrelationEvidenceに混在する場合は安全側へ。
        # 「イベント全体は2日、本人出演は1日」の誤生成を防ぐ。
        if relation and _count_calendar_mentions(relation) > 1:
            reasons.append("AMBIGUOUS_MULTI_DATE_RELATION")

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

    for idx, tw in enumerate(parsed.get("ticketWindows", []) or []):
        reasons = []
        evidence = str(tw.get("evidence") or "")
        source_url = tw.get("sourceUrl")
        source_texts = _source_texts(item, source_url)

        if source_url not in allowed_urls:
            reasons.append("INVALID_SOURCE_URL")
        if not evidence:
            reasons.append("EMPTY_EVIDENCE")
        if not re.search(r"抽選|先行|受付|発売|当日券|チケット|販売", evidence):
            reasons.append("NOT_EXPLICIT_TICKET_WINDOW")
        if re.search(r"配信視聴|視聴チケット|配信チケット", evidence):
            reasons.append("STREAMING_TICKET")

        start_dt = _parse_isoish_date(tw.get("startAt"))
        end_dt = _parse_isoish_date(tw.get("endAt"))
        effective = end_dt or start_dt
        if effective and effective.date().toordinal() < history_cutoff:
            reasons.append("STALE_TICKET_WINDOW")

        # start/endの明示値がsourceに全く見当たらない場合は要監査。
        # ただし表記揺れが大きいのでhard rejectにはしない。
        soft_notes = []
        for field in ("startAt", "endAt"):
            val = tw.get(field)
            if val:
                date_part = str(val)[:10]
                if _parse_date(date_part) and not any(
                    _date_mentioned(t, date_part) for t in source_texts
                ):
                    soft_notes.append(f"{field.upper()}_DATE_NOT_IN_SOURCE")

        reasons.extend(soft_notes)
        soft = {
            "STARTAT_DATE_NOT_IN_SOURCE",
            "ENDAT_DATE_NOT_IN_SOURCE",
        }
        hard = [r for r in reasons if r not in soft]
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
        chunks = _material_chunks(
            item,
            max_evidence_chars=args.max_evidence_chars,
            max_block_chars=args.max_block_chars,
        )
        valid_parts = []
        artist_rejected = []
        chunk_errors = []
        artist_elapsed = 0.0

        for chunk_index, material in enumerate(chunks, start=1):
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
                valid_parts.append(valid_part)
                artist_rejected.extend(rejected_items)
                requests.append({
                    "artistId": aid,
                    "chunkIndex": chunk_index,
                    "chunkCount": len(chunks),
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
