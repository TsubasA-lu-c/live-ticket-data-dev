#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "enrich_ai_queue_details.py"
spec = importlib.util.spec_from_file_location("enricher", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestDetailEnricher(unittest.TestCase):
    def test_invalid_links(self):
        self.assertIsNone(mod.valid_href("https://example.com/news/", "javascript:void(0)"))
        self.assertIsNone(mod.valid_href("https://example.com/news/", "mailto:a@example.com"))

    def test_same_site_subdomain(self):
        self.assertTrue(mod.same_site("https://vaundy.jp/news", "https://member.vaundy.jp/news/detail/1"))

    def test_exact_title_scores_high(self):
        ref = '2026年11月8日(日)「バズリズム LIVE 2026」出演決定！'
        anchor = '2026年11月8日(日)「バズリズム LIVE 2026」出演決定！'
        score = mod.score_anchor(ref, anchor, "https://vaundy.jp/news/detail/11261")
        self.assertGreater(score, 200)

    def test_unrelated_title_low(self):
        ref = '「18th Single ひなた坂46 LIVE」開催決定！'
        anchor = 'Blu-ray発売のお知らせ'
        score = mod.score_anchor(ref, anchor, "https://example.com/news/detail/1")
        self.assertLess(score, 120)


if __name__ == "__main__":
    unittest.main()
