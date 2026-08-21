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


    def test_semantic_venue_parentheses(self):
        self.assertTrue(
            mod._scalar_semantic_present(
                "横浜アリーナ（神奈川県）",
                ["会場：横浜アリーナ (神奈川県)"]
            )
        )

    def test_merchandise_ticket_guard(self):
        self.assertTrue(
            mod._merch_ticket_context(
                "sakanaction × km5 Cp1 受注販売。販売期間：2026年9月8日12:00〜"
            )
        )

    def test_goods_event_guard(self):
        ev = {
            "title": 'Vaundy ASIA ARENA TOUR 2026 "HORO"',
            "relationEvidence": "9月5日(土) 10:00 整理券対応時間：10:00〜13:59",
            "evidence": "グッズ会場販売のお知らせ 9月5日(土)10:00 整理券対応時間"
        }
        self.assertTrue(mod._strong_nonperformance_event_context(ev))

    def test_news_date_guard(self):
        ev = {
            "title": None,
            "venue": None,
            "openTime": None,
            "startTime": None,
            "relationEvidence": "2026.08.10 アジアツアー限定 one room会員連動企画決定!",
            "evidence": "2026.08.10 アジアツアー限定 one room会員連動企画決定!"
        }
        self.assertTrue(mod._weak_news_date_event(ev))

    def test_source_repair_prefers_detail(self):
        item = {
            "evidenceBlocks": [
                {"sourceUrl": "https://example.com/list", "text": "18th Single LIVE 開催決定"},
                {
                    "sourceUrl": "https://example.com/detail",
                    "kind": "detail_enriched",
                    "text": "2026年10月6日 開場17:00 開演18:30 会場 LaLa arena TOKYO-BAY"
                },
            ]
        }
        ev = {
            "date": "2026-10-06",
            "venue": "LaLa arena TOKYO-BAY",
            "openTime": "17:00",
            "startTime": "18:30",
            "sourceUrl": "https://example.com/list"
        }
        fixed = mod._repair_event_source(item, ev)
        self.assertEqual(fixed["sourceUrl"], "https://example.com/detail")


    def test_generic_sales_period_is_not_ticket(self):
        tw = {"name": "販売期間", "evidence": "販売期間 2026年9月8日12:00〜", "startAt": "2026-09-08T12:00", "endAt": None}
        self.assertFalse(mod._ticket_window_is_explicit(tw, "商品受注販売のお知らせ"))
        self.assertTrue(mod._ticket_is_merchandise(tw, ["sakanaction × km5 Cp1 商品受注販売。販売期間 2026年9月8日12:00〜"]))

    def test_equipment_seat_sale_is_ticket(self):
        tw = {"name": "機材席開放販売", "evidence": "機材席開放販売 8月19日18:00〜", "startAt": "2026-08-19T18:00", "endAt": None}
        self.assertTrue(mod._ticket_window_is_explicit(tw, "機材席開放販売を実施します"))

    def test_multidate_relation_can_be_disambiguated_by_line(self):
        relation = "9月26日(土) i☆Ris出演\n10月3日(土) i☆Ris出演"
        self.assertFalse(mod._relation_ambiguous_for_date(relation, "2026-09-26"))
        self.assertFalse(mod._relation_ambiguous_for_date(relation, "2026-10-03"))

    def test_multidate_relation_stays_ambiguous_in_same_clause(self):
        relation = "イベントは8月29日、8月30日開催。高城れには8月29日出演。"
        self.assertTrue(mod._relation_ambiguous_for_date(relation, "2026-08-30"))


    def test_generic_merch_sale_rejected_even_with_neighbor_ticket_context(self):
        tw = {
            "name": "販売期間",
            "evidence": "sakanaction × km5 Cp1 受注販売。販売期間：2026年9月8日12:00〜2026年10月12日23:59",
            "startAt": "2026-09-08T12:00",
            "endAt": "2026-10-12T23:59",
        }
        source = [
            "チケット一般発売のお知らせ。sakanaction × km5 Cp1 受注販売。販売期間：2026年9月8日12:00〜2026年10月12日23:59"
        ]
        self.assertTrue(mod._ticket_is_merchandise(tw, source))

    def test_ticket_sale_period_with_direct_ticket_evidence_is_not_merch(self):
        tw = {
            "name": "販売期間",
            "evidence": "チケット販売期間：2026年9月8日12:00〜2026年10月12日23:59",
        }
        self.assertFalse(mod._ticket_is_merchandise(tw, [tw["evidence"]]))

    def test_refine_machine_seat_ticket_name(self):
        tw = {
            "name": "一般発売",
            "evidence": "機材席開放販売を2026年8月19日18:00より開始します",
        }
        fixed = mod._refine_ticket_name(tw)
        self.assertEqual(fixed["name"], "機材席開放販売")

    def test_refine_stage_back_ticket_name(self):
        tw = {
            "name": "一般発売",
            "evidence": "ステージバック席追加販売は8月15日12:00より",
        }
        fixed = mod._refine_ticket_name(tw)
        self.assertEqual(fixed["name"], "ステージバック席追加販売")

    def test_member_focus_material_tetsuya(self):
        item = {
            "artistId": "larc_en_ciel",
            "artistName": "L'Arc~en~Ciel",
            "evidenceBlocks": [
                {
                    "sourceUrl": "https://example.com/tetsuya",
                    "kind": "detail_enriched",
                    "text": "TETSUYA Acoustic Live Tour 2026 2026年9月12日 Billboard Live TAIPEI",
                },
                {
                    "sourceUrl": "https://example.com/larc",
                    "text": "L'Arc~en~Ciel 35th L'Anniversary TOUR",
                },
            ],
        }
        relations = {
            "artists": {
                "larc_en_ciel": {
                    "members": [{
                        "memberId": "tetsuya",
                        "name": "tetsuya",
                        "aliases": ["TETSUYA", "tetsuya"],
                    }]
                }
            }
        }
        mats = mod._member_focus_materials(item, relations, 9000, 5000)
        self.assertEqual(len(mats), 1)
        self.assertEqual(mats[0]["focusSubject"]["memberId"], "tetsuya")
        self.assertEqual(len(mats[0]["evidenceBlocks"]), 1)

    def test_short_member_alias_can_be_skipped_for_focus(self):
        self.assertFalse(mod._member_alias_in_block(
            "ticket information and broken link",
            "ken",
            True,
        ))


    def test_refine_ticket_name_from_source_context(self):
        tw = {
            "name": "一般発売",
            "startAt": "2026-08-19T18:00",
            "evidence": "8月19日18:00より販売開始",
        }
        source = [
            "2026年8月19日 18:00より、機材席開放販売を開始いたします。"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "機材席開放販売")

    def test_refine_ignores_other_date_ticket_label(self):
        tw = {
            "name": "一般発売",
            "startAt": "2026-08-19T18:00",
            "evidence": "8月19日18:00より販売開始",
        }
        source = [
            "2026年8月15日12:00 ステージバック席追加販売。"
            "2026年8月19日18:00 機材席開放販売。"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "機材席開放販売")

    def test_cross_pass_ticket_dedupe_prefers_specific_name(self):
        tickets = [
            {
                "name": "TETSUYA Acoustic Live Tour 2026 ticket sale – pre-sale",
                "subjectName": "tetsuya",
                "startAt": "2026-07-11T21:00",
                "endAt": "2026-07-19T23:59",
                "sourceUrl": "https://example.com/t",
                "_originPass": "member_ticket_focus",
            },
            {
                "name": "TETSUYA Official Fan Club「CÉLUXE」先行抽選（PREMIUM/STANDARD共通）",
                "subjectName": "TETSUYA",
                "startAt": "2026-07-11T21:00",
                "endAt": "2026-07-19T23:59",
                "sourceUrl": "https://example.com/t",
                "_originPass": "normal",
            },
        ]
        out = mod._dedupe_cross_pass_tickets(tickets)
        self.assertEqual(len(out), 1)
        self.assertIn("CÉLUXE", out[0]["name"])

    def test_cross_pass_dedupe_does_not_merge_same_pass(self):
        tickets = [
            {
                "name": "FC先行A",
                "subjectName": "member",
                "startAt": "2026-08-01T12:00",
                "endAt": None,
                "sourceUrl": "https://example.com/t",
                "_originPass": "normal",
            },
            {
                "name": "FC先行B",
                "subjectName": "member",
                "startAt": "2026-08-01T12:00",
                "endAt": None,
                "sourceUrl": "https://example.com/t",
                "_originPass": "normal",
            },
        ]
        out = mod._dedupe_cross_pass_tickets(tickets)
        self.assertEqual(len(out), 2)

    def test_ticket_specificity_prefers_official_specific_label(self):
        a = {
            "name": "TETSUYA Acoustic Live Tour 2026 ticket sale – general sale"
        }
        b = {
            "name": "TETSUYA Official Fan Club「CÉLUXE」一般発売"
        }
        self.assertGreater(
            mod._ticket_name_specificity(b),
            mod._ticket_name_specificity(a),
        )

    def test_member_focus_material_can_be_split_into_event_task(self):
        material = {
            "focusSubject": {"memberId": "tetsuya"},
            "evidenceBlocks": [{"text": "TETSUYA 2026年9月12日 LIVE"}],
        }
        event_material = dict(material)
        event_material["focusTask"] = "events_only"
        self.assertEqual(event_material["focusTask"], "events_only")
        self.assertEqual(event_material["focusSubject"]["memberId"], "tetsuya")


    def test_date_mentioned_japanese_year_dot(self):
        self.assertTrue(mod._date_mentioned("Billboard Live YOKOHAMA 2026年9.03 Thu", "2026-09-03"))

    def test_date_mentioned_month_dot(self):
        self.assertTrue(mod._date_mentioned("2026年 日本武道館 9.08 Tue OPEN 17:30 START 18:30", "2026-09-08"))

    def test_calendar_mentions_dot_dates(self):
        self.assertEqual(mod._count_calendar_mentions("9.26 東京・10.3 神奈川"), 2)

    def test_relation_explicit_performer_clause_not_ambiguous(self):
        relation = "2026年11月6日、7日、8日開催 ※Vaundyは11月8日(日)に出演いたします"
        self.assertFalse(mod._relation_ambiguous_for_date(relation, "2026-11-08"))

    def test_source_detail_resolves_multidate_headline(self):
        source = ["【日程①】2026年9月26日（土） 【会場】東京都・アニメイト池袋本店9F北館 animate hall BLACK 【日程②】2026年10月3日（土） 【会場】神奈川県・横浜ワールドポーターズ"]
        self.assertTrue(mod._source_has_unambiguous_date_venue(source, "2026-09-26", "東京都・アニメイト池袋本店9F北館 animate hall BLACK"))

    def test_streaming_only_event_rejected(self):
        ev = {"title":"清水依与吏TikTok LIVE Premiere - Acoustic Live","venue":None,"relationEvidence":"8/8 18:30 TikTok LIVE Premiere 開催","evidence":"TikTok LIVE Premiere 生配信"}
        self.assertTrue(mod._streaming_only_event(ev))

    def test_physical_event_with_streaming_word_not_rejected(self):
        ev = {"title":"LIVE","venue":"横浜アリーナ","relationEvidence":"横浜アリーナ公演を生配信","evidence":"会場 横浜アリーナ"}
        self.assertFalse(mod._streaming_only_event(ev))

    def test_expand_billboard_two_stages(self):
        item = {"evidenceBlocks":[{"sourceUrl":"https://example.com/t","text":"Billboard Live YOKOHAMA 2026年9月03日(木) ＜1st stage＞ Open 16:30｜Start 17:30 ＜2nd stage＞ Open 19:30｜Start 20:30"}]}
        ev = {"date":"2026-09-03","venue":"Billboard Live YOKOHAMA","sourceUrl":"https://example.com/t","openTime":"16:30","startTime":"17:30"}
        out = mod._expand_explicit_same_day_stages(item, ev)
        self.assertEqual(len(out), 2)
        self.assertEqual([(x["openTime"],x["startTime"]) for x in out], [("16:30","17:30"),("19:30","20:30")])

    def test_cross_pass_event_dedupe_merges_title_and_subject(self):
        events = [
            {"date":"2026-09-12","venue":"Billboard Live TAIPEI","sourceUrl":"https://example.com/t","openTime":None,"startTime":None,"title":None,"subjectName":"tetsuya","_originPass":"member_event_focus"},
            {"date":"2026-09-12","venue":"Billboard Live TAIPEI","sourceUrl":"https://example.com/t","openTime":None,"startTime":None,"title":"TETSUYA Acoustic Live Tour 2026 at Billboard Live","subjectName":None,"_originPass":"normal"},
        ]
        out = mod._dedupe_cross_pass_events(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subjectName"], "tetsuya")
        self.assertIn("TETSUYA Acoustic", out[0]["title"])


    def test_refine_machine_seat_from_natural_headline(self):
        tw = {
            "name": "一般発売",
            "startAt": "2026-08-19T18:00",
            "evidence": "8月19日(水)18時より販売開始",
        }
        source = [
            "機材席開放につき8月19日(水)18時より販売開始"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "機材席開放販売")


    def test_ticket_year_conflict_from_evidence(self):
        self.assertTrue(mod._date_year_conflict(
            "2026-03-04",
            "チケット一般発売日2017年3月4日(土)10:00~",
            "https://example.com/tour",
            ["チケット一般発売日2017年3月4日(土)10:00~"],
        ))

    def test_ticket_year_conflict_from_fdate_url(self):
        self.assertTrue(mod._date_year_conflict(
            "2026-03-05",
            "受付期間:3/5(土)16:00~3/9(水)23:59",
            "https://example.com/tour.php?fdate=2016-04-24&ldate=2016-05-21",
            ["i☆Ris 2nd Live Tour 2016 受付期間:3/5(土)16:00~3/9(水)23:59"],
        ))

    def test_no_ticket_year_conflict_for_same_year(self):
        self.assertFalse(mod._date_year_conflict(
            "2026-08-19",
            "2026年8月19日18:00より一般発売",
            "https://example.com/t",
            ["2026年8月19日18:00より一般発売"],
        ))

    def test_non_admission_raffle_detected(self):
        tw = {
            "name":"HIGEDAN CHANCE",
            "evidence":"FC会員限定の宝くじ企画。オリジナルグッズが当たる。抽選チケット発行：2026年7月27日18:00〜。当選発表は8月下旬。",
            "startAt":"2026-07-27T18:00",
        }
        self.assertTrue(mod._ticket_is_non_admission_raffle(tw, [tw["evidence"]]))

    def test_actual_admission_lottery_not_non_admission_raffle(self):
        tw = {
            "name":"FC先行抽選",
            "evidence":"ライブ公演チケットのFC先行抽選。入場には座席指定チケットが必要です。",
            "startAt":"2026-08-20T12:00",
        }
        self.assertFalse(mod._ticket_is_non_admission_raffle(tw, [tw["evidence"]]))

    def test_news_date_with_title_is_rejected(self):
        ev = {
            "title":"Grateful Yesterdays Tour 2026",
            "venue":None,"openTime":None,"startTime":None,
            "relationEvidence":"2026.08.10 Grateful Yesterdays Tour 2026 アジアツアー限定 one room会員連動企画決定！",
            "evidence":"2026.08.10 Grateful Yesterdays Tour 2026 アジアツアー限定 one room会員連動企画決定！",
        }
        self.assertTrue(mod._weak_news_date_event(ev))

    def test_real_event_announcement_not_news_date(self):
        ev = {
            "title":"18th Single ひなた坂46 LIVE",
            "venue":None,"openTime":None,"startTime":None,
            "relationEvidence":"2026.08.10 「18th Single ひなた坂46 LIVE」開催決定！",
            "evidence":"2026.08.10 「18th Single ひなた坂46 LIVE」開催決定！",
        }
        self.assertFalse(mod._weak_news_date_event(ev))

    def test_relation_date_can_be_backed_by_event_evidence(self):
        ev = {
            "date":"2026-09-03",
            "title":"TETSUYA Acoustic Live Tour 2026 at Billboard Live",
            "venue":"Billboard Live YOKOHAMA",
            "openTime":"16:30","startTime":"17:30",
            "relationEvidence":"TETSUYA Acoustic Live Tour 2026 at Billboard Live",
            "evidence":"Billboard Live YOKOHAMA 2026年9月03日(木) 1st stage Open 16:30 Start 17:30",
        }
        self.assertTrue(mod._relation_date_backed_by_atomic_evidence(ev))

    def test_promoter_is_not_venue(self):
        self.assertTrue(mod._looks_like_promoter_not_venue("ディスクガレージ"))
        self.assertFalse(mod._looks_like_promoter_not_venue("日本武道館"))

    def test_recover_vaundy_quoted_live_title(self):
        ev = {
            "title":None,
            "relationEvidence":"2026年11月8日(日)「バズリズム LIVE 2026」出演決定！",
            "evidence":"横浜アリーナにて開催される「バズリズム LIVE 2026」にVaundyの出演が決定",
        }
        fixed=mod._recover_event_title(ev)
        self.assertEqual(fixed["title"], "バズリズム LIVE 2026")

    def test_refine_release_event_title(self):
        ev = {
            "title":"i☆Ris 13th Anniversary Live -TITLE MATCH-",
            "relationEvidence":"「i☆Ris 13th Anniversary Live -TITLE MATCH-」発売記念リリースイベント",
            "evidence":"「i☆Ris 13th Anniversary Live -TITLE MATCH-」発売記念リリースイベント",
        }
        fixed=mod._recover_event_title(ev)
        self.assertEqual(fixed["title"], "「i☆Ris 13th Anniversary Live -TITLE MATCH-」発売記念リリースイベント")


    def test_billboard_stage_pairs_do_not_cross_into_next_date(self):
        item = {"evidenceBlocks":[{"sourceUrl":"https://example.com/t","text":(
            "Billboard Live YOKOHAMA 2026年9月03日(木) "
            "＜1st stage＞ Open 16:30｜Start 17:30 ＜2nd stage＞ Open 19:30｜Start 20:30 "
            "Billboard Live OSAKA 2026年9月05日(土) "
            "＜1st stage＞ Open 14:00｜Start 15:00 ＜2nd stage＞ Open 17:00｜Start 18:00 "
            "Billboard Live TAIPEI 2026年9月12日(土) ※詳細後日発表"
        )}]}
        yoko={"date":"2026-09-03","venue":"Billboard Live YOKOHAMA","sourceUrl":"https://example.com/t"}
        osaka={"date":"2026-09-05","venue":"Billboard Live OSAKA","sourceUrl":"https://example.com/t"}
        taipei={"date":"2026-09-12","venue":"Billboard Live TAIPEI","sourceUrl":"https://example.com/t"}
        self.assertEqual(mod._explicit_stage_pairs_for_event(item,yoko),[("16:30","17:30"),("19:30","20:30")])
        self.assertEqual(mod._explicit_stage_pairs_for_event(item,osaka),[("14:00","15:00"),("17:00","18:00")])
        self.assertEqual(mod._explicit_stage_pairs_for_event(item,taipei),[])


    def test_recover_fan_meeting_title(self):
        ev={"title":None,"relationEvidence":"【久保田未夢】9/5(土)韓国ファンミーティング「Miyu Kubota Fan Meeting in Seoul」開催決定!","evidence":"Miyu Kubota Fan Meeting in Seoul"}
        fixed=mod._recover_event_title(ev)
        self.assertEqual(fixed["title"],"Miyu Kubota Fan Meeting in Seoul")

    def test_recover_quoted_member_event_title(self):
        ev={"title":None,"relationEvidence":"i☆Ris山北早紀と茜屋日海夏の「濃厚♡ぶどうじゅ~しゅ!」イベント開催決定!","evidence":"イベント開催決定"}
        fixed=mod._recover_event_title(ev)
        self.assertEqual(fixed["title"],"「濃厚♡ぶどうじゅ~しゅ!」イベント")


    def test_same_source_page_ignores_query(self):
        self.assertTrue(
            mod._same_source_page(
                "https://example.com/news/detail/E00857",
                "https://example.com/news/detail/E00857?ima=0000",
            )
        )

    def test_source_texts_matches_same_path_with_query_difference(self):
        item = {
            "evidenceBlocks": [{
                "sourceUrl": "https://example.com/news/detail/E00857?ima=0000",
                "text": "公式チケット・トレード 2026年7月8日18:00開始",
            }]
        }
        texts = mod._source_texts(
            item,
            "https://example.com/news/detail/E00857",
        )
        self.assertEqual(len(texts), 1)
        self.assertIn("チケット", texts[0])

    def test_ticket_source_repair_prefers_detail_with_start_and_end(self):
        item = {
            "evidenceBlocks": [
                {
                    "sourceUrl": "https://example.com/news/detail/OLD",
                    "text": "18th LIVE ファンクラブ抽選先行が8月20日22:00よりスタート",
                },
                {
                    "sourceUrl": "https://example.com/news/detail/NEW?ima=0000",
                    "kind": "detail_enriched",
                    "text": (
                        "18th LIVE ファンクラブ抽選先行 "
                        "受付期間 2026年8月20日22:00〜2026年9月1日23:59"
                    ),
                },
            ]
        }
        tw = {
            "name": "ファンクラブ抽選先行",
            "startAt": "2026-08-20T22:00",
            "endAt": "2026-09-01T23:59",
            "sourceUrl": "https://example.com/news/detail/OLD",
            "evidence": "8月20日22:00よりスタート",
        }
        fixed = mod._repair_ticket_source(item, tw)
        self.assertEqual(
            fixed["sourceUrl"],
            "https://example.com/news/detail/NEW?ima=0000",
        )

    def test_ticket_source_repair_for_general_sale(self):
        item = {
            "evidenceBlocks": [
                {
                    "sourceUrl": "https://example.com/wrong",
                    "text": "18th LIVE 開催決定",
                },
                {
                    "sourceUrl": "https://example.com/detail",
                    "kind": "detail_enriched",
                    "text": "チケット一般発売 2026年9月19日(土)12:00〜",
                },
            ]
        }
        tw = {
            "name": "一般発売",
            "startAt": "2026-09-19T12:00",
            "endAt": None,
            "sourceUrl": "https://example.com/wrong",
            "evidence": "一般発売 9月19日(土)12:00〜",
        }
        fixed = mod._repair_ticket_source(item, tw)
        self.assertEqual(fixed["sourceUrl"], "https://example.com/detail")

    def test_validate_ticket_survives_repaired_detail_source(self):
        item = {
            "artistId": "test",
            "artistName": "Test",
            "evidenceBlocks": [
                {
                    "sourceUrl": "https://example.com/old",
                    "text": "FC先行が8月20日22:00よりスタート",
                },
                {
                    "sourceUrl": "https://example.com/detail?ima=0000",
                    "kind": "detail_enriched",
                    "text": (
                        "チケット先行 受付期間 "
                        "2026年8月20日(木)22:00〜2026年9月1日(火)23:59 "
                        "ファンクラブ抽選先行"
                    ),
                },
            ],
        }
        parsed = {
            "events": [],
            "ticketWindows": [{
                "name": "ファンクラブ抽選先行",
                "startAt": "2026-08-20T22:00",
                "endAt": "2026-09-01T23:59",
                "sourceUrl": "https://example.com/old",
                "evidence": "ファンクラブ抽選先行 8月20日22:00よりスタート",
            }],
            "uncertain": [],
        }
        valid, rejected = mod._validate_extraction(
            item,
            parsed,
            reference_date=mod.datetime.fromisoformat("2026-08-21T12:00:00+09:00"),
            history_days=180,
        )
        self.assertEqual(len(rejected), 0)
        self.assertEqual(len(valid["ticketWindows"]), 1)
        self.assertEqual(
            valid["ticketWindows"][0]["sourceUrl"],
            "https://example.com/detail?ima=0000",
        )


    def test_general_sale_evidence_beats_neighbor_fc_context(self):
        tw = {
            "name": "一般発売",
            "startAt": "2026-09-19T12:00",
            "endAt": None,
            "evidence": "チケット一般発売が、9月19日(土)12:00〜",
        }
        source = [
            "ファンクラブ抽選先行 8月20日22:00〜9月1日23:59。"
            "オフィシャル先行 9月7日12:00〜9月9日23:59。"
            "チケット一般発売が、9月19日(土)12:00〜"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "一般発売")

    def test_official_presale_evidence_beats_neighbor_fc_context(self):
        tw = {
            "name": "受付",
            "startAt": "2026-09-07T12:00",
            "endAt": "2026-09-09T23:59",
            "evidence": "オフィシャル先行 9月7日12:00〜9月9日23:59",
        }
        source = [
            "ファンクラブ抽選先行 8月20日22:00〜9月1日23:59。"
            "オフィシャル先行 9月7日12:00〜9月9日23:59。"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "オフィシャル先行")

    def test_special_seat_context_still_refines_generic_general_sale(self):
        tw = {
            "name": "一般発売",
            "startAt": "2026-08-19T18:00",
            "endAt": None,
            "evidence": "＜一般発売日＞ 8月19日(水)18:00～",
        }
        source = [
            "機材席開放につき追加販売を実施します。"
            "＜一般発売日＞ 2026年8月19日(水)18:00～"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "機材席開放販売")

    def test_expanded_stage_gets_own_evidence(self):
        item = {
            "evidenceBlocks": [{
                "sourceUrl": "https://example.com/tetsuya",
                "kind": "detail_enriched",
                "text": (
                    "Billboard Live YOKOHAMA 2026年9月03日(木) "
                    "＜1st stage＞ Open 16:30｜Start 17:30 "
                    "＜2nd stage＞ Open 19:30｜Start 20:30 "
                    "Billboard Live OSAKA 2026年9月05日(土)"
                ),
            }]
        }
        ev = {
            "title": "TETSUYA Acoustic Live Tour 2026 at Billboard Live",
            "subjectName": "tetsuya",
            "date": "2026-09-03",
            "venue": "Billboard Live YOKOHAMA",
            "openTime": "16:30",
            "startTime": "17:30",
            "sourceUrl": "https://example.com/tetsuya",
            "relationEvidence": "TETSUYA Acoustic Live Tour 2026 at Billboard Live",
            "evidence": "＜1st stage＞ Open 16:30｜Start 17:30",
        }
        out = mod._expand_explicit_same_day_stages(item, ev)
        self.assertEqual(len(out), 2)
        self.assertIn("1st stage", out[0]["evidence"])
        self.assertIn("16:30", out[0]["evidence"])
        self.assertIn("2nd stage", out[1]["evidence"])
        self.assertIn("20:30", out[1]["evidence"])
        self.assertNotIn("1st stage", out[1]["evidence"])


    def test_general_sale_evidence_repairs_wrong_fc_label(self):
        tw = {
            "name": "ファンクラブ抽選先行",
            "startAt": "2026-09-19T12:00",
            "endAt": None,
            "evidence": "チケット一般発売が、9月19日(土)12:00〜",
        }
        source = [
            "ファンクラブ抽選先行 8月20日22:00〜9月1日23:59。"
            "チケット一般発売が、9月19日(土)12:00〜"
        ]
        fixed = mod._refine_ticket_name(tw, source)
        self.assertEqual(fixed["name"], "一般発売")
        self.assertEqual(fixed["_nameRefinedFrom"], "ファンクラブ抽選先行")


if __name__ == "__main__":
    unittest.main()
