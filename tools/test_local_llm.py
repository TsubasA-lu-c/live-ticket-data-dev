"""ローカルLLM stagingと安全な昇格処理のテスト（外部APIは呼ばない）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.local_llm_collect import (
    LocalLLMError, _normalize_raw_candidate, _parse_chat_response, process_queue,
)
from tools.local_llm_staging import (
    candidate_digest, read_json, validate_candidate, write_json_atomic,
)
from tools.promote_reviewed import PromotionError, promote


FETCHED_AT = "2026-08-20T22:00:00+09:00"
SOURCE_URL = "https://example.com/live"
EVIDENCE = (
    "TEST TOUR 2026 2026年10月3日 日本武道館 "
    "開場17:00 開演18:00 FC先行受付"
)


def queue_item(artist_id="testartist"):
    return {
        "artistId": artist_id,
        "artistName": f"テスト {artist_id}",
        "reason": "LOTTERY_TEXT",
        "sources": [{"url": SOURCE_URL, "diff": "CHANGED"}],
        "parsedEventKeys": [],
        "existingSummary": {"tours": [], "lotteryIds": [], "knownPerformanceKeys": []},
        "changedLotteryText": [EVIDENCE],
        "unparsedDateLines": [],
        "approxChars": 500,
        "estimatedInputTokens": 450,
    }


def queue_payload(items=None):
    return {
        "generatedAt": FETCHED_AT,
        "instructions": "Web巡回禁止",
        "items": items if items is not None else [queue_item()],
    }


def tour_candidate(artist_id="testartist"):
    return {
        "action": "add",
        "entityType": "tour",
        "confidence": 0.91,
        "sourceUrl": SOURCE_URL,
        "evidence": EVIDENCE,
        "reason": "入力に受付期間が明記されている",
        "candidate": {
            "id": f"{artist_id}_tour_2026",
            "artistId": artist_id,
            "title": "TEST TOUR 2026",
            "startDate": "2026-10-03T00:00:00+09:00",
            "endDate": "2026-10-03T00:00:00+09:00",
            "prices": None,
            "source": "system",
            "sourceUrl": SOURCE_URL,
        },
    }


class FakeClient:
    def __init__(self, response_factory=None, error=None):
        self.response_factory = response_factory or (
            lambda item: {"candidates": [tour_candidate(item["artistId"])], "rejected": []}
        )
        self.error = error
        self.calls = []

    def complete(self, item, model):
        self.calls.append(item["artistId"])
        if self.error:
            raise self.error
        response = self.response_factory(item)
        return response, {
            "structuredOutput": "json_schema",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            "outputChars": 160,
            "attempts": 1,
        }


class LocalLLMCollectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.staging = self.root / "local_llm"
        self.artist_dir = self.root / "data" / "artist"
        self.artist_dir.mkdir(parents=True)
        self.addCleanup(self.temp.cleanup)

    def test_empty_queue_does_not_call_llm(self):
        client = FakeClient()
        result = process_queue(
            queue_payload([]), client, "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(result["report"]["llmCalls"], 0)
        self.assertEqual(read_json(result["runDir"] / "candidates.json")["items"], [])

    def test_safe_reject_records_classification_and_link_audit(self):
        item = queue_item()
        item["linkAudit"] = {
            "relatedLinksFound": 3,
            "relatedLinksFollowed": 1,
            "detailFetchFailed": 0,
        }
        client = FakeClient(response_factory=lambda _: {
            "candidates": [],
            "rejected": [{
                "sourceUrl": SOURCE_URL,
                "evidence": EVIDENCE,
                "reason": "会場情報が不足しています",
            }],
        })
        result = process_queue(
            queue_payload([item]), client, "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        rejected = read_json(result["runDir"] / "rejected.json")["items"][0]
        self.assertEqual(rejected["safeRejectCode"], "SAFE_REJECT_NO_VENUE")
        self.assertEqual(rejected["relatedLinksFound"], 3)
        self.assertEqual(rejected["relatedLinksFollowed"], 1)

    def test_valid_candidate_keeps_full_provenance_and_review_required(self):
        client = FakeClient()
        result = process_queue(
            queue_payload(), client, "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        candidates = read_json(result["runDir"] / "candidates.json")["items"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["sourceUrl"], SOURCE_URL)
        self.assertEqual(candidate["evidence"], EVIDENCE)
        self.assertEqual(candidate["sourceFetchedAt"], FETCHED_AT)
        self.assertEqual(candidate["runId"], result["report"]["runId"])
        self.assertEqual(candidate["reviewStatus"], "pending")
        self.assertEqual(candidate["candidate"]["lastVerifiedAt"], FETCHED_AT)
        pending = read_json(self.staging / "review" / "pending.json")
        self.assertEqual(len(pending["items"]), 1)
        self.assertEqual(pending["items"][0]["category"], "review_required")

    def test_evidence_not_in_input_is_rejected(self):
        def response(item):
            candidate = tour_candidate(item["artistId"])
            candidate["evidence"] = "入力には存在しない文章"
            return {"candidates": [candidate], "rejected": []}

        result = process_queue(
            queue_payload(), FakeClient(response), "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        self.assertEqual(result["report"]["candidateCount"], 0)
        rejected = read_json(result["runDir"] / "rejected.json")["items"]
        self.assertIn("AI queue入力に存在しません", rejected[0]["reason"])

    def test_date_not_supported_by_evidence_is_rejected(self):
        def response(item):
            candidate = tour_candidate(item["artistId"])
            candidate["candidate"]["startDate"] = "2026-12-31T00:00:00+09:00"
            return {"candidates": [candidate], "rejected": []}

        result = process_queue(
            queue_payload(), FakeClient(response), "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        self.assertEqual(result["report"]["candidateCount"], 0)
        rejected = read_json(result["runDir"] / "rejected.json")["items"]
        self.assertIn("日付がevidenceにありません", rejected[0]["reason"])

    def test_llm_timeout_is_recorded_without_touching_data(self):
        marker = self.root / "data" / "marker.json"
        marker.write_text('{"safe":true}\n', encoding="utf-8")
        before = marker.read_bytes()
        result = process_queue(
            queue_payload(), FakeClient(error=TimeoutError("timed out")),
            "test-model", self.staging, artist_dir=self.artist_dir,
        )
        self.assertEqual(result["report"]["errorCount"], 1)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(read_json(result["runDir"] / "candidates.json")["items"], [])

    def test_ten_item_poc_calls_only_the_ten_queue_items(self):
        items = [queue_item(f"artist_{index}") for index in range(10)]
        client = FakeClient()
        result = process_queue(
            queue_payload(items), client, "test-model", self.staging,
            artist_dir=self.artist_dir,
        )
        self.assertEqual(len(client.calls), 10)
        self.assertEqual(result["report"]["artistsInput"], 10)
        self.assertEqual(result["report"]["candidateCount"], 10)
        self.assertEqual(result["report"]["reportedPromptTokens"], 1000)

    def test_invalid_json_response_fails_closed(self):
        response = {"choices": [{"message": {"content": "not-json"}}]}
        with self.assertRaises(LocalLLMError):
            _parse_chat_response(response)

    def test_machine_parsed_fact_normalizes_only_schema_metadata(self):
        item = queue_item()
        item["parsedEvents"] = [{
            "status": "NEW", "existingId": None, "date": "2026-10-03",
            "venue": "日本武道館", "startTime": "18:00", "openTime": None,
            "title": "TEST TOUR 2026", "sourceUrl": SOURCE_URL,
            "evidence": EVIDENCE,
        }]
        raw = {
            "action": "add", "entityType": "performance", "confidence": 0.9,
            "sourceUrl": SOURCE_URL, "evidence": EVIDENCE, "reason": "明記",
            "candidate": {
                "id": "2026-10-03|日本武道館", "tourId": "testartist_tour_2026",
                "venue": "日本武道館", "performanceAt": "2026-10-03",
                "doorOpenAt": None, "kind": None, "eventName": "TEST TOUR 2026",
            },
        }
        value = _normalize_raw_candidate(raw, item, FETCHED_AT)
        self.assertEqual(value["candidate"]["performanceAt"], "2026-10-03T18:00:00+09:00")
        self.assertEqual(value["candidate"]["kind"], "oneman")
        self.assertRegex(value["candidate"]["id"], r"^testartist_tour_2026_20261003_1800$")

    def test_semantic_duplicate_performance_is_rejected_even_with_new_id(self):
        write_json_atomic(self.artist_dir / "testartist.json", {
            "artistId": "testartist",
            "tours": [{"id": "testartist_tour_2026", "title": "TEST TOUR 2026"}],
            "performances": [{
                "id": "existing", "tourId": "testartist_tour_2026",
                "venue": "日本武道館",
                "performanceAt": "2026-10-03T18:00:00+09:00",
            }],
            "lotteries": [],
        })
        item = queue_item()
        item["parsedEvents"] = [{
            "status": "UNCHANGED", "existingId": "existing",
            "date": "2026-10-03", "venue": "日本武道館",
            "startTime": "18:00", "openTime": None, "title": "TEST TOUR 2026",
            "sourceUrl": SOURCE_URL, "evidence": EVIDENCE,
        }]
        raw = {
            "action": "add", "entityType": "performance", "confidence": 0.9,
            "sourceUrl": SOURCE_URL, "evidence": EVIDENCE, "reason": "明記",
            "candidate": {
                "id": "different_id", "tourId": "testartist_tour_2026",
                "venue": "日本武道館",
                "performanceAt": "2026-10-03T18:00:00+09:00",
                "doorOpenAt": None, "kind": "oneman", "eventName": None,
                "source": "system", "sourceUrl": SOURCE_URL,
            },
        }
        _, errors = validate_candidate(raw, item, FETCHED_AT, self.artist_dir)
        self.assertIn("同日・同会場の既存performanceがあるためaddできません", errors)


class PromoteReviewedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / "data" / "artist").mkdir(parents=True)
        (self.root / "tools").mkdir()
        (self.root / "local_llm" / "review").mkdir(parents=True)
        (self.root / "local_llm" / "runs" / "run1").mkdir(parents=True)
        source_tools = Path(__file__).resolve().parent
        for script in ("validate.py", "update_manifest.py"):
            shutil.copy2(source_tools / script, self.root / "tools" / script)
        write_json_atomic(self.root / "data" / "artists.json", [{
            "id": "testartist", "name": "テスト", "aliases": [], "genre": "ソロ",
            "imageUrl": None, "source": "system", "sourceUrl": SOURCE_URL,
            "lastVerifiedAt": FETCHED_AT,
        }])
        write_json_atomic(self.root / "data" / "artist" / "testartist.json", {
            "artistId": "testartist", "tours": [], "performances": [],
            "lotteries": [], "appleMusicTracks": [],
        })
        subprocess.run(
            [sys.executable, str(self.root / "tools" / "update_manifest.py")],
            cwd=self.root, check=True, stdout=subprocess.DEVNULL,
        )
        write_json_atomic(self.root / "local_llm" / "runs" / "run1" / "input.json", {
            "runId": "run1", "items": [queue_item()],
        })

    def approved_record(self, candidate=None):
        record = tour_candidate()
        if candidate is not None:
            record = candidate
        record.update({
            "artistId": "testartist",
            "artistName": "テスト testartist",
            "runId": "run1",
            "sourceFetchedAt": FETCHED_AT,
            "llmProcessedAt": FETCHED_AT,
            "model": "test-model",
            "reviewStatus": "pending",
        })
        record["candidate"].setdefault("lastVerifiedAt", FETCHED_AT)
        record["candidateDigest"] = candidate_digest(record)
        record["reviewStatus"] = "approved"
        return record

    def write_pending(self, records):
        path = self.root / "local_llm" / "review" / "pending.json"
        write_json_atomic(path, {"schemaVersion": 1, "items": records})
        return path

    def test_only_approved_candidate_is_promoted_after_validation(self):
        path = self.write_pending([self.approved_record()])
        result = promote(
            pending_path=path, repo_root=self.root, skip_source_accept=True,
        )
        data = read_json(self.root / "data" / "artist" / "testartist.json")
        self.assertEqual([tour["id"] for tour in data["tours"]], ["testartist_tour_2026"])
        self.assertEqual(result["approvedCandidateCount"], 1)
        pending = read_json(path)
        self.assertEqual(pending["items"][0]["reviewStatus"], "promoted")
        self.assertTrue(Path(result["promotionPath"]).exists())

    def test_unapproved_candidate_is_never_promoted(self):
        record = self.approved_record()
        record["reviewStatus"] = "pending"
        path = self.write_pending([record])
        before = (self.root / "data" / "artist" / "testartist.json").read_bytes()
        with self.assertRaises(PromotionError):
            promote(pending_path=path, repo_root=self.root, skip_source_accept=True)
        self.assertEqual(
            (self.root / "data" / "artist" / "testartist.json").read_bytes(), before
        )

    def test_validator_failure_leaves_production_data_unchanged(self):
        broken = {
            "action": "add",
            "entityType": "performance",
            "confidence": 0.9,
            "sourceUrl": SOURCE_URL,
            "evidence": EVIDENCE,
            "reason": "テスト",
            "candidate": {
                "id": "testartist_perf_2026",
                "tourId": "missing_tour",
                "venue": "日本武道館",
                "performanceAt": "2026-10-03T18:00:00+09:00",
                "doorOpenAt": None,
                "kind": "oneman",
                "eventName": None,
                "source": "system",
                "sourceUrl": SOURCE_URL,
            },
        }
        path = self.write_pending([self.approved_record(broken)])
        data_path = self.root / "data" / "artist" / "testartist.json"
        manifest_path = self.root / "data" / "manifest.json"
        data_before = data_path.read_bytes()
        manifest_before = manifest_path.read_bytes()
        with self.assertRaises(PromotionError):
            promote(pending_path=path, repo_root=self.root, skip_source_accept=True)
        self.assertEqual(data_path.read_bytes(), data_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)


if __name__ == "__main__":
    unittest.main()
