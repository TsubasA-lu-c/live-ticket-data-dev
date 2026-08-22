import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import run_local_llm_job as job


class LocalLlmJobRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agent" / "jobs" / "job-001").mkdir(parents=True)
        (self.root / "data" / "artist").mkdir(parents=True)
        (self.root / "data" / "artist" / "yuzu.json").write_text(
            json.dumps({"artistId": "yuzu"}), encoding="utf-8"
        )
        self.request_path = self.root / "agent" / "jobs" / "job-001" / "request.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, **updates):
        value = {
            "schemaVersion": 1,
            "jobId": "job-001",
            "kind": "artist_live_research",
            "targets": ["yuzu"],
            "model": "gpt-oss:20b",
        }
        value.update(updates)
        return value

    def test_valid_request(self):
        with mock.patch.object(job, "REPO_ROOT", self.root):
            cfg = job.validate_request(self.request_path, self._request())
        self.assertEqual(cfg["targets"], ["yuzu"])
        self.assertEqual(cfg["historyDays"], 180)
        self.assertTrue(cfg["enrichDetails"])

    def test_unknown_artist_is_rejected(self):
        with mock.patch.object(job, "REPO_ROOT", self.root):
            with self.assertRaises(job.JobConfigError):
                job.validate_request(
                    self.request_path,
                    self._request(targets=["not_registered"]),
                )

    def test_arbitrary_model_is_rejected(self):
        with mock.patch.object(job, "REPO_ROOT", self.root):
            with self.assertRaises(job.JobConfigError):
                job.validate_request(
                    self.request_path,
                    self._request(model="untrusted-model"),
                )

    def test_path_must_match_job_id(self):
        bad_path = self.root / "agent" / "jobs" / "other" / "request.json"
        bad_path.parent.mkdir(parents=True)
        with mock.patch.object(job, "REPO_ROOT", self.root):
            with self.assertRaises(job.JobConfigError):
                job.validate_request(bad_path, self._request())


if __name__ == "__main__":
    unittest.main()
