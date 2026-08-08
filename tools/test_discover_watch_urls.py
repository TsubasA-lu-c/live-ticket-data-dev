#!/usr/bin/env python3
"""discover_watch_urls.py の候補選びの確認。

**個別記事だけで枠が埋まると、公演日程のページを永久に見に行かない。**
BUMP OF CHICKEN のTOPページはニュース記事を7本並べており、以前はその3本で
枠が尽きて /live_information が一度も登録されなかった（2026-08 に発覚）。
"""
import unittest

from tools.discover_watch_urls import extract_candidates


def page(hrefs):
    body = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><body>{body}</body></html>"


class ExtractCandidatesTests(unittest.TestCase):

    SOURCE = "https://example.jp/"

    def test_live_page_survives_a_flood_of_news_articles(self):
        html = page(
            [f"/news/news/{i}" for i in range(3700, 3710)] + ["/live_information"]
        )
        found = extract_candidates(html, self.SOURCE)
        self.assertIn("https://example.jp/live_information", found)

    def test_articles_are_capped(self):
        html = page([f"/news/news/{i}" for i in range(3700, 3710)] + ["/live_information"])
        found = extract_candidates(html, self.SOURCE)
        articles = [u for u in found if u.rstrip("/").rsplit("/", 1)[-1].isdigit()]
        self.assertLessEqual(len(articles), 2)

    def test_index_beats_article_within_same_tier(self):
        html = page(["/news/news/3757", "/news/"])
        found = extract_candidates(html, self.SOURCE)
        self.assertEqual(found[0], "https://example.jp/news/")

    def test_keeps_plain_index_pages(self):
        html = page(["/news/", "/live/", "/schedule/"])
        found = extract_candidates(html, self.SOURCE)
        self.assertEqual(
            found,
            [
                "https://example.jp/news/",
                "https://example.jp/live/",
                "https://example.jp/schedule/",
            ],
        )

    def test_ignores_unrelated_links(self):
        html = page(["/profile", "/goods", "/privacy"])
        self.assertEqual(extract_candidates(html, self.SOURCE), [])

    def test_ignores_other_hosts(self):
        html = page(["https://twitter.com/example/news", "/news/"])
        found = extract_candidates(html, self.SOURCE)
        self.assertEqual(found, ["https://example.jp/news/"])


if __name__ == "__main__":
    unittest.main()
