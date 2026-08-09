"""収集パイプラインのテスト。

実サイトは叩かない。取得層だけ差し替えて、正規化以降を検証する。
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tools.collect import diff as diffmod
from tools.collect import extract as extractmod
from tools.collect import merge as mergemod
from tools.collect import normalize as normmod
from tools.collect import targets as targetsmod
from tools.collect.fetcher import FetchResult, decode_body
from tools.collect.pipeline import Pipeline, build_queue_item

TODAY = date(2026, 8, 10)


class FakeFetcher:
    """URL → HTML（または FetchResult）を返すだけの取得層。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        value = self.pages.get(url)
        if isinstance(value, FetchResult):
            return value
        if value is None:
            return FetchResult(url=url, status=404, error="http_404")
        return FetchResult(url=url, final_url=url, status=200, text=value)


def page(rows_html, title="LIVE SCHEDULE"):
    return f"""<!doctype html><html><head><title>{title}</title>
    <script>var t = 1710000000;</script><style>.x{{color:red}}</style></head>
    <body>
      <header><nav><ul><li><a href="/">HOME</a></li><li><a href="/news">NEWS</a></li></ul></nav></header>
      <div class="ad-banner">広告バナー</div>
      <main>
        <h2>{title}</h2>
        <ul class="live-list">{rows_html}</ul>
      </main>
      <footer><p>(C) Example</p></footer>
    </body></html>"""


def row(text, href=None):
    inner = f'<a href="{href}">{text}</a>' if href else text
    return f"<li>{inner}</li>"


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "artist").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def artist_file(self, artist_id, data):
        (self.root / "artist" / f"{artist_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def pipeline(self, pages):
        return Pipeline(
            fetcher=FakeFetcher(pages),
            state_file=self.root / "state.json",
            snapshot_dir=self.root / "snap",
            artist_dir=self.root / "artist",
            today=TODAY,
        )

    def target(self, url="https://example.com/live/"):
        return targetsmod.Target(
            artist_id="testartist", artist_name="テストアーティスト",
            official_url="https://example.com/", live_url=url,
        )


# ---------------------------------------------------------------- 正規化

class NormalizeTest(unittest.TestCase):
    def test_drops_script_style_nav_footer_and_ads(self):
        html = page(row("2026年10月3日(土) 日本武道館 OPEN 17:00 START 18:00"))
        text, _ = normmod.normalize_html(html, "https://example.com/live/")
        self.assertIn("日本武道館", text)
        for noise in ("var t", "color:red", "HOME", "広告バナー", "(C) Example"):
            self.assertNotIn(noise, text)

    def test_hidden_elements_are_removed(self):
        html = '<main><p style="display:none">隠し</p><p>表示 2026年10月3日 日本武道館</p></main>'
        text, _ = normmod.normalize_html(html)
        self.assertNotIn("隠し", text)
        self.assertIn("表示", text)

    def test_repeated_lines_are_collapsed(self):
        html = "<main><ul>" + "".join(f"<li>同じ行</li>" for _ in range(5)) + "</ul></main>"
        text, _ = normmod.normalize_html(html)
        self.assertEqual(text.count("同じ行"), 1)

    def test_blocks_keep_links(self):
        html = page(row("2026年10月3日(土) 日本武道館", href="/live/123"))
        _, blocks = normmod.normalize_html(html, "https://example.com/live/")
        links = [l for b in blocks for l in b.links]
        self.assertIn("https://example.com/live/123", [u for _, u in links])

    def test_decode_shift_jis(self):
        raw = "日本武道館".encode("cp932")
        text, enc = decode_body(raw, "text/html; charset=Shift_JIS")
        self.assertEqual(text, "日本武道館")
        self.assertEqual(enc, "cp932")


# ---------------------------------------------------------------- 日付・会場

class ExtractPrimitiveTest(unittest.TestCase):
    def test_japanese_date_normalized(self):
        self.assertEqual(extractmod.find_dates("2026年10月3日(土)", today=TODAY), ["2026-10-03"])
        self.assertEqual(extractmod.find_dates("2026.10.03", today=TODAY), ["2026-10-03"])
        self.assertEqual(extractmod.find_dates("2026/10/3", today=TODAY), ["2026-10-03"])

    def test_year_omitted_resolves_to_next_occurrence(self):
        # 8/10 時点で「3/1」は翌年、「10/3」は同年
        self.assertEqual(extractmod.find_dates("3月1日(日)", today=TODAY), ["2027-03-01"])
        self.assertEqual(extractmod.find_dates("10月3日(土)", today=TODAY), ["2026-10-03"])

    def test_multi_day_notations(self):
        self.assertEqual(
            extractmod.find_dates("2026年10月3日(土)・4日(日)", today=TODAY),
            ["2026-10-03", "2026-10-04"])
        self.assertEqual(
            extractmod.find_dates("2026年10月3日(土)〜5日(月)", today=TODAY),
            ["2026-10-03", "2026-10-04", "2026-10-05"])

    def test_phone_numbers_are_not_dates(self):
        # 2026-08に刀ミュの上映館一覧で「019-622-4770」が9月6日として拾われた
        for phone in ("019-622-4770", "045-222-6222", "0570-783-018", "03-3462-2539"):
            self.assertEqual(
                extractmod.find_dates(f"フォーラム盛岡 {phone}", today=TODAY), [],
                msg=phone)

    def test_table_header_and_prose_are_not_venues(self):
        for line in ("都道府県 劇場名 電話番号",
                     "◆劇場販売 2026年10月24日(土)~",
                     "以下劇場の下記座席を特別料金にて販売いたします。",
                     "※ランダム商品はブラインド袋に入ったランダムでの販売となります。"):
            self.assertIsNone(extractmod.find_venue(line), msg=line)

    def test_message_is_not_a_venue(self):
        # 「メッセ」が「メッセージ」に誤爆して乃木坂46のページで会場になった
        self.assertIsNone(extractmod.find_venue("乃木坂46メッセージにて"))
        self.assertEqual(extractmod.find_venue("福岡県 マリンメッセ福岡A館"), "マリンメッセ福岡A館")

    def test_venue_survives_next_to_phone_number(self):
        self.assertEqual(
            extractmod.find_venue("神奈川県 横浜ブルク13 045-222-6222"), "横浜ブルク13")

    def test_invalid_date_is_ignored(self):
        self.assertEqual(extractmod.find_dates("2026年2月30日", today=TODAY), [])

    def test_times(self):
        self.assertEqual(
            extractmod.find_times("開場17:00 開演18:00"), ("17:00", "18:00"))
        self.assertEqual(
            extractmod.find_times("OPEN 17:00 / START 18:00"), ("17:00", "18:00"))
        self.assertEqual(extractmod.find_times("17:00/18:00"), ("17:00", "18:00"))

    def test_venue_and_prefecture(self):
        line = "2026年10月3日(土) [東京] 日本武道館 OPEN 17:00"
        self.assertEqual(extractmod.find_venue(line), "日本武道館")
        self.assertEqual(extractmod.find_prefecture(line, "日本武道館"), "東京都")

    def test_venue_not_guessed_from_ticket_text(self):
        self.assertIsNone(extractmod.find_venue("2026年10月3日(土) チケット一般発売開始"))

    def test_venue_normalization_absorbs_notation(self):
        self.assertEqual(
            extractmod.normalize_venue("さいたまスーパーアリーナ"),
            extractmod.normalize_venue("埼玉県 さいたま スーパーアリーナ（メインアリーナ）"))

    def test_venue_notation_tolerance(self):
        # 全角/半角・空白・括弧書き・都道府県接頭辞の違いで別公演にしない
        base = extractmod.normalize_venue("マリンメッセ福岡A館")
        for other in ("福岡県 マリンメッセ福岡Ａ館", "マリンメッセ福岡A館（メイン）",
                      "マリンメッセ福岡A館 公演"):
            self.assertEqual(extractmod.normalize_venue(other), base, msg=other)

    def test_jsonld_event(self):
        html = """<script type="application/ld+json">
        {"@type":"MusicEvent","name":"TOUR 2026","startDate":"2026-10-03T18:00:00+09:00",
         "location":{"@type":"Place","name":"さいたまスーパーアリーナ",
         "address":{"addressRegion":"埼玉県"}}}</script>"""
        events = extractmod.extract_jsonld_events(html, "https://example.com/live/")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].date, "2026-10-03")
        self.assertEqual(events[0].prefecture, "埼玉県")
        self.assertEqual(events[0].start_time, "18:00")


# ---------------------------------------------------------------- 差分

class DiffTest(unittest.TestCase):
    def test_same_content_is_no_change(self):
        result = diffmod.compare("A\nB", "A\nB")
        self.assertEqual(result.status, diffmod.NO_CHANGE)
        self.assertFalse(result.changed)

    def test_volatile_numbers_ignored(self):
        self.assertEqual(
            diffmod.compare("cache=1710000000\nLIVE", "cache=1810000000\nLIVE").status,
            diffmod.NO_CHANGE)

    def test_added_lines_are_isolated(self):
        result = diffmod.compare("A\nB\nC\nNEW", "A\nB\nC")
        self.assertEqual(result.status, diffmod.CHANGED)
        self.assertEqual(result.added, ["NEW"])
        self.assertIn("NEW", result.changed_text)


# ---------------------------------------------------------------- 突き合わせ

def existing_artist(perfs):
    return {
        "artistId": "testartist",
        "tours": [{"id": "t1", "title": "TOUR 2026"}],
        "performances": perfs,
        "lotteries": [],
    }


class MergeTest(unittest.TestCase):
    def test_new_updated_unchanged_removed(self):
        data = existing_artist([
            {"id": "p1", "venue": "日本武道館", "performanceAt": "2026-10-03T18:00:00+09:00",
             "doorOpenAt": "2026-10-03T17:00:00+09:00"},
            {"id": "p2", "venue": "大阪城ホール", "performanceAt": "2026-11-01T18:00:00+09:00"},
        ])
        extracted = [
            extractmod.ExtractedEvent(date="2026-10-03", venue="日本武道館",
                                      open_time="17:00", start_time="18:00"),
            extractmod.ExtractedEvent(date="2026-12-05", venue="Kアリーナ横浜"),
        ]
        statuses = mergemod.diff_events(extracted, data, today=TODAY)
        by_status = {s.status: s for s in statuses}
        self.assertIn(mergemod.UNCHANGED, by_status)
        self.assertEqual(by_status[mergemod.NEW].event.venue, "Kアリーナ横浜")
        self.assertEqual(by_status[mergemod.REMOVED].existing_id, "p2")

    def test_time_change_is_updated(self):
        data = existing_artist([
            {"id": "p1", "venue": "日本武道館", "performanceAt": "2026-10-03T18:00:00+09:00",
             "doorOpenAt": "2026-10-03T17:00:00+09:00"}])
        extracted = [extractmod.ExtractedEvent(date="2026-10-03", venue="日本武道館",
                                               open_time="16:30", start_time="17:30")]
        statuses = mergemod.diff_events(extracted, data, today=TODAY)
        self.assertEqual(statuses[0].status, mergemod.UPDATED)
        self.assertEqual(statuses[0].changes["startTime"], ("18:00", "17:30"))

    def test_venue_notation_change_is_not_updated(self):
        data = existing_artist([
            {"id": "p1", "venue": "さいたまスーパーアリーナ",
             "performanceAt": "2026-10-03T18:00:00+09:00"}])
        extracted = [extractmod.ExtractedEvent(date="2026-10-03",
                                               venue="さいたま スーパーアリーナ")]
        statuses = mergemod.diff_events(extracted, data, today=TODAY)
        self.assertEqual(statuses[0].status, mergemod.UNCHANGED)

    def test_past_performance_not_reported_as_removed(self):
        data = existing_artist([
            {"id": "old", "venue": "日本武道館", "performanceAt": "2026-01-03T18:00:00+09:00"}])
        statuses = mergemod.diff_events([], data, today=TODAY)
        self.assertEqual(statuses, [])

    def test_duplicate_extraction_is_collapsed(self):
        data = existing_artist([])
        ev = extractmod.ExtractedEvent(date="2026-10-03", venue="日本武道館")
        statuses = mergemod.diff_events([ev, ev], data, today=TODAY)
        self.assertEqual(len(statuses), 1)


# ---------------------------------------------------------------- AI出力の検証

class ValidateAiTest(unittest.TestCase):
    def ok_event(self, **over):
        base = {"title": "TOUR 2026", "date": "2026-10-03", "venue": "さいたまスーパーアリーナ",
                "prefecture": "埼玉県", "openTime": "17:00", "startTime": "18:00",
                "detailUrl": "https://example.com/live/123"}
        base.update(over)
        return base

    def test_accepts_valid_event(self):
        events, issues = mergemod.validate_ai_events([self.ok_event()], "テスト", today=TODAY)
        self.assertEqual(len(events), 1)
        self.assertEqual([i for i in issues if i.level == "error"], [])

    def test_rejects_bad_date_and_empty_venue(self):
        events, issues = mergemod.validate_ai_events(
            [self.ok_event(date="2026-13-40"), self.ok_event(venue="  ")], "テスト", today=TODAY)
        self.assertEqual(events, [])
        self.assertEqual({i.code for i in issues}, {"BAD_DATE", "EMPTY_VENUE"})

    def test_rejects_prose_title_and_artist_mismatch(self):
        _, issues = mergemod.validate_ai_events(
            [self.ok_event(title="チケットは明日発売となりますのでご確認ください")],
            "テスト", today=TODAY)
        self.assertIn("PROSE_TITLE", {i.code for i in issues})

        events, issues = mergemod.validate_ai_events(
            [self.ok_event(artistName="別のアーティスト")], "テスト", today=TODAY)
        self.assertEqual(events, [])
        self.assertIn("ARTIST_MISMATCH", {i.code for i in issues})

    def test_flags_past_event_and_duplicates(self):
        events, issues = mergemod.validate_ai_events(
            [self.ok_event(date="2026-01-01"), self.ok_event(), self.ok_event()],
            "テスト", today=TODAY)
        codes = {i.code for i in issues}
        self.assertIn("PAST_EVENT", codes)
        self.assertIn("DUPLICATE", codes)
        self.assertEqual(len(events), 2)

    def test_bad_time_and_url_are_nulled(self):
        payload = [self.ok_event(startTime="25:99", detailUrl="notaurl")]
        events, issues = mergemod.validate_ai_events(payload, "テスト", today=TODAY)
        self.assertIsNone(events[0]["startTime"])
        self.assertIsNone(events[0]["detailUrl"])
        self.assertIn("BAD_TIME", {i.code for i in issues})


# ---------------------------------------------------------------- パイプライン

LIVE_URL = "https://example.com/live/"


class PipelineTest(_Case):
    def test_no_change_skips_ai(self):
        html = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        self.artist_file("testartist", existing_artist([
            {"id": "p1", "venue": "日本武道館", "performanceAt": "2026-10-03T18:00:00+09:00",
             "doorOpenAt": "2026-10-03T17:00:00+09:00"}]))

        pipe = self.pipeline({LIVE_URL: html})
        pipe.run_artist(self.target())          # 初回でスナップショットを作る
        pipe.save_state()

        pipe2 = self.pipeline({LIVE_URL: html})
        outcome = pipe2.run_artist(self.target())
        self.assertTrue(outcome.fetch_ok)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.ai_reason)
        self.assertIn("NO_CHANGE", outcome.log_lines())
        self.assertIn("AI_NOT_USED", outcome.log_lines())

    def _seed(self, pipe_pages, artist_data=None):
        self.artist_file("testartist", artist_data or existing_artist([]))
        pipe = self.pipeline(pipe_pages)
        pipe.run_artist(self.target())
        pipe.save_state()

    def test_single_new_live_detected_without_ai(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        after = before.replace(
            "</ul>", row("2026年11月1日(日) [大阪] 大阪城ホール OPEN 16:00 START 17:00") + "</ul>")
        self._seed({LIVE_URL: before})

        pipe = self.pipeline({LIVE_URL: after})
        outcome = pipe.run_artist(self.target())
        self.assertTrue(outcome.changed)
        self.assertTrue(outcome.parser_ok)
        self.assertIsNone(outcome.ai_reason)
        new = [s.event for s in outcome.statuses if s.status == mergemod.NEW]
        self.assertIn("大阪城ホール", [e.venue for e in new])
        self.assertEqual([e.prefecture for e in new if e.venue == "大阪城ホール"], ["大阪府"])

    def test_multiple_new_lives(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館"))
        after = page(
            row("2026年10月3日(土) [東京] 日本武道館")
            + row("2026年11月1日(日) [大阪] 大阪城ホール")
            + row("2026年11月8日(日) [愛知] 日本ガイシホール"))
        self._seed({LIVE_URL: before})

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        new = [s.event.venue for s in outcome.statuses if s.status == mergemod.NEW]
        self.assertEqual(sorted(new), sorted(["日本ガイシホール", "大阪城ホール", "日本武道館"]))

    def test_performance_time_correction_is_updated(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        after = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 16:00 START 17:00"))
        self._seed({LIVE_URL: before}, existing_artist([
            {"id": "p1", "venue": "日本武道館", "performanceAt": "2026-10-03T18:00:00+09:00",
             "doorOpenAt": "2026-10-03T17:00:00+09:00"}]))

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        updated = [s for s in outcome.statuses if s.status == mergemod.UPDATED]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].changes["startTime"], ("18:00", "17:00"))

    def test_html_structure_change_keeps_parsing(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        after = """<main><table><tr><td>2026.10.03(土)</td><td>東京都</td>
          <td>日本武道館</td><td>OPEN 17:00 / START 18:00</td></tr></table></main>"""
        self._seed({LIVE_URL: before})

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        self.assertTrue(outcome.parser_ok)
        self.assertIn("日本武道館", [s.event.venue for s in outcome.statuses if s.event])

    def test_dt_dd_layout_finds_venue_in_next_block(self):
        html = """<main><dl>
          <dt>2026年10月3日(土)</dt><dd>東京都 / 日本武道館 OPEN 17:00 START 18:00</dd>
        </dl></main>"""
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        venues = [s.event.venue for s in outcome.statuses if s.event]
        self.assertIn("日本武道館", venues)

    def test_lottery_text_triggers_ai_with_small_payload(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        after = before.replace(
            "</ul>",
            row("FC先行受付：2026年8月20日(木)12:00〜2026年8月28日(金)23:59 "
                "当落発表 9月3日(木)15:00 入金期限 9月8日(火)23:59") + "</ul>")
        self._seed({LIVE_URL: before})

        pipe = self.pipeline({LIVE_URL: after})
        outcome = pipe.run_artist(self.target())
        self.assertEqual(outcome.ai_reason, "LOTTERY_TEXT")

        item = build_queue_item(outcome, pipe._load_artist_data("testartist"))
        self.assertTrue(item["changedLotteryText"])
        self.assertIn("FC先行受付", item["changedLotteryText"][0])
        # ページ全文ではなく変化した数行だけが載っていること
        self.assertLess(item["approxChars"], 3000)
        self.assertNotIn("日本武道館 OPEN 17:00 START 18:00",
                         "\n".join(item["changedLotteryText"]))

    def test_parser_failure_queues_ai(self):
        before = page(row("お知らせ"))
        after = page(row("2026年10月3日(土) 開催決定！詳細は後日発表"))
        self._seed({LIVE_URL: before})

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        self.assertFalse(outcome.parser_ok)
        self.assertIn(outcome.ai_reason, ("PARSER_FAILED", "LOTTERY_TEXT"))

    def test_http_404_records_error_without_ai(self):
        outcome = self.pipeline({}).run_artist(self.target())
        self.assertFalse(outcome.fetch_ok)
        self.assertIsNone(outcome.ai_reason)
        self.assertTrue(any("404" in e for e in outcome.errors))

    def test_http_500_and_timeout_are_recorded(self):
        pipe = self.pipeline({LIVE_URL: FetchResult(url=LIVE_URL, status=500, error="http_500")})
        self.assertIn("FETCH_FAILED", pipe.run_artist(self.target()).log_lines())

        pipe = self.pipeline({LIVE_URL: FetchResult(url=LIVE_URL, error="timeout")})
        outcome = pipe.run_artist(self.target())
        self.assertTrue(any("timeout" in e for e in outcome.errors))

    def test_site_down_does_not_stop_other_pages(self):
        target = targetsmod.Target(
            artist_id="testartist", artist_name="テストアーティスト",
            official_url="https://example.com/",
            live_url=LIVE_URL, news_url="https://example.com/news/")
        pages = {
            LIVE_URL: FetchResult(url=LIVE_URL, error="URLError: connection refused"),
            "https://example.com/news/": page(row("2026年10月3日(土) [東京] 日本武道館")),
            "https://example.com/": page(row("2026年10月3日(土) [東京] 日本武道館")),
        }
        outcome = self.pipeline(pages).run_artist(target)
        self.assertTrue(outcome.fetch_ok)          # 生きているページで継続できている
        self.assertTrue(outcome.errors)            # 落ちたURLは記録されている
        self.assertTrue(outcome.parser_ok)

    def test_javascript_rendered_page_is_flagged(self):
        html = '<html><body><div id="root"></div><script>render()</script></body></html>'
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        self.assertTrue(outcome.pages[0].js_suspect)
        self.assertEqual(outcome.ai_reason, "JS_RENDERED")

    def test_lost_snapshot_falls_back_to_state_hash(self):
        html = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        self.artist_file("testartist", existing_artist([]))
        pipe = self.pipeline({LIVE_URL: html})
        pipe.run_artist(self.target())
        pipe.save_state()

        # 正規化スナップショットだけ失った状態を作る（ハッシュは state に残っている）
        for path in (self.root / "snap").rglob("*.txt"):
            path.unlink()

        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.ai_reason)

    def test_empty_live_page_is_not_an_error(self):
        html = page("")
        self._seed({LIVE_URL: html})
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        self.assertTrue(outcome.fetch_ok)
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.ai_reason)

    def test_past_event_on_site_is_not_new(self):
        before = page(row("お知らせ"))
        after = page(row("2026年1月3日(土) [東京] 日本武道館"))
        self._seed({LIVE_URL: before})
        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        news = [s.event for s in outcome.statuses if s.status == mergemod.NEW]
        # 年省略なしの過去日はそのまま過去として扱われ、未来公演として増えない
        self.assertTrue(all(e.date >= "2026-08-10" for e in news))

    def test_venue_heading_applies_to_following_date_rows(self):
        # 乃木坂46型: 会場が見出しで、その下に日付が並ぶ
        html = """<main>
          <h3>横浜アリーナ</h3>
          <ul><li>2026年10月3日(土) 開場 17:00 / 開演 18:30</li>
              <li>2026年10月4日(日) 開場 16:00 / 開演 17:30</li></ul>
          <h3>大阪城ホール</h3>
          <ul><li>2026年11月1日(日) 開場 16:30 / 開演 18:00</li></ul>
        </main>"""
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        found = {(s.event.date, s.event.venue) for s in outcome.statuses if s.event}
        self.assertIn(("2026-10-03", "横浜アリーナ"), found)
        self.assertIn(("2026-10-04", "横浜アリーナ"), found)
        self.assertIn(("2026-11-01", "大阪城ホール"), found)

    def test_venue_context_does_not_leak_far(self):
        filler = "".join(f"<li>案内文{i}</li>" for i in range(8))
        html = f"""<main><h3>横浜アリーナ</h3><ul>{filler}
          <li>2026年10月3日(土) 開場 17:00 / 開演 18:30</li></ul></main>"""
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        self.assertEqual([s for s in outcome.statuses if s.event], [])

    def test_news_list_rows_do_not_become_performances(self):
        # Mrs. GREEN APPLE のnewsページ型: 記事の投稿日 + 会場名を含む記事見出し。
        # 投稿日を公演日、見出しを会場として拾ってはいけない
        html = """<main><ul>
          <li>2026.04.13</li>
          <li>≪ゼンジン未到とイ/ミュータブル〜間奏編〜≫4月 MUFGスタジアム(国立競技場)会場のSEAT MAPを公開</li>
          <li>2026.06.02</li>
          <li>≪ゼンジン未到とイ/ミュータブル〜間奏編〜≫POP-UP STORE 新宿・上野・横浜・博多、予約開始日時のご案内</li>
        </ul></main>"""
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        self.assertEqual([s for s in outcome.statuses if s.event], [])

    def test_year_inferred_dates_are_flagged_and_scored_lower(self):
        # 8月時点の「7月4日」は翌年に寄るが、過去記事の可能性がある
        flagged = extractmod.find_dates_flagged("7月4日(土) 横浜アリーナ", today=TODAY)
        self.assertEqual(flagged, [("2027-07-04", True)])
        self.assertEqual(
            extractmod.find_dates_flagged("2026年7月4日(土)", today=TODAY),
            [("2026-07-04", False)])

        html = "<main><ul><li>7月4日(土) 横浜アリーナ 開場 17:00 開演 18:30</li></ul></main>"
        outcome = self.pipeline({LIVE_URL: html}).run_artist(self.target())
        events = [s.event for s in outcome.statuses if s.event]
        self.assertTrue(events[0].year_inferred)
        self.assertLess(events[0].confidence, 0.8)

    def test_fan_club_is_not_a_venue(self):
        self.assertIsNone(extractmod.find_venue("OFFICIAL FAN CLUB 「Ringo Jam」"))

    def test_volatile_page_change_is_not_treated_as_update(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00")
                      + row("おすすめ商品A"))
        after = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00")
                     + row("おすすめ商品B"))
        self._seed({LIVE_URL: before})

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.ai_reason)
        self.assertEqual(outcome.pages[0].diff_status, diffmod.VOLATILE)

    def test_real_date_change_is_still_detected(self):
        before = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        after = before.replace(
            "</ul>", row("2026年11月1日(日) [大阪] 大阪城ホール OPEN 16:00 START 17:00") + "</ul>")
        self._seed({LIVE_URL: before})

        outcome = self.pipeline({LIVE_URL: after}).run_artist(self.target())
        self.assertTrue(outcome.changed)

    def test_ai_queued_artist_stays_pending_until_accepted(self):
        # AIに回した分の指紋を確定してしまうと、次回 NO_CHANGE になって更新が消える
        before = page(row("お知らせ"))
        after = page(row("2026年10月3日(土) FC先行受付 8月20日(木)12:00〜8月28日(金)23:59"))
        self._seed({LIVE_URL: before})

        pipe = self.pipeline({LIVE_URL: after})
        outcome = pipe.run_artist(self.target())
        pipe.save_state()
        self.assertIsNotNone(outcome.ai_reason)
        self.assertIn("testartist", pipe.pending)

        # 収集しないまま再実行しても、まだ「変更あり」として見え続ける
        pipe2 = self.pipeline({LIVE_URL: after})
        again = pipe2.run_artist(self.target())
        self.assertTrue(again.changed)
        self.assertIsNotNone(again.ai_reason)

        # 収集・validate が済んで確定させると、次回から NO_CHANGE になる
        pipe3 = self.pipeline({LIVE_URL: after})
        self.assertEqual(pipe3.accept(["testartist"]), ["testartist"])
        pipe3.save_state()
        self.assertNotIn("testartist", pipe3.pending)

        pipe4 = self.pipeline({LIVE_URL: after})
        self.assertFalse(pipe4.run_artist(self.target()).changed)

    def test_fetch_failure_is_not_confirmed(self):
        html = page(row("2026年10月3日(土) [東京] 日本武道館 OPEN 17:00 START 18:00"))
        self._seed({LIVE_URL: html})

        pipe = self.pipeline({LIVE_URL: FetchResult(url=LIVE_URL, error="timeout")})
        pipe.run_artist(self.target())
        pipe.save_state()
        self.assertIn("testartist", pipe.pending)

    def test_queue_item_excludes_full_page_and_full_history(self):
        before = page(row("お知らせ"))
        after = page(row("2026年10月3日(土) FC先行受付 8月20日(木)12:00〜8月28日(金)23:59"))
        data = existing_artist([
            {"id": f"p{i}", "venue": f"会場{i}ホール",
             "performanceAt": f"2026-09-{i:02d}T18:00:00+09:00"} for i in range(1, 30)])
        self._seed({LIVE_URL: before}, data)

        pipe = self.pipeline({LIVE_URL: after})
        outcome = pipe.run_artist(self.target())
        item = build_queue_item(outcome, data)
        self.assertLessEqual(item["approxChars"], 6000)
        self.assertLessEqual(len(item["existingSummary"]["knownPerformanceKeys"]), 40)


# ---------------------------------------------------------------- 取得設定

class TargetsTest(unittest.TestCase):
    def test_classify_prefers_list_pages_over_articles(self):
        found = targetsmod.classify([
            "https://example.com/news/detail/1234",
            "https://example.com/news/",
            "https://example.com/live/",
            "https://example.com/feed.xml",
        ], "https://example.com/")
        self.assertEqual(found["live"], "https://example.com/live/")
        self.assertEqual(found["news"], "https://example.com/news/")
        self.assertEqual(found["feed"], "https://example.com/feed.xml")

    def test_target_url_order_puts_live_first(self):
        t = targetsmod.Target(artist_id="a", official_url="https://example.com/",
                              live_url="https://example.com/live/",
                              news_url="https://example.com/news/")
        self.assertEqual(t.urls()[0], "https://example.com/live/")
        self.assertIn("https://example.com/", t.urls())


if __name__ == "__main__":
    unittest.main()
