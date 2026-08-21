#!/usr/bin/env python3
"""ローカルLLM候補の共通検証・staging入出力。

このモジュールはWeb取得も本番データへの書き込みも行わない。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_LLM_ROOT = REPO_ROOT / "local_llm"
RUNS_DIR = LOCAL_LLM_ROOT / "runs"
PENDING_FILE = LOCAL_LLM_ROOT / "review" / "pending.json"
ARTIST_DIR = REPO_ROOT / "data" / "artist"

ENTITY_COLLECTIONS = {
    "tour": "tours",
    "performance": "performances",
    "lottery": "lotteries",
}
VALID_ACTIONS = {"add", "update"}
VALID_REVIEW_STATUSES = {"pending", "approved", "rejected", "promoted"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class StagingValidationError(ValueError):
    """入力または候補が安全条件を満たさない。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StagingValidationError(f"JSONファイルがありません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StagingValidationError(f"JSONとして読めません: {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    """同じディレクトリ内で置換し、中途半端なJSONを残さない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def json_digest(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def ensure_under(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise StagingValidationError(f"許可された範囲外のパスです: {resolved}")
    return resolved


def validate_queue(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise StagingValidationError("AI queueのルートはobjectである必要があります")
    generated_at = payload.get("generatedAt")
    _validate_datetime(generated_at, "generatedAt", nullable=False)
    items = payload.get("items")
    if not isinstance(items, list):
        raise StagingValidationError("AI queue.itemsはarrayである必要があります")

    seen = set()
    for index, item in enumerate(items):
        context = f"items[{index}]"
        if not isinstance(item, dict):
            raise StagingValidationError(f"{context}はobjectである必要があります")
        artist_id = item.get("artistId")
        if not isinstance(artist_id, str) or not ID_PATTERN.fullmatch(artist_id):
            raise StagingValidationError(f"{context}.artistIdが不正です")
        if artist_id in seen:
            raise StagingValidationError(f"artistIdが重複しています: {artist_id}")
        seen.add(artist_id)
        if not isinstance(item.get("artistName"), str) or not item["artistName"].strip():
            raise StagingValidationError(f"{context}.artistNameがありません")
        sources = item.get("sources")
        if not isinstance(sources, list) or not sources:
            raise StagingValidationError(f"{context}.sourcesがありません")
        for source in sources:
            if not isinstance(source, dict) or not _valid_http_url(source.get("url")):
                raise StagingValidationError(f"{context}.sourcesに不正なURLがあります")
        for key in ("changedLotteryText", "unparsedDateLines", "parsedEventKeys"):
            value = item.get(key, [])
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise StagingValidationError(f"{context}.{key}は文字列arrayである必要があります")
        evidence_blocks = item.get("evidenceBlocks", [])
        if not isinstance(evidence_blocks, list):
            raise StagingValidationError(f"{context}.evidenceBlocksはarrayである必要があります")
        for block in evidence_blocks:
            if (not isinstance(block, dict)
                    or not _valid_http_url(block.get("sourceUrl"))
                    or not isinstance(block.get("text"), str)
                    or not block["text"].strip()):
                raise StagingValidationError(f"{context}.evidenceBlocksが不正です")
        parsed_events = item.get("parsedEvents", [])
        if not isinstance(parsed_events, list):
            raise StagingValidationError(f"{context}.parsedEventsはarrayである必要があります")
        for event in parsed_events:
            if (not isinstance(event, dict)
                    or event.get("status") not in {"NEW", "UPDATED", "UNCHANGED"}
                    or not _valid_http_url(event.get("sourceUrl"))
                    or not isinstance(event.get("evidence"), str)):
                raise StagingValidationError(f"{context}.parsedEventsが不正です")
        approximate = item.get("approxChars")
        if not isinstance(approximate, int) or approximate < 0:
            raise StagingValidationError(f"{context}.approxCharsが不正です")
        # 既存pipelineの上限を越えた入力を後段で無制限に送らない。
        if approximate > 12000:
            raise StagingValidationError(
                f"{context}.approxChars={approximate} は安全上限12000を超えています"
            )
        link_audit = item.get("linkAudit", {})
        if not isinstance(link_audit, dict) or any(
            not isinstance(link_audit.get(key, 0), int)
            or link_audit.get(key, 0) < 0
            for key in (
                "relatedLinksFound", "relatedLinksFollowed", "detailFetchFailed"
            )
        ):
            raise StagingValidationError(f"{context}.linkAuditが不正です")
    return payload


def queue_item_text(item: Dict[str, Any]) -> str:
    parts: List[str] = []
    # parsedEventKeysは紐付け用の参考情報で、候補生成の一次根拠にはしない。
    for key in ("changedLotteryText", "unparsedDateLines"):
        parts.extend(v for v in item.get(key, []) if isinstance(v, str))
    parts.extend(
        block.get("text", "")
        for block in item.get("evidenceBlocks", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    parts.extend(
        event.get("evidence", "")
        for event in item.get("parsedEvents", [])
        if isinstance(event, dict) and isinstance(event.get("evidence"), str)
    )
    return "\n".join(parts)


def source_evidence_text(item: Dict[str, Any], source_url: str) -> str:
    parts: List[str] = []
    for block in item.get("evidenceBlocks", []):
        if isinstance(block, dict) and block.get("sourceUrl") == source_url:
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
    for event in item.get("parsedEvents", []):
        if isinstance(event, dict) and event.get("sourceUrl") == source_url:
            if isinstance(event.get("evidence"), str):
                parts.append(event["evidence"])
    # 旧queueはsource別の根拠を持たないため、互換入力に限って従来の全文照合。
    if not item.get("evidenceBlocks") and not item.get("parsedEvents"):
        return queue_item_text(item)
    return "\n".join(parts)


def allowed_source_urls(item: Dict[str, Any]) -> List[str]:
    return [
        source["url"]
        for source in item.get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("url"), str)
    ]


def validate_candidate(
    raw: Any,
    queue_item: Dict[str, Any],
    queue_generated_at: str,
    artist_dir: Path = ARTIST_DIR,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """LLM候補を本番互換形へ正規化し、根拠を入力内へ拘束する。"""
    errors: List[str] = []
    if not isinstance(raw, dict):
        return None, ["candidate entryはobjectである必要があります"]

    artist_id = queue_item["artistId"]
    action = raw.get("action")
    entity_type = raw.get("entityType")
    confidence = raw.get("confidence")
    source_url = raw.get("sourceUrl")
    evidence = raw.get("evidence")
    reason = raw.get("reason")
    candidate = raw.get("candidate")

    if action not in VALID_ACTIONS:
        errors.append("actionはadd/updateのみです")
    if entity_type not in ENTITY_COLLECTIONS:
        errors.append("entityTypeはtour/performance/lotteryのみです")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not 0 <= float(confidence) <= 1:
        errors.append("confidenceは0〜1の数値である必要があります")
    if source_url not in allowed_source_urls(queue_item):
        errors.append("sourceUrlがAI queueの取得元URLと一致しません")
    if not isinstance(evidence, str) or not evidence.strip():
        errors.append("evidenceがありません")
    elif evidence.strip() not in source_evidence_text(queue_item, source_url):
        errors.append("evidenceがsourceUrlに対応するAI queue入力に存在しません")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("reasonがありません")
    if not isinstance(candidate, dict):
        errors.append("candidateはobjectである必要があります")
        return None, errors

    normalized = dict(candidate)
    # 事実ではない管理メタデータだけを決定的に補う。
    normalized.setdefault("source", "system")
    normalized.setdefault("sourceUrl", source_url)
    normalized.setdefault("lastVerifiedAt", queue_generated_at)

    if normalized.get("source") != "system":
        errors.append("candidate.sourceはsystemである必要があります")
    if normalized.get("sourceUrl") != source_url:
        errors.append("candidate.sourceUrlが外側のsourceUrlと一致しません")
    if normalized.get("id") is None or not isinstance(normalized.get("id"), str) \
            or not ID_PATTERN.fullmatch(normalized["id"]):
        errors.append("candidate.idが不正です")
    _validate_entity(normalized, entity_type, artist_id, errors)
    if isinstance(evidence, str) and evidence.strip():
        _validate_facts_against_evidence(
            normalized, entity_type, evidence.strip(), queue_generated_at, errors
        )

    if entity_type in ENTITY_COLLECTIONS and isinstance(normalized.get("id"), str):
        existing = _load_existing_ids(artist_dir, artist_id, ENTITY_COLLECTIONS[entity_type])
        exists = normalized["id"] in existing
        if action == "add" and exists:
            errors.append("action=addですが同じIDが既に存在します")
        if action == "update" and not exists:
            errors.append("action=updateですが同じIDが本番データに存在しません")
    _validate_semantic_duplicate(
        normalized, entity_type, action, artist_dir, artist_id, errors
    )
    _validate_update_changes(
        normalized, entity_type, action, evidence if isinstance(evidence, str) else "",
        artist_dir, artist_id, errors,
    )

    if errors:
        return None, errors

    return {
        "artistId": artist_id,
        "artistName": queue_item["artistName"],
        "action": action,
        "entityType": entity_type,
        "confidence": float(confidence),
        "sourceUrl": source_url,
        "evidence": evidence.strip(),
        "reason": reason.strip(),
        "candidate": normalized,
    }, []


def _validate_semantic_duplicate(
    candidate: Dict[str, Any],
    entity_type: Any,
    action: Any,
    artist_dir: Path,
    artist_id: str,
    errors: List[str],
) -> None:
    """IDが違っても同じ実体ならaddを拒否する。"""
    if action != "add" or entity_type not in {"tour", "performance"}:
        return
    data = _load_artist_payload(artist_dir, artist_id)
    if entity_type == "tour":
        from tools.collect.merge import title_key

        key = title_key(candidate.get("title"))
        if key and any(
            title_key(tour.get("title")) == key
            for tour in data.get("tours", [])
            if isinstance(tour, dict)
        ):
            errors.append("同一タイトルの既存tourがあるためaddできません")
    else:
        from tools.collect.extract import normalize_venue

        at = str(candidate.get("performanceAt") or "")
        key = (at[:10], normalize_venue(str(candidate.get("venue") or "")))
        if key[0] and key[1] and any(
            (str(perf.get("performanceAt") or "")[:10],
             normalize_venue(str(perf.get("venue") or ""))) == key
            for perf in data.get("performances", [])
            if isinstance(perf, dict)
        ):
            errors.append("同日・同会場の既存performanceがあるためaddできません")


def _validate_update_changes(
    candidate: Dict[str, Any],
    entity_type: Any,
    action: Any,
    evidence: str,
    artist_dir: Path,
    artist_id: str,
    errors: List[str],
) -> None:
    if action != "update" or entity_type not in ENTITY_COLLECTIONS:
        return
    data = _load_artist_payload(artist_dir, artist_id)
    existing = next((
        value for value in data.get(ENTITY_COLLECTIONS[entity_type], [])
        if isinstance(value, dict) and value.get("id") == candidate.get("id")
    ), None)
    if existing is None:
        return
    ignored = {"lastVerifiedAt", "source", "sourceUrl"}
    changed = [
        key for key, value in candidate.items()
        if key not in ignored and existing.get(key) != value
    ]
    if not changed:
        errors.append("既存実体と同一内容のためupdate候補にできません")
        return
    normalized_evidence = unicodedata.normalize("NFKC", evidence)
    for key in changed:
        value = candidate.get(key)
        if isinstance(value, str) and value not in normalized_evidence:
            # 日時は別の厳格検証で日付・時刻を確認済み。
            if key not in {
                "startDate", "endDate", "performanceAt", "doorOpenAt",
                "entryStartAt", "entryEndAt", "resultAt",
                "paymentStartAt", "paymentEndAt",
            }:
                errors.append(f"update対象のcandidate.{key}がevidenceにありません")


def _validate_entity(
    candidate: Dict[str, Any], entity_type: Any, artist_id: str, errors: List[str]
) -> None:
    if entity_type == "tour":
        _require(candidate, ("id", "artistId", "title", "source", "sourceUrl", "lastVerifiedAt"), errors)
        if candidate.get("artistId") != artist_id:
            errors.append("tour.artistIdがqueueのartistIdと一致しません")
        if not isinstance(candidate.get("title"), str) or not candidate["title"].strip():
            errors.append("tour.titleがありません")
        for key in ("startDate", "endDate", "lastVerifiedAt"):
            if key in candidate:
                _validate_datetime(candidate.get(key), f"tour.{key}", nullable=key != "lastVerifiedAt", errors=errors)
        if "prices" in candidate and candidate["prices"] is not None \
                and not isinstance(candidate["prices"], list):
            errors.append("tour.pricesはarrayまたはnullである必要があります")
    elif entity_type == "performance":
        _require(candidate, ("id", "tourId", "venue", "performanceAt", "source"), errors)
        if candidate.get("kind") not in {"oneman", "fes", "taiban"}:
            errors.append("performance.kindが不正です")
        if candidate.get("kind") in {"fes", "taiban"} and not candidate.get("eventName"):
            errors.append("fes/taibanにはeventNameが必要です")
        _validate_datetime(candidate.get("performanceAt"), "performance.performanceAt", False, errors)
        for key in ("doorOpenAt", "lastVerifiedAt"):
            if key in candidate:
                _validate_datetime(candidate.get(key), f"performance.{key}", key == "doorOpenAt", errors)
    elif entity_type == "lottery":
        _require(
            candidate,
            ("id", "tourId", "type", "entryStartAt", "entryEndAt", "resultAt", "source", "performanceIds"),
            errors,
        )
        if not isinstance(candidate.get("type"), str) or not candidate["type"].strip():
            errors.append("lottery.typeがありません")
        for key in (
            "entryStartAt", "entryEndAt", "resultAt", "paymentStartAt",
            "paymentEndAt", "lastVerifiedAt",
        ):
            if key in candidate:
                _validate_datetime(candidate.get(key), f"lottery.{key}", key != "lastVerifiedAt", errors)
        performance_ids = candidate.get("performanceIds")
        if performance_ids is not None and (
            not isinstance(performance_ids, list)
            or not performance_ids
            or any(not isinstance(value, str) for value in performance_ids)
        ):
            errors.append("lottery.performanceIdsはnullまたは空でない文字列arrayです")


def _require(candidate: Dict[str, Any], keys: Iterable[str], errors: List[str]) -> None:
    for key in keys:
        if key not in candidate:
            errors.append(f"candidate.{key}がありません")


def _validate_facts_against_evidence(
    candidate: Dict[str, Any],
    entity_type: Any,
    evidence: str,
    queue_generated_at: str,
    errors: List[str],
) -> None:
    """日時・会場が引用文に存在することを機械的に再確認する。"""
    from tools.collect.extract import find_dates_flagged

    try:
        reference_date = datetime.fromisoformat(queue_generated_at).date()
    except (TypeError, ValueError):
        errors.append("queueGeneratedAtが不正なため根拠日付を照合できません")
        return
    normalized_evidence = unicodedata.normalize("NFKC", evidence)
    evidence_dates = {
        value for value, inferred in find_dates_flagged(
            normalized_evidence, today=reference_date
        )
        if not inferred
    }
    evidence_times = {
        f"{int(hour):02d}:{minute}"
        for hour, minute in re.findall(
            r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", normalized_evidence
        )
        if int(hour) <= 23
    }

    datetime_fields = {
        "tour": ("startDate", "endDate"),
        "performance": ("performanceAt", "doorOpenAt"),
        "lottery": (
            "entryStartAt", "entryEndAt", "resultAt",
            "paymentStartAt", "paymentEndAt",
        ),
    }.get(entity_type, ())
    for key in datetime_fields:
        value = candidate.get(key)
        if value is None or not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue  # ISO形式エラーは別検証で報告する。
        candidate_date = parsed.date().isoformat()
        if candidate_date not in evidence_dates:
            errors.append(f"candidate.{key}の日付がevidenceにありません")
        # Tourの00:00は日付境界表現なので、根拠内の時刻までは要求しない。
        if entity_type != "tour":
            candidate_time = parsed.strftime("%H:%M")
            if candidate_time not in evidence_times:
                errors.append(f"candidate.{key}の時刻がevidenceにありません")

    if entity_type == "performance" and candidate.get("venue"):
        from tools.collect.extract import find_venue, normalize_venue

        evidence_venue = find_venue(normalized_evidence)
        if (not evidence_venue
                or normalize_venue(str(candidate["venue"]))
                != normalize_venue(evidence_venue)):
            errors.append("candidate.venueがevidenceにありません")


def _validate_datetime(
    value: Any,
    context: str,
    nullable: bool,
    errors: Optional[List[str]] = None,
) -> bool:
    target = errors if errors is not None else []
    if value is None:
        if not nullable:
            target.append(f"{context}はnullにできません")
            if errors is None:
                raise StagingValidationError(target[-1])
        return nullable
    if not isinstance(value, str):
        target.append(f"{context}はISO8601文字列である必要があります")
        if errors is None:
            raise StagingValidationError(target[-1])
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        target.append(f"{context}がISO8601ではありません")
        if errors is None:
            raise StagingValidationError(target[-1])
        return False
    if parsed.utcoffset() is None:
        target.append(f"{context}にタイムゾーンがありません")
        if errors is None:
            raise StagingValidationError(target[-1])
        return False
    return True


def _load_existing_ids(artist_dir: Path, artist_id: str, collection: str) -> set:
    payload = _load_artist_payload(artist_dir, artist_id)
    return {
        entry.get("id")
        for entry in payload.get(collection, [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _load_artist_payload(artist_dir: Path, artist_id: str) -> Dict[str, Any]:
    path = artist_dir / f"{artist_id}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def candidate_digest(record: Dict[str, Any]) -> str:
    immutable = {
        key: record.get(key)
        for key in (
            "artistId", "artistName", "action", "entityType", "confidence",
            "sourceUrl", "evidence", "reason", "candidate", "runId",
            "sourceFetchedAt", "llmProcessedAt", "model",
        )
    }
    return json_digest(immutable)


def merge_pending(pending_path: Path, new_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    existing: Dict[str, Any] = {"schemaVersion": 1, "items": []}
    if pending_path.exists():
        loaded = read_json(pending_path)
        if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
            existing = loaded
    items = existing.get("items", [])
    known = {_semantic_candidate_key(item) for item in items if isinstance(item, dict)}
    for record in new_records:
        key = _semantic_candidate_key(record)
        if key not in known:
            items.append(record)
            known.add(key)
    existing.update({"schemaVersion": 1, "generatedAt": now_iso(), "items": items})
    write_json_atomic(pending_path, existing)
    return existing


def _semantic_candidate_key(record: Dict[str, Any]) -> str:
    candidate = dict(record.get("candidate") or {})
    # 検証日時は再巡回ごとに変わるが、同じ実体のレビュー候補を増殖させない。
    candidate.pop("lastVerifiedAt", None)
    return json_digest({
        "artistId": record.get("artistId"),
        "action": record.get("action"),
        "entityType": record.get("entityType"),
        "sourceUrl": record.get("sourceUrl"),
        "candidate": candidate,
    })
