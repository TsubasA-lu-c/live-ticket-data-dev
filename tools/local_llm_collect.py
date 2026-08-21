#!/usr/bin/env python3
"""cache/ai_queue.json をOpenAI互換ローカルLLMでstaging候補へ変換する。

このコマンドの書き込み先は local_llm/ 配下だけで、data/ や収集hashは変更しない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.local_llm_staging import (  # noqa: E402
    LOCAL_LLM_ROOT,
    StagingValidationError,
    candidate_digest,
    ensure_under,
    merge_pending,
    new_run_id,
    now_iso,
    read_json,
    validate_candidate,
    validate_queue,
    write_json_atomic,
)


DEFAULT_QUEUE = Path("cache/ai_queue.json")
DEFAULT_TIMEOUT = 120.0

SYSTEM_PROMPT = """あなたはライブ情報の構造化器です。Web検索、Web巡回、URL生成は禁止です。
入力JSONに含まれる evidenceBlocks / parsedEvents / changedLotteryText / unparsedDateLines だけを根拠にしてください。
入力にない日付、会場、開場・開演時刻、抽選期間、URLを推測・補完しないでください。
アーティスト本人の公演か判断できない情報、参照先がない情報は candidates に入れず rejected に入れてください。
不明値は null にし、各候補の evidence には入力内の根拠テキストを完全一致で引用してください。
parsedEvents は公式HTMLを決定的パーサーで抽出した事実です。status=NEW はadd候補、UPDATED は既存IDのupdate候補、UNCHANGED は既登録なので候補を作らないでください。
parsedEventsが複数日イベントのうち対象アーティストの出演日を1日だけ示す場合、evidenceに他日程があってもparsedEventsの日付だけを候補化してください。
parsedEventKeys は互換用の要約であり、一次根拠にはしないでください。
tourの期間・タイトルがevidenceBlocksに明記され、対応するNEW公演がある場合だけtour候補を作成してください。
外部LIVE・フェス出演のNEW公演で既存tourが無い場合は、parsedEvents.titleまたは同じ公式evidenceに明記されたイベント名をそのまま使う親tourとperformanceを同時に作成し、performance.kind="fes"、eventName=イベント名としてください。
既存Summaryと同じtour、または同日・同会場のperformanceを別IDでaddしないでください。
chunkContext.includeTourCandidate=false のときtour候補を繰り返さず、requiredTourIdをperformance/lotteryのtourIdにそのまま使ってください。
候補は根拠が明確でconfidence>=0.8の場合だけcandidatesへ入れ、それ未満はrejectedへ入れてください。
rejectedは同じ理由・同じ案件を入力行ごとに列挙せず集約し、最大10件にしてください。
日時は必ず `2026-10-30T17:00:00+09:00` のようなタイムゾーン付きISO 8601にしてください。
日本国内の本人単独ツアー公演は kind="oneman"、eventName=null としてください。
出力は説明文やMarkdownを付けず、指定されたJSON objectだけにしてください。

出力形式:
{
  "candidates": [{
    "action": "add|update",
    "entityType": "tour|performance|lottery",
    "confidence": 0.0,
    "sourceUrl": "入力sourcesに存在するURL",
    "evidence": "入力内の完全一致引用",
    "reason": "候補とした理由",
    "candidate": {"既存 data/artist/*.json と同じ実体"}
  }],
  "rejected": [{
    "sourceUrl": "入力sourcesに存在するURLまたはnull",
    "evidence": "入力内の引用またはnull",
    "reason": "候補にしなかった理由"
  }]
}

candidateの規則:
- tour: id, artistId, title, startDate, endDate, prices, source, sourceUrl, lastVerifiedAt
- performance: id, tourId, venue, performanceAt, doorOpenAt, kind, eventName, source, sourceUrl, lastVerifiedAt
- lottery: id, tourId, type, entryStartAt, entryEndAt, resultAt, paymentStartAt, paymentEndAt,
  performanceIds, source, sourceUrl, lastVerifiedAt
- source は常に "system"。sourceUrl は入力sourcesからそのまま選ぶ。
- IDは既存ID規則に従う英小文字snake_case。既存Summaryに同じIDがあればupdate、なければadd。
- 1つの根拠から安全に作れない親tourや紐付け先を創作しない。その場合はrejectedへ入れる。
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"enum": ["add", "update"]},
                    "entityType": {"enum": ["tour", "performance", "lottery"]},
                    "confidence": {"type": "number", "minimum": 0.8, "maximum": 1},
                    "sourceUrl": {"type": "string"},
                    "evidence": {"type": "string"},
                    "reason": {"type": "string"},
                    "candidate": {"type": "object"},
                },
                "required": [
                    "action", "entityType", "confidence", "sourceUrl",
                    "evidence", "reason", "candidate",
                ],
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sourceUrl": {"type": ["string", "null"]},
                    "evidence": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["sourceUrl", "evidence", "reason"],
            },
        },
    },
    "required": ["candidates", "rejected"],
}


class LocalLLMError(RuntimeError):
    pass


class LocalLLMHTTPError(LocalLLMError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


@dataclass(frozen=True)
class LocalLLMConfig:
    api_root: str
    model: Optional[str]
    api_key: Optional[str]
    timeout: float
    structured_output: str
    reasoning_effort: str
    max_tokens: int

    @classmethod
    def from_env(cls) -> "LocalLLMConfig":
        base_url = os.environ.get("LOCAL_LLM_BASE_URL", "").strip()
        if not base_url:
            raise LocalLLMError("LOCAL_LLM_BASE_URL が設定されていません")
        if not base_url.startswith(("http://", "https://")):
            raise LocalLLMError("LOCAL_LLM_BASE_URL はhttp(s) URLで指定してください")
        root = base_url.rstrip("/")
        if root.endswith("/chat/completions"):
            root = root[: -len("/chat/completions")]
        elif not root.endswith("/v1"):
            root += "/v1"
        try:
            timeout = float(os.environ.get("LOCAL_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError as exc:
            raise LocalLLMError("LOCAL_LLM_TIMEOUT は秒数で指定してください") from exc
        if timeout <= 0 or timeout > 1800:
            raise LocalLLMError("LOCAL_LLM_TIMEOUT は0より大きく1800以下にしてください")
        structured = os.environ.get("LOCAL_LLM_STRUCTURED_OUTPUT", "auto").lower()
        if structured not in {"auto", "json_schema", "json_object", "none"}:
            raise LocalLLMError(
                "LOCAL_LLM_STRUCTURED_OUTPUT はauto/json_schema/json_object/noneです"
            )
        reasoning_effort = os.environ.get("LOCAL_LLM_REASONING_EFFORT", "none").lower()
        if reasoning_effort not in {"none", "low", "medium", "high"}:
            raise LocalLLMError(
                "LOCAL_LLM_REASONING_EFFORT はnone/low/medium/highです"
            )
        try:
            max_tokens = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "12000"))
        except ValueError as exc:
            raise LocalLLMError("LOCAL_LLM_MAX_TOKENS は整数で指定してください") from exc
        if max_tokens < 256 or max_tokens > 32768:
            raise LocalLLMError("LOCAL_LLM_MAX_TOKENS は256〜32768で指定してください")
        return cls(
            api_root=root,
            model=os.environ.get("LOCAL_LLM_MODEL") or None,
            api_key=os.environ.get("LOCAL_LLM_API_KEY") or None,
            timeout=timeout,
            structured_output=structured,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )


class OpenAICompatibleClient:
    def __init__(self, config: LocalLLMConfig):
        self.config = config

    def models(self) -> List[str]:
        payload = self._request("GET", f"{self.config.api_root}/models")
        models = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return models

    def resolve_model(self) -> str:
        if self.config.model:
            return self.config.model
        models = self.models()
        if not models:
            raise LocalLLMError("/v1/models がmodelを返しませんでした。LOCAL_LLM_MODELを設定してください")
        return models[0]

    def complete(self, item: Dict[str, Any], model: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        chunks = _split_item(item)
        combined = {"candidates": [], "rejected": []}
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        total_output_chars = 0
        total_prompt_chars = 0
        total_attempts = 0
        formats: List[str] = []
        required_tour_id: Optional[str] = None

        for chunk_index, chunk in enumerate(chunks):
            chunk["chunkContext"] = {
                "index": chunk_index + 1,
                "count": len(chunks),
                "includeTourCandidate": chunk_index == 0,
            }
            if required_tour_id:
                chunk["chunkContext"]["requiredTourId"] = required_tour_id
            total_prompt_chars += len(SYSTEM_PROMPT) + len(
                json.dumps(chunk, ensure_ascii=False)
            )
            parsed, meta = self._complete_once(chunk, model)
            raw_candidates = parsed.get("candidates")
            raw_rejected = parsed.get("rejected")
            if not isinstance(raw_candidates, list) or not isinstance(raw_rejected, list):
                raise LocalLLMError("LLM出力にcandidates/rejected arrayがありません")
            if chunk_index:
                # 後続chunkのtour再生成は、親IDの揺れと重複を招くため採用しない。
                raw_candidates = [
                    candidate for candidate in raw_candidates
                    if not isinstance(candidate, dict)
                    or candidate.get("entityType") != "tour"
                ]
            combined["candidates"].extend(raw_candidates)
            combined["rejected"].extend(raw_rejected)
            if required_tour_id is None:
                for candidate in raw_candidates:
                    if (isinstance(candidate, dict)
                            and candidate.get("entityType") == "tour"
                            and isinstance(candidate.get("candidate"), dict)
                            and isinstance(candidate["candidate"].get("id"), str)):
                        required_tour_id = candidate["candidate"]["id"]
                        break
            usage = meta.get("usage") or {}
            total_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            total_usage["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            total_output_chars += int(meta.get("outputChars", 0))
            total_attempts += int(meta.get("attempts", 1))
            formats.append(str(meta.get("structuredOutput", "unknown")))

        return combined, {
            "structuredOutput": ",".join(formats),
            "usage": total_usage,
            "outputChars": total_output_chars,
            "attempts": total_attempts,
            "callCount": total_attempts,
            "promptChars": total_prompt_chars,
        }

    def _complete_once(self, item: Dict[str, Any], model: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        user_content = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        base = {
            "model": model,
            "temperature": 0,
            # 構造化変換では長いthinkingより、JSON本体へcontextを使う。
            # Ollama OpenAI互換APIで対応している。必要なら環境変数で変更可能。
            "reasoning_effort": self.config.reasoning_effort,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        modes = self._response_modes()
        last_error: Optional[Exception] = None
        for index, response_format in enumerate(modes):
            for retry in range(2):
                request_payload = dict(base)
                if response_format is not None:
                    request_payload["response_format"] = response_format
                try:
                    response = self._request(
                        "POST", f"{self.config.api_root}/chat/completions", request_payload
                    )
                    parsed, output_chars = _parse_chat_response(response)
                    usage = response.get("usage") if isinstance(response, dict) else None
                    return parsed, {
                        "structuredOutput": _format_name(response_format),
                        "usage": usage if isinstance(usage, dict) else {},
                        "outputChars": output_chars,
                        "attempts": index + retry + 1,
                    }
                except LocalLLMHTTPError as exc:
                    last_error = exc
                    if index + 1 == len(modes) or exc.status not in {400, 404, 415, 422}:
                        raise
                    break
                except LocalLLMError as exc:
                    last_error = exc
                    if retry == 0:
                        continue
                    raise
        raise LocalLLMError(str(last_error or "ローカルLLM呼び出しに失敗しました"))

    def _response_modes(self) -> List[Optional[Dict[str, Any]]]:
        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "ticket_candidates",
                "strict": False,
                "schema": RESPONSE_SCHEMA,
            },
        }
        json_object = {"type": "json_object"}
        selected = self.config.structured_output
        if selected == "auto":
            return [schema, json_object, None]
        if selected == "json_schema":
            return [schema]
        if selected == "json_object":
            return [json_object]
        return [None]

    def _request(
        self, method: str, url: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise LocalLLMHTTPError(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LocalLLMError(f"接続失敗: {type(exc).__name__}: {exc}") from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LocalLLMError("API応答がJSONではありません") from exc
        if not isinstance(parsed, dict):
            raise LocalLLMError("API応答のルートがobjectではありません")
        return parsed


def _parse_chat_response(response: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LocalLLMError("chat/completions応答にchoicesがありません")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LocalLLMError("chat/completions応答にmessageがありません")
    if isinstance(message.get("parsed"), dict):
        parsed = message["parsed"]
        return parsed, len(json.dumps(parsed, ensure_ascii=False))
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str):
        raise LocalLLMError("message.contentが文字列ではありません")
    stripped = content.strip()
    if not stripped:
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        finish_reason = choices[0].get("finish_reason")
        raise LocalLLMError(
            "message.contentが空です"
            f"（finish_reason={finish_reason}, reasoningChars={len(reasoning)}）"
        )
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LocalLLMError(f"LLM出力がJSONではありません: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LocalLLMError("LLM出力のルートがobjectではありません")
    return parsed, len(content)


def _format_name(value: Optional[Dict[str, Any]]) -> str:
    if value is None:
        return "prompt_only"
    return str(value.get("type", "unknown"))


def _split_item(item: Dict[str, Any], events_per_call: int = 3) -> List[Dict[str, Any]]:
    """長いJSON応答が途中で切れないよう、公演だけを小さな固定単位に分ける。"""
    events = item.get("parsedEvents")
    actionable = [
        event for event in events or []
        if isinstance(event, dict) and event.get("status") in {"NEW", "UPDATED"}
    ]
    if (not isinstance(events, list) or len(events) <= events_per_call
            or not actionable):
        return [dict(item)]
    chunks: List[Dict[str, Any]] = []
    for start in range(0, len(events), events_per_call):
        chunk = dict(item)
        chunk["parsedEvents"] = events[start:start + events_per_call]
        chunk["parsedEventKeys"] = [
            f"{event.get('date', '')}|{event.get('venue', '')}|{event.get('title', '')}"
            for event in chunk["parsedEvents"]
            if isinstance(event, dict)
        ]
        # 抽選・未解決文は最初のcallだけで判定し、重複候補を作らせない。
        if start:
            chunk["changedLotteryText"] = []
            chunk["unparsedDateLines"] = []
        chunk["approxChars"] = len(json.dumps(chunk, ensure_ascii=False))
        chunk["estimatedInputTokens"] = round(chunk["approxChars"] * 0.9)
        chunks.append(chunk)
    return chunks


def _safe_reject_metadata(item: Dict[str, Any], reason: Any) -> Dict[str, Any]:
    text = str(reason or "")
    link_audit = item.get("linkAudit") if isinstance(item.get("linkAudit"), dict) else {}
    found = int(link_audit.get("relatedLinksFound", 0) or 0)
    followed = int(link_audit.get("relatedLinksFollowed", 0) or 0)
    fetch_failed = int(link_audit.get("detailFetchFailed", 0) or 0)
    if re.search(r"既存|重複|UNCHANGED|追加・更新対象がありません", text, re.I):
        code = "SAFE_REJECT_EXISTING_ENTITY_NO_CHANGE"
    elif re.search(r"日付|日時|performanceAt.*ありません", text, re.I):
        code = "SAFE_REJECT_NO_DATE"
    elif re.search(r"会場|venue", text, re.I):
        code = "SAFE_REJECT_NO_VENUE"
    elif re.search(r"時刻|開場|開演|doorOpenAt|startTime", text, re.I):
        code = "SAFE_REJECT_NO_TIME"
    elif re.search(r"tourId|親tour|ツアー.*紐", text, re.I):
        code = "SAFE_REJECT_NO_TOUR_MATCH"
    elif re.search(r"evidence|根拠.*一致|矛盾|競合|schema", text, re.I):
        code = "SAFE_REJECT_EVIDENCE_CONFLICT"
    elif fetch_failed:
        code = "SAFE_REJECT_DETAIL_FETCH_FAILED"
    elif found > followed:
        code = "SAFE_REJECT_DETAIL_NOT_FOUND"
    else:
        code = "SAFE_REJECT_INSUFFICIENT_EVIDENCE"
    return {
        "safeRejectCode": code,
        "relatedLinksFound": found,
        "relatedLinksFollowed": followed,
        "detailFetchFailed": fetch_failed,
    }


def process_queue(
    queue_payload: Dict[str, Any],
    client: Any,
    model: str,
    staging_root: Path = LOCAL_LLM_ROOT,
    selected_artist_ids: Optional[List[str]] = None,
    limit: Optional[int] = None,
    artist_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    queue_payload = validate_queue(queue_payload)
    root = Path(staging_root)
    run_id = new_run_id()
    run_dir = root / "runs" / run_id
    ensure_under(run_dir, root)

    items = list(queue_payload["items"])
    if selected_artist_ids:
        requested = set(selected_artist_ids)
        items = [item for item in items if item["artistId"] in requested]
        missing = requested - {item["artistId"] for item in items}
        if missing:
            raise StagingValidationError(
                "AI queueに存在しないartistIdです: " + ", ".join(sorted(missing))
            )
    if limit is not None:
        if limit < 1:
            raise StagingValidationError("--limit は1以上で指定してください")
        items = items[:limit]

    started = time.monotonic()
    input_payload = {
        "schemaVersion": 1,
        "runId": run_id,
        "capturedAt": now_iso(),
        "queueGeneratedAt": queue_payload["generatedAt"],
        "model": model,
        "instructions": queue_payload.get("instructions"),
        "items": items,
    }
    write_json_atomic(run_dir / "input.json", input_payload)

    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    request_reports: List[Dict[str, Any]] = []
    total_prompt_chars = 0
    total_output_chars = 0
    prompt_tokens_reported = 0
    completion_tokens_reported = 0

    effective_artist_dir = artist_dir or (Path(__file__).resolve().parent.parent / "data" / "artist")
    for item in items:
        llm_processed_at = now_iso()
        artist_started = time.monotonic()
        prompt_chars = len(SYSTEM_PROMPT) + len(json.dumps(item, ensure_ascii=False))
        try:
            response, request_meta = client.complete(item, model)
            total_prompt_chars += int(request_meta.get("promptChars", prompt_chars))
            request_reports.append({
                "artistId": item["artistId"],
                "elapsedSeconds": round(time.monotonic() - artist_started, 3),
                **request_meta,
            })
            total_output_chars += int(request_meta.get("outputChars", 0))
            usage = request_meta.get("usage") or {}
            prompt_tokens_reported += int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens_reported += int(usage.get("completion_tokens", 0) or 0)
            raw_candidates = response.get("candidates")
            raw_rejected = response.get("rejected")
            if not isinstance(raw_candidates, list) or not isinstance(raw_rejected, list):
                raise LocalLLMError("LLM出力にcandidates/rejected arrayがありません")

            raw_candidates = _inject_grounded_parent_tour(raw_candidates, item)

            existing_tour_ids = _existing_tour_ids(effective_artist_dir, item["artistId"])
            accepted_tour_ids = set(existing_tour_ids)
            new_tour_id: Optional[str] = None
            seen_candidate_ids = set()
            # 親tourを先に検証し、performance/lotteryが実在または同一応答内の
            # 検証済みtourだけを参照できるようにする。
            ordered_candidates = sorted(
                raw_candidates,
                key=lambda value: 0 if isinstance(value, dict)
                and value.get("entityType") == "tour" else 1,
            )
            for raw_candidate in ordered_candidates:
                raw_candidate = _normalize_raw_candidate(
                    raw_candidate,
                    item,
                    queue_payload["generatedAt"],
                    default_tour_id=new_tour_id,
                )
                normalized, candidate_errors = validate_candidate(
                    raw_candidate,
                    item,
                    queue_payload["generatedAt"],
                    artist_dir=effective_artist_dir,
                )
                if candidate_errors:
                    reject_reason = "; ".join(candidate_errors)
                    rejected.append({
                        "artistId": item["artistId"],
                        "artistName": item["artistName"],
                        "category": "schema_rejected",
                        "reason": reject_reason,
                        **_safe_reject_metadata(item, reject_reason),
                        "raw": raw_candidate,
                        "runId": run_id,
                        "llmProcessedAt": llm_processed_at,
                    })
                    continue
                assert normalized is not None
                entity = normalized["candidate"]
                candidate_id = entity.get("id")
                if candidate_id in seen_candidate_ids:
                    reject_reason = "同一LLM応答内でcandidate.idが重複しています"
                    rejected.append({
                        "artistId": item["artistId"],
                        "artistName": item["artistName"],
                        "category": "schema_rejected",
                        "reason": reject_reason,
                        **_safe_reject_metadata(item, reject_reason),
                        "raw": raw_candidate,
                        "runId": run_id,
                        "llmProcessedAt": llm_processed_at,
                    })
                    continue
                if normalized["entityType"] in {"performance", "lottery"} \
                        and entity.get("tourId") not in accepted_tour_ids:
                    reject_reason = "tourIdが既存または同一応答内の検証済みtourを参照していません"
                    rejected.append({
                        "artistId": item["artistId"],
                        "artistName": item["artistName"],
                        "category": "schema_rejected",
                        "reason": reject_reason,
                        **_safe_reject_metadata(item, reject_reason),
                        "raw": raw_candidate,
                        "runId": run_id,
                        "llmProcessedAt": llm_processed_at,
                    })
                    continue
                seen_candidate_ids.add(candidate_id)
                if normalized["entityType"] == "tour":
                    accepted_tour_ids.add(candidate_id)
                    new_tour_id = candidate_id
                normalized.update({
                    "category": "candidate",
                    "runId": run_id,
                    "sourceFetchedAt": queue_payload["generatedAt"],
                    "llmProcessedAt": llm_processed_at,
                    "model": model,
                    "reviewStatus": "pending",
                })
                normalized["candidateDigest"] = candidate_digest(normalized)
                candidates.append(normalized)

            for raw_rejection in raw_rejected:
                reject_reason = (
                    raw_rejection.get("reason")
                    if isinstance(raw_rejection, dict)
                    else "LLMが候補化しませんでした"
                )
                rejected.append({
                    "artistId": item["artistId"],
                    "artistName": item["artistName"],
                    "category": "llm_rejected",
                    "reason": reject_reason,
                    **_safe_reject_metadata(item, reject_reason),
                    "sourceUrl": raw_rejection.get("sourceUrl") if isinstance(raw_rejection, dict) else None,
                    "evidence": raw_rejection.get("evidence") if isinstance(raw_rejection, dict) else None,
                    "runId": run_id,
                    "llmProcessedAt": llm_processed_at,
                })
            if not raw_candidates and not raw_rejected:
                reject_reason = "公式根拠を既存データと照合した結果、追加・更新対象がありません"
                rejected.append({
                    "artistId": item["artistId"],
                    "artistName": item["artistName"],
                    "category": "safe_reject",
                    "reason": reject_reason,
                    **_safe_reject_metadata(item, reject_reason),
                    "sourceUrl": None,
                    "evidence": None,
                    "runId": run_id,
                    "llmProcessedAt": llm_processed_at,
                })
        except Exception as exc:  # 1組のLLM障害で他を止めず、本番には触れない。
            total_prompt_chars += prompt_chars
            errors.append({
                "artistId": item["artistId"],
                "artistName": item["artistName"],
                "category": "llm_error",
                "errorType": type(exc).__name__,
                "message": str(exc),
                "elapsedSeconds": round(time.monotonic() - artist_started, 3),
                "runId": run_id,
                "occurredAt": now_iso(),
            })

    write_json_atomic(run_dir / "candidates.json", {
        "schemaVersion": 1, "runId": run_id, "items": candidates,
    })
    write_json_atomic(run_dir / "rejected.json", {
        "schemaVersion": 1, "runId": run_id, "items": rejected,
    })
    write_json_atomic(run_dir / "errors.json", {
        "schemaVersion": 1, "runId": run_id, "items": errors,
    })

    pending_path = root / "review" / "pending.json"
    review_required = [dict(candidate, category="review_required") for candidate in candidates]
    pending = merge_pending(pending_path, review_required)
    elapsed = round(time.monotonic() - started, 3)
    report = {
        "schemaVersion": 1,
        "runId": run_id,
        "startedAt": input_payload["capturedAt"],
        "completedAt": now_iso(),
        "model": model,
        "queueGeneratedAt": queue_payload["generatedAt"],
        "artistsInput": len(items),
        "llmCalls": sum(
            int(request.get("callCount", 1)) for request in request_reports
        ) + len(errors),
        "candidateCount": len(candidates),
        "candidateCounts": {
            entity_type: sum(1 for candidate in candidates
                             if candidate.get("entityType") == entity_type)
            for entity_type in ("tour", "performance", "lottery")
        },
        "rejectedCount": len(rejected),
        "safeRejectCount": len(rejected),
        "errorCount": len(errors),
        "existingMatchedCount": sum(
            1 for item in items for event in item.get("parsedEvents", [])
            if isinstance(event, dict) and event.get("status") == "UNCHANGED"
        ),
        "duplicateExcludedCount": sum(
            1 for item in items for event in item.get("parsedEvents", [])
            if isinstance(event, dict) and event.get("status") == "UNCHANGED"
        ) + sum(
            1 for rejected_item in rejected
            if rejected_item.get("category") == "schema_rejected"
            and ("既存" in str(rejected_item.get("reason", ""))
                 or "重複" in str(rejected_item.get("reason", "")))
        ),
        "existingTourLinkedCount": sum(
            1 for item in items for event in item.get("parsedEvents", [])
            if isinstance(event, dict)
            and event.get("status") in {"UNCHANGED", "UPDATED"}
            and event.get("existingId")
        ),
        "updateCandidateCount": sum(
            1 for candidate in candidates if candidate.get("action") == "update"
        ),
        "reviewRequiredCount": len(candidates),
        "pendingTotal": len(pending.get("items", [])),
        "promptChars": total_prompt_chars,
        "estimatedPromptTokens": round(total_prompt_chars * 0.9),
        "reportedPromptTokens": prompt_tokens_reported or None,
        "reportedCompletionTokens": completion_tokens_reported or None,
        "outputChars": total_output_chars,
        "elapsedSeconds": elapsed,
        "requests": request_reports,
        "writeBoundary": "local_llm/ only",
    }
    write_json_atomic(run_dir / "report.json", report)
    return {"runDir": run_dir, "report": report}


def _existing_tour_ids(artist_dir: Path, artist_id: str) -> set:
    path = artist_dir / f"{artist_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        tour.get("id") for tour in payload.get("tours", [])
        if isinstance(tour, dict) and isinstance(tour.get("id"), str)
    }


def _normalize_raw_candidate(
    raw: Any,
    item: Dict[str, Any],
    generated_at: str,
    default_tour_id: Optional[str] = None,
) -> Any:
    """公式根拠の値は変えず、data schemaの管理形式だけを決定的に整える。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("candidate"), dict):
        return raw
    normalized = dict(raw)
    candidate = dict(raw["candidate"])
    normalized["candidate"] = candidate
    source_url = normalized.get("sourceUrl")
    candidate["source"] = "system"
    candidate["sourceUrl"] = source_url
    candidate["lastVerifiedAt"] = generated_at

    entity_type = normalized.get("entityType")
    if entity_type == "tour":
        title_key = str(candidate.get("title") or "").strip().lower()
        parsed_title_match = next((
            event for event in item.get("parsedEvents", [])
            if isinstance(event, dict)
            and event.get("sourceUrl") == source_url
            and title_key
            and title_key == str(event.get("title") or "").strip().lower()
            and isinstance(event.get("evidence"), str)
        ), None)
        if parsed_title_match is not None:
            normalized["evidence"] = parsed_title_match["evidence"]
        for key in ("startDate", "endDate"):
            value = candidate.get(key)
            if isinstance(value, str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
                candidate[key] = value + "T00:00:00+09:00"
        return normalized

    if entity_type != "performance":
        return normalized

    evidence = normalized.get("evidence")
    from tools.collect.extract import normalize_venue

    candidate_date = str(candidate.get("performanceAt") or "")[:10]
    candidate_venue = normalize_venue(str(candidate.get("venue") or ""))
    fact_match = next((
        event for event in item.get("parsedEvents", [])
        if isinstance(event, dict)
        and event.get("sourceUrl") == source_url
        and event.get("date") == candidate_date
        and candidate_venue
        and normalize_venue(str(event.get("venue") or "")) == candidate_venue
        and isinstance(event.get("evidence"), str)
    ), None)
    if fact_match is not None:
        evidence = fact_match["evidence"]
        normalized["evidence"] = evidence
    parsed = next((
        event for event in item.get("parsedEvents", [])
        if isinstance(event, dict)
        and event.get("sourceUrl") == source_url
        and isinstance(event.get("evidence"), str)
        and isinstance(evidence, str)
        and (
            event.get("evidence") == evidence
            or (len(evidence) >= 40 and event["evidence"].startswith(evidence))
        )
    ), None)
    if parsed is None:
        return normalized

    status = parsed.get("status")
    normalized["action"] = "update" if status == "UPDATED" else "add"
    if status == "UPDATED" and parsed.get("existingId"):
        candidate["id"] = parsed["existingId"]
    elif status == "NEW":
        tour_id = default_tour_id or candidate.get("tourId")
        if default_tour_id:
            # 公式evidenceから新規親tourが検証済みの場合、LLMが選んだ
            # 名称不一致の既存tourへの無理な紐付けを上書きする。
            candidate["tourId"] = default_tour_id
        if isinstance(tour_id, str):
            suffix = str(parsed.get("date") or "").replace("-", "")
            if parsed.get("startTime"):
                suffix += "_" + str(parsed["startTime"]).replace(":", "")
            if not suffix:
                suffix = hashlib.sha256(str(evidence).encode("utf-8")).hexdigest()[:10]
            candidate["id"] = f"{tour_id}_{suffix}"
    if default_tour_id and not isinstance(candidate.get("tourId"), str):
        candidate["tourId"] = default_tour_id
    candidate["venue"] = parsed.get("venue")
    event_date = parsed.get("date")
    start_time = parsed.get("startTime")
    if isinstance(event_date, str) and isinstance(start_time, str):
        candidate["performanceAt"] = f"{event_date}T{start_time}:00+09:00"
    candidate["doorOpenAt"] = (
        f"{event_date}T{parsed['openTime']}:00+09:00"
        if isinstance(event_date, str) and isinstance(parsed.get("openTime"), str)
        else None
    )
    if parsed.get("eventKind") == "fes" and parsed.get("title"):
        candidate["kind"] = "fes"
        candidate["eventName"] = parsed["title"]
    else:
        candidate["kind"] = "oneman"
        candidate["eventName"] = None
    return normalized


def _inject_grounded_parent_tour(raw_candidates: List[Any], item: Dict[str, Any]) -> List[Any]:
    """公式parsedEventsとLLMのperformanceが一致するのに親tourだけ無い場合を補う。

    タイトル・日付・URL・evidenceはすべて取得済み公式事実から作り、
    既存tour名または同一応答内tour名と一致する場合は何も追加しない。
    """
    performances = [value for value in raw_candidates if isinstance(value, dict)
                    and value.get("entityType") == "performance"]
    if not performances:
        return raw_candidates
    known_titles = {
        str(tour.get("title") or "").strip().casefold()
        for tour in item.get("existingSummary", {}).get("tours", [])
        if isinstance(tour, dict)
    }
    known_titles.update(
        str(value.get("candidate", {}).get("title") or "").strip().casefold()
        for value in raw_candidates if isinstance(value, dict)
        and value.get("entityType") == "tour" and isinstance(value.get("candidate"), dict)
    )
    parsed = [event for event in item.get("parsedEvents", []) if isinstance(event, dict)
              and event.get("status") == "NEW" and event.get("title")]
    titles = {str(event["title"]).strip() for event in parsed}
    titles = {title for title in titles
              if len(title) >= 4
              and title.casefold() not in {"live", "tour", "event", "news", "ライブ", "ニュース"}
              and re.search(r"live|tour|event|ライブ|ツアー|フェス", title, re.I)}
    missing = [title for title in sorted(titles) if title.casefold() not in known_titles]
    if len(missing) != 1:
        return raw_candidates
    title = missing[0]
    title_events = [event for event in parsed if str(event.get("title") or "").strip() == title]
    # LLMがその日付のperformanceを実際に候補化した場合だけ補完する。
    dates = {str(event.get("date")) for event in title_events}
    if not any(str(value.get("candidate", {}).get("performanceAt") or "")[:10] in dates
               for value in performances if isinstance(value.get("candidate"), dict)):
        return raw_candidates
    event_dates = sorted(date for date in dates if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date))
    source_event = title_events[0]
    if not event_dates or not source_event.get("sourceUrl") or not source_event.get("evidence"):
        return raw_candidates
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    tour_id = f"{item['artistId']}_event_{digest}"
    parent = {
        "action": "add",
        "entityType": "tour",
        "confidence": 0.9,
        "sourceUrl": source_event["sourceUrl"],
        "evidence": source_event["evidence"],
        "reason": "公式parsedEventsと一致した新規公演の親tourを補完",
        "candidate": {
            "id": tour_id,
            "artistId": item["artistId"],
            "title": title,
            "startDate": f"{event_dates[0]}T00:00:00+09:00",
            # tourのevidenceは単一公式excerptとして検証するため、
            # 別行の最終日を無理に合成せずnullに保つ。
            "endDate": None,
            "prices": None,
        },
    }
    return [parent, *raw_candidates]


def main() -> int:
    parser = argparse.ArgumentParser(description="ローカルLLMでAI queueをstaging候補へ変換")
    parser.add_argument("artist_ids", nargs="*", help="AI queue内の対象artistId")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=LOCAL_LLM_ROOT,
        help="local_llm/配下の隔離staging root",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="/v1/models疎通だけ確認する")
    parser.add_argument("--list-models", action="store_true", help="利用可能modelを一覧表示する")
    args = parser.parse_args()

    try:
        staging_root = ensure_under(args.staging_root, LOCAL_LLM_ROOT)
        if args.check or args.list_models:
            config = LocalLLMConfig.from_env()
            client = OpenAICompatibleClient(config)
            models = client.models()
            if not models:
                print("接続はできましたがmodelがありません", file=sys.stderr)
                return 2
            print("ローカルLLM接続OK")
            for model in models:
                print(f"  {model}")
            return 0

        queue = validate_queue(read_json(args.queue))
        selected = list(args.artist_ids)
        items = queue["items"]
        if selected:
            items = [item for item in items if item.get("artistId") in set(selected)]
        if args.limit is not None:
            items = items[:args.limit]

        # AI対象が0件なら、接続設定がなくても0 callのrunを追跡可能にする。
        if items:
            config = LocalLLMConfig.from_env()
            client = OpenAICompatibleClient(config)
            model = client.resolve_model()
        else:
            model = os.environ.get("LOCAL_LLM_MODEL") or "not-called"

            class NoCallClient:
                def complete(self, item: Dict[str, Any], model_name: str) -> Any:
                    raise AssertionError("空queueでLLMを呼んではいけません")

            client = NoCallClient()

        result = process_queue(
            queue,
            client,
            model,
            staging_root=staging_root,
            selected_artist_ids=selected or None,
            limit=args.limit,
        )
        report = result["report"]
        print(f"run: {report['runId']}")
        print(f"入力 {report['artistsInput']}組 / LLM {report['llmCalls']}回")
        print(
            f"候補 {report['candidateCount']} / 却下 {report['rejectedCount']} / "
            f"エラー {report['errorCount']}"
        )
        print(f"保存先: {result['runDir']}")
        print(f"レビュー待ち: {staging_root / 'review' / 'pending.json'}")
        return 2 if report["errorCount"] else 0
    except (StagingValidationError, LocalLLMError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
