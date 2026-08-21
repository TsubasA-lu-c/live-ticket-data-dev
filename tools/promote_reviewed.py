#!/usr/bin/env python3
"""明示承認済みのlocal_llm候補だけを既存データへ安全に昇格する。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.local_llm_staging import (  # noqa: E402
    ENTITY_COLLECTIONS,
    PENDING_FILE,
    REPO_ROOT,
    StagingValidationError,
    candidate_digest,
    ensure_under,
    new_run_id,
    now_iso,
    read_json,
    validate_candidate,
    write_json_atomic,
)


class PromotionError(RuntimeError):
    pass


def promote(
    pending_path: Path = PENDING_FILE,
    repo_root: Path = REPO_ROOT,
    candidate_ids: Optional[Iterable[str]] = None,
    run_ids: Optional[Iterable[str]] = None,
    dry_run: bool = False,
    skip_source_accept: bool = False,
) -> Dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    pending_path = Path(pending_path)
    payload = read_json(pending_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PromotionError("review/pending.json の形式が不正です")

    id_filter = set(candidate_ids or [])
    run_filter = set(run_ids or [])
    approved = []
    for item in payload["items"]:
        if not isinstance(item, dict) or item.get("reviewStatus") != "approved":
            continue
        if id_filter and item.get("candidateDigest") not in id_filter:
            continue
        if run_filter and item.get("runId") not in run_filter:
            continue
        approved.append(item)

    if not approved:
        raise PromotionError("対象となる reviewStatus=approved の候補がありません")

    artist_dir = repo_root / "data" / "artist"
    _preflight_candidates(approved, repo_root, artist_dir)
    artist_ids = sorted({item["artistId"] for item in approved})

    pipeline = None
    if not skip_source_accept and not dry_run:
        pipeline = _prepare_source_accept(repo_root, artist_ids)

    with tempfile.TemporaryDirectory(prefix="local-llm-promote-") as temporary:
        validation_root = Path(temporary)
        shutil.copytree(repo_root / "data", validation_root / "data")
        (validation_root / "tools").mkdir()
        for script in ("validate.py", "update_manifest.py"):
            shutil.copy2(repo_root / "tools" / script, validation_root / "tools" / script)

        changed = _apply_to_artist_dir(approved, validation_root / "data" / "artist")
        manifest_result = _run_script(validation_root, "update_manifest.py")
        validation_result = _run_script(validation_root, "validate.py")
        if validation_result.returncode != 0:
            raise PromotionError(
                "既存validatorに失敗したため本番データは変更していません\n"
                + validation_result.stdout[-8000:]
            )
        if manifest_result.returncode != 0:
            raise PromotionError(
                "manifest更新に失敗したため本番データは変更していません\n"
                + manifest_result.stdout[-4000:]
            )

        result = {
            "promotionId": new_run_id(),
            "validatedAt": now_iso(),
            "dryRun": dry_run,
            "approvedCandidateCount": len(approved),
            "artists": artist_ids,
            "changedEntities": changed,
            "sourceHashesAccepted": [],
            "validation": {
                "command": "python3 tools/validate.py",
                "exitCode": validation_result.returncode,
                "output": validation_result.stdout[-12000:],
            },
        }

        if dry_run:
            return result

        # 検証済み一時コピーから、許可したartist JSONとmanifestだけを原子的に反映する。
        for artist_id in artist_ids:
            source = validation_root / "data" / "artist" / f"{artist_id}.json"
            destination = artist_dir / f"{artist_id}.json"
            write_json_atomic(destination, read_json(source))
        write_json_atomic(
            repo_root / "data" / "manifest.json",
            read_json(validation_root / "data" / "manifest.json"),
        )

    if pipeline is not None:
        accepted = pipeline.accept(artist_ids)
        pipeline.save_state()
        result["sourceHashesAccepted"] = accepted

    promoted_at = now_iso()
    promoted_digests = {item["candidateDigest"] for item in approved}
    for item in payload["items"]:
        if isinstance(item, dict) and item.get("candidateDigest") in promoted_digests:
            item["reviewStatus"] = "promoted"
            item["promotedAt"] = promoted_at
            item["promotionId"] = result["promotionId"]
    payload["generatedAt"] = promoted_at
    write_json_atomic(pending_path, payload)

    local_llm_root = repo_root / "local_llm"
    promotion_path = local_llm_root / "promotions" / f"{result['promotionId']}.json"
    ensure_under(promotion_path, local_llm_root)
    write_json_atomic(promotion_path, result)
    result["promotionPath"] = str(promotion_path)
    return result


def _preflight_candidates(
    approved: List[Dict[str, Any]], repo_root: Path, artist_dir: Path
) -> None:
    queues: Dict[str, Dict[str, Any]] = {}
    for item in approved:
        stored_digest = item.get("candidateDigest")
        if not isinstance(stored_digest, str) or candidate_digest(item) != stored_digest:
            raise PromotionError("候補内容が生成後に変更されています（candidateDigest不一致）")
        run_id = item.get("runId")
        if not isinstance(run_id, str):
            raise PromotionError("候補にrunIdがありません")
        if run_id not in queues:
            input_path = repo_root / "local_llm" / "runs" / run_id / "input.json"
            ensure_under(input_path, repo_root / "local_llm")
            run_input = read_json(input_path)
            queues[run_id] = {
                queue_item["artistId"]: queue_item
                for queue_item in run_input.get("items", [])
                if isinstance(queue_item, dict) and isinstance(queue_item.get("artistId"), str)
            }
        queue_item = queues[run_id].get(item.get("artistId"))
        if queue_item is None:
            raise PromotionError("run inputに対応するartistIdがありません")
        normalized, errors = validate_candidate(
            item,
            queue_item,
            item.get("sourceFetchedAt"),
            artist_dir=artist_dir,
        )
        if errors or normalized is None:
            raise PromotionError("候補の再検証に失敗: " + "; ".join(errors))


def _apply_to_artist_dir(
    approved: List[Dict[str, Any]], artist_dir: Path
) -> Dict[str, Dict[str, int]]:
    changed: Dict[str, Dict[str, int]] = {}
    by_artist: Dict[str, List[Dict[str, Any]]] = {}
    for item in approved:
        by_artist.setdefault(item["artistId"], []).append(item)

    for artist_id, items in by_artist.items():
        path = artist_dir / f"{artist_id}.json"
        ensure_under(path, artist_dir)
        data = read_json(path)
        if not isinstance(data, dict) or data.get("artistId") != artist_id:
            raise PromotionError(f"本番artist JSONが不正です: {artist_id}")
        counts = {"tour": 0, "performance": 0, "lottery": 0}
        for item in items:
            entity_type = item["entityType"]
            collection_name = ENTITY_COLLECTIONS[entity_type]
            collection = data.get(collection_name)
            if not isinstance(collection, list):
                raise PromotionError(f"{artist_id}.{collection_name} がarrayではありません")
            candidate = item["candidate"]
            matches = [index for index, value in enumerate(collection)
                       if isinstance(value, dict) and value.get("id") == candidate["id"]]
            if item["action"] == "add":
                if matches:
                    raise PromotionError(f"add対象IDが既に存在します: {candidate['id']}")
                collection.append(candidate)
            else:
                if len(matches) != 1:
                    raise PromotionError(f"update対象IDを一意に特定できません: {candidate['id']}")
                collection[matches[0]] = candidate
            counts[entity_type] += 1
        write_json_atomic(path, data)
        changed[artist_id] = counts
    return changed


def _run_script(root: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "tools" / script)],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _prepare_source_accept(repo_root: Path, artist_ids: List[str]) -> Any:
    from tools.collect.pipeline import Pipeline

    pipeline = Pipeline(
        state_file=repo_root / "cache" / "collect_state.json",
        pending_state_file=repo_root / "cache" / "collect_state.pending.json",
        snapshot_dir=repo_root / "cache" / "normalized",
        pending_snapshot_dir=repo_root / "cache" / "normalized_pending",
        artist_dir=repo_root / "data" / "artist",
        update_cache=True,
    )
    missing = [artist_id for artist_id in artist_ids if artist_id not in pipeline.pending]
    if missing:
        raise PromotionError(
            "source hash確定待ちに存在しないため昇格を中止します: " + ", ".join(missing)
            + "（検証目的だけなら --dry-run、特別な移行時のみ --skip-source-accept）"
        )
    return pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="承認済みlocal_llm候補を本番データへ昇格")
    parser.add_argument("--pending", type=Path, default=PENDING_FILE)
    parser.add_argument("--candidate-id", action="append", default=[])
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-source-accept",
        action="store_true",
        help="特殊な移行・テスト用。通常運用では使用しない",
    )
    args = parser.parse_args()
    try:
        result = promote(
            pending_path=args.pending,
            candidate_ids=args.candidate_id,
            run_ids=args.run_id,
            dry_run=args.dry_run,
            skip_source_accept=args.skip_source_accept,
        )
        print(
            f"検証済み候補 {result['approvedCandidateCount']}件 / "
            f"対象 {len(result['artists'])}組"
        )
        if args.dry_run:
            print("dry-runのため本番データ・hashは変更していません")
        else:
            print("昇格完了: " + ", ".join(result["artists"]))
            print("source hash確定: " + ", ".join(result["sourceHashesAccepted"]))
            print(f"記録: {result['promotionPath']}")
        return 0
    except (PromotionError, StagingValidationError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
