#!/usr/bin/env python3
"""Run a GitHub-triggered local LLM research job without modifying production data."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ARTIST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ALLOWED_KINDS = {"artist_live_research"}
DEFAULT_ALLOWED_MODELS = {"gpt-oss:20b", "qwen3.5:9b"}


class JobConfigError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise JobConfigError(f"{path} のルートはobjectである必要があります")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _allowed_models() -> set[str]:
    raw = os.getenv("LOCAL_LLM_ALLOWED_MODELS", "")
    if not raw.strip():
        return set(DEFAULT_ALLOWED_MODELS)
    return {x.strip() for x in raw.split(",") if x.strip()}


def validate_request(request_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    try:
        request_path = request_path.resolve()
        jobs_root = (REPO_ROOT / "agent" / "jobs").resolve()
        request_path.relative_to(jobs_root)
    except (ValueError, OSError) as exc:
        raise JobConfigError("requestは agent/jobs/ 配下だけ許可されます") from exc

    if request_path.name != "request.json":
        raise JobConfigError("requestファイル名は request.json 固定です")

    if request.get("schemaVersion") != 1:
        raise JobConfigError("schemaVersion=1 だけ対応しています")

    job_id = str(request.get("jobId") or "")
    if not JOB_ID_RE.fullmatch(job_id):
        raise JobConfigError("jobIdが不正です")
    if request_path.parent.name != job_id:
        raise JobConfigError("jobIdと agent/jobs/<jobId>/ が一致していません")

    kind = str(request.get("kind") or "")
    if kind not in ALLOWED_KINDS:
        raise JobConfigError(f"未許可のkindです: {kind}")

    targets = request.get("targets")
    if not isinstance(targets, list) or not targets:
        raise JobConfigError("targetsは1件以上のartistId配列が必要です")
    if len(targets) > 100:
        raise JobConfigError("targetsは最大100件です")

    normalized_targets: list[str] = []
    for value in targets:
        if not isinstance(value, str) or not ARTIST_ID_RE.fullmatch(value):
            raise JobConfigError(f"artistIdが不正です: {value!r}")
        if not (REPO_ROOT / "data" / "artist" / f"{value}.json").exists():
            raise JobConfigError(f"未知のartistIdです: {value}")
        if value not in normalized_targets:
            normalized_targets.append(value)

    model = str(request.get("model") or os.getenv("LOCAL_LLM_MODEL") or "gpt-oss:20b")
    if model not in _allowed_models():
        raise JobConfigError(f"未許可のmodelです: {model}")

    history_days = request.get("historyDays", 180)
    if not isinstance(history_days, int) or not 1 <= history_days <= 365:
        raise JobConfigError("historyDaysは1..365の整数です")

    enrich_details = request.get("enrichDetails", True)
    if not isinstance(enrich_details, bool):
        raise JobConfigError("enrichDetailsはbooleanです")

    return {
        "schemaVersion": 1,
        "jobId": job_id,
        "kind": kind,
        "targets": normalized_targets,
        "model": model,
        "historyDays": history_days,
        "enrichDetails": enrich_details,
        "requestedBy": request.get("requestedBy"),
        "requestedAt": request.get("requestedAt"),
        "note": request.get("note"),
    }


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=cwd, text=True, check=False)


def _latest_run_dir(out_root: Path) -> Path | None:
    runs = out_root / "runs"
    if not runs.exists():
        return None
    dirs = [p for p in runs.iterdir() if p.is_dir()]
    return max(dirs, key=lambda p: p.name) if dirs else None


def _compact_collection(report: dict[str, Any]) -> dict[str, Any]:
    compact_artists = []
    for artist in report.get("artists", []) or []:
        if not isinstance(artist, dict):
            continue
        if not (
            artist.get("changed")
            or not artist.get("fetchOk")
            or artist.get("newEvents")
            or artist.get("missingOnSite")
            or artist.get("aiReason")
            or artist.get("warnings")
            or artist.get("errors")
        ):
            continue

        pages = []
        for page in artist.get("pages", []) or []:
            if not isinstance(page, dict):
                continue
            if not (
                page.get("liveRelatedDiff")
                or page.get("error")
                or page.get("categoryWarning")
                or page.get("events")
            ):
                continue
            pages.append({
                "url": page.get("url"),
                "finalUrl": page.get("finalUrl"),
                "pageTitle": page.get("pageTitle"),
                "status": page.get("status"),
                "diff": page.get("diff"),
                "depth": page.get("depth"),
                "liveRelatedDiff": (page.get("liveRelatedDiff") or [])[:20],
                "relatedLinks": (page.get("relatedLinks") or [])[:10],
                "error": page.get("error"),
                "categoryWarning": page.get("categoryWarning"),
            })

        compact_artists.append({
            "artistId": artist.get("artistId"),
            "fetchOk": artist.get("fetchOk"),
            "changed": artist.get("changed"),
            "parserOk": artist.get("parserOk"),
            "aiReason": artist.get("aiReason"),
            "counts": artist.get("counts"),
            "newEvents": artist.get("newEvents") or [],
            "missingOnSite": artist.get("missingOnSite") or [],
            "warnings": artist.get("warnings") or [],
            "errors": artist.get("errors") or [],
            "pages": pages[:12],
        })

    return {
        "metrics": report.get("metrics") or {},
        "errors": report.get("errors") or [],
        "queueArtists": report.get("queueArtists") or [],
        "artists": compact_artists,
    }


def _build_result(
    cfg: dict[str, Any],
    collection: dict[str, Any],
    *,
    pipeline_errors: list[dict[str, Any]],
    llm_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    metrics = collection.get("metrics") or {}
    changed_count = int(metrics.get("sitesChanged") or 0)
    fetch_failed = int(metrics.get("fetchFailed") or 0)
    has_llm_errors = bool((llm_payload or {}).get("errors"))
    needs_attention = bool(changed_count or fetch_failed or pipeline_errors or has_llm_errors)

    return {
        "schemaVersion": 1,
        "jobId": cfg["jobId"],
        "kind": cfg["kind"],
        "status": "waiting_chatgpt_review",
        "completedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request": {
            "targets": cfg["targets"],
            "model": cfg["model"],
            "historyDays": cfg["historyDays"],
            "enrichDetails": cfg["enrichDetails"],
            "requestedBy": cfg.get("requestedBy"),
            "requestedAt": cfg.get("requestedAt"),
            "note": cfg.get("note"),
        },
        "summary": {
            "artistsProcessed": metrics.get("artistsProcessed", 0),
            "sitesChanged": changed_count,
            "fetchFailed": fetch_failed,
            "newEventCandidates": metrics.get("newEventCandidates", 0),
            "aiFallbackCount": metrics.get("aiFallbackCount", 0),
            "needsAttention": needs_attention,
        },
        "collection": _compact_collection(collection),
        "localLlm": llm_payload or {
            "executed": False,
            "reason": "AI_QUEUE_EMPTY",
            "classification": {
                "schemaVersion": 2,
                "artists": [],
                "counts": {"duplicate": 0, "new": 0, "review": 0, "related_member": 0},
            },
            "errors": [],
        },
        "pipelineErrors": pipeline_errors,
        "reviewPolicy": {
            "productionDataWriteAllowed": False,
            "requirements": [
                "公式サイトまたは正規チケットサイトの直接取得結果だけを根拠にする",
                "日付・会場・受付期間・当落・入金期限を推測で補完しない",
                "missingOnSiteだけを理由に削除しない",
                "localLlm.classification の new/review/related_member を重点確認する",
                "collection.newEvents もローカルLLM未使用の機械抽出候補として確認する",
                "承認結果は同じjobディレクトリの review.json に保存する",
            ],
            "reviewVerdicts": ["approved", "needs_rework", "manual_review", "no_change"],
        },
        "safety": {"productionDataModified": False, "rawLocalLlmOutputsCommitted": False},
    }


def run_job(request_path: Path, result_path: Path | None = None) -> int:
    request_path = request_path.resolve()
    request = _read_json(request_path)
    cfg = validate_request(request_path, request)

    result_path = (result_path or (request_path.parent / "result.json")).resolve()
    if result_path.parent != request_path.parent or result_path.name != "result.json":
        raise JobConfigError("resultは同じjobディレクトリの result.json だけ許可されます")

    state_root = REPO_ROOT / "local_llm" / "actions" / "state"
    job_root = REPO_ROOT / "local_llm" / "actions" / "jobs" / cfg["jobId"]
    job_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)

    collect_report = job_root / "collect_report.json"
    queue_copy = job_root / "ai_queue.json"
    pipeline_errors: list[dict[str, Any]] = []

    collect_proc = _run([
        sys.executable,
        str(REPO_ROOT / "tools" / "collect_live_info.py"),
        *cfg["targets"],
        "--cache-root", str(state_root),
        "--report", str(collect_report),
        "--quiet",
    ])
    if collect_proc.returncode != 0:
        pipeline_errors.append({
            "stage": "collect",
            "exitCode": collect_proc.returncode,
            "message": "collect_live_info.py returned non-zero",
        })

    if not collect_report.exists():
        collection = {
            "metrics": {
                "artistsProcessed": 0,
                "sitesChanged": 0,
                "fetchFailed": len(cfg["targets"]),
                "newEventCandidates": 0,
                "aiFallbackCount": 0,
            },
            "errors": [{"message": "collect_report.json was not generated"}],
            "queueArtists": [],
            "artists": [],
        }
    else:
        collection = _read_json(collect_report)

    state_queue = state_root / "ai_queue.json"
    if state_queue.exists():
        shutil.copy2(state_queue, queue_copy)
        queue_data = _read_json(queue_copy)
    else:
        queue_data = {"generatedAt": None, "items": []}
        _write_json(queue_copy, queue_data)

    queue_items = queue_data.get("items") if isinstance(queue_data, dict) else []
    llm_payload: dict[str, Any] | None = None

    if isinstance(queue_items, list) and queue_items:
        queue_for_llm = queue_copy
        if cfg["enrichDetails"]:
            enriched = job_root / "ai_queue_enriched.json"
            proc = _run([
                sys.executable,
                str(REPO_ROOT / "tools" / "enrich_ai_queue_details.py"),
                "--queue", str(queue_copy),
                "--output", str(enriched),
            ])
            if proc.returncode == 0 and enriched.exists():
                queue_for_llm = enriched
            else:
                pipeline_errors.append({
                    "stage": "enrich",
                    "exitCode": proc.returncode,
                    "message": "detail enrichment failed; original queue used",
                })

        out_root = job_root / "extract"
        proc = _run([
            sys.executable,
            str(REPO_ROOT / "tools" / "local_llm_extract_native.py"),
            "--queue", str(queue_for_llm),
            "--out-root", str(out_root),
            "--model", cfg["model"],
            "--history-days", str(cfg["historyDays"]),
        ])

        run_dir = _latest_run_dir(out_root)
        if run_dir is None:
            pipeline_errors.append({
                "stage": "local_llm",
                "exitCode": proc.returncode,
                "message": "local LLM run directory was not generated",
            })
        else:
            facts_path = run_dir / "facts.json"
            classification_path = run_dir / "classification.json"
            if facts_path.exists():
                classify_proc = _run([
                    sys.executable,
                    str(REPO_ROOT / "tools" / "classify_local_llm_facts.py"),
                    "--facts", str(facts_path),
                    "--artist-dir", str(REPO_ROOT / "data" / "artist"),
                    "--relations", str(REPO_ROOT / "config" / "artist_relations.json"),
                    "--output", str(classification_path),
                ])
                if classify_proc.returncode != 0:
                    pipeline_errors.append({
                        "stage": "classify",
                        "exitCode": classify_proc.returncode,
                        "message": "classification failed",
                    })

            report = _read_json(run_dir / "report.json") if (run_dir / "report.json").exists() else {}
            errors_doc = _read_json(run_dir / "errors.json") if (run_dir / "errors.json").exists() else {"items": []}
            rejected_doc = _read_json(run_dir / "rejected.json") if (run_dir / "rejected.json").exists() else {"items": []}
            classification = (
                _read_json(classification_path)
                if classification_path.exists()
                else {
                    "schemaVersion": 2,
                    "artists": [],
                    "counts": {"duplicate": 0, "new": 0, "review": 0, "related_member": 0},
                }
            )
            llm_payload = {
                "executed": True,
                "model": cfg["model"],
                "runId": report.get("runId") or run_dir.name,
                "report": report,
                "classification": classification,
                "errors": (errors_doc.get("items") or [])[:50],
                "rejected": (rejected_doc.get("items") or [])[:50],
            }
            if proc.returncode != 0:
                pipeline_errors.append({
                    "stage": "local_llm",
                    "exitCode": proc.returncode,
                    "message": "local_llm_extract_native.py returned non-zero",
                })

    result = _build_result(cfg, collection, pipeline_errors=pipeline_errors, llm_payload=llm_payload)
    _write_json(result_path, result)
    print(f"ChatGPT review envelope: {result_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GitHub Actionsから安全にローカルLLM収集ジョブを実行する"
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, default=None)
    args = parser.parse_args()
    try:
        return run_job(args.request, args.result)
    except (JobConfigError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
