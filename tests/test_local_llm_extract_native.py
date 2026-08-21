#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "local_llm_extract_native.py"
spec = importlib.util.spec_from_file_location("extract_native", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestNativeExtractGuards(unittest.TestCase):
    def test_date_variants(self):
        self.assertTrue(mod._date_mentioned("2026年8月29日(土)11:00出演", "2026-08-29"))
        self.assertFalse(mod._date_mentioned("2026年8月29日(土)11:00出演", "2026-08-30"))

    def test_single_date_relation_is_one(self):
        self.assertEqual(mod._count_calendar_mentions("2026/08/29(土)11:00出演"), 1)

    def test_multi_date_relation_is_detected(self):
        text = "イベントは2026年8月29日、30日開催。本人は29日11:00出演。"
        self.assertGreater(mod._count_calendar_mentions(text), 1)


    def test_dot_date_is_recognized(self):
        self.assertTrue(mod._date_mentioned("2026.09.12 ワンマン", "2026-09-12"))

    def test_stale_date_parse(self):
        self.assertEqual(mod._parse_isoish_date("2023-08-14T12:00").year, 2023)

    def test_time_validation(self):
        self.assertTrue(mod._time_valid("19:30"))
        self.assertFalse(mod._time_valid("25:00"))
        self.assertTrue(mod._time_valid(None))

    def test_gpt_oss_think_auto(self):
        self.assertEqual(mod._think_value("gpt-oss:20b", "auto"), "low")
        self.assertFalse(mod._think_value("qwen3.5:9b", "auto"))


    def test_long_block_is_split(self):
        block = {"sourceUrl": "https://example.com", "text": "A" * 12000}
        parts = mod._split_long_block(block, max_block_chars=5000, overlap=300)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(x["text"]) <= 5000 for x in parts))

    def test_material_chunks_are_bounded(self):
        item = {
            "artistId": "x",
            "artistName": "X",
            "reason": "test",
            "sources": [],
            "evidenceBlocks": [
                {"sourceUrl": "https://example.com/1", "text": "A" * 4500},
                {"sourceUrl": "https://example.com/2", "text": "B" * 4500},
                {"sourceUrl": "https://example.com/3", "text": "C" * 4500},
            ],
            "changedLotteryText": [],
            "unparsedDateLines": [],
        }
        chunks = mod._material_chunks(item, max_evidence_chars=9000, max_block_chars=5000)
        self.assertEqual(len(chunks), 2)

    def test_merge_dedupes_events(self):
        ev = {
            "title": "A",
            "date": "2026-08-01",
            "venue": "V",
            "openTime": None,
            "startTime": "18:00",
            "sourceUrl": "https://example.com",
        }
        merged = mod._merge_valid_facts(
            [{"events": [dict(ev)], "ticketWindows": [], "uncertain": []},
             {"events": [dict(ev)], "ticketWindows": [], "uncertain": []}],
            "x",
            "X",
        )
        self.assertEqual(len(merged["events"]), 1)


if __name__ == "__main__":
    unittest.main()
