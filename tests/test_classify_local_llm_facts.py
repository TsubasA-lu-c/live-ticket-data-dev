#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "classify_local_llm_facts.py"
spec = importlib.util.spec_from_file_location("classifier", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestClassifier(unittest.TestCase):
    def test_same_date_venue_duplicate(self):
        data = {
            "tours": [{"id": "t1", "title": "TOUR"}],
            "performances": [{
                "id": "p1",
                "tourId": "t1",
                "venue": "横浜アリーナ",
                "performanceAt": "2026-08-20T18:00:00+09:00",
            }],
            "lotteries": [],
        }
        ev = {
            "title": "TOUR",
            "date": "2026-08-20",
            "venue": "横浜アリーナ（神奈川県）",
        }
        c = mod.classify_event(ev, data)
        self.assertEqual(c["classification"], "duplicate")

    def test_new_date_is_new(self):
        data = {"tours": [], "performances": [], "lotteries": []}
        ev = {"title": "バズリズム LIVE 2026", "date": "2026-11-08", "venue": "横浜アリーナ"}
        c = mod.classify_event(ev, data)
        self.assertEqual(c["classification"], "new")

    def test_ticket_duplicate(self):
        data = {
            "lotteries": [{
                "id": "l1",
                "type": "一般発売",
                "entryStartAt": "2026-08-15T12:00:00+09:00",
                "entryEndAt": None,
            }]
        }
        tw = {"name": "一般発売", "startAt": "2026-08-15T12:00", "endAt": None}
        c = mod.classify_ticket(tw, data)
        self.assertEqual(c["classification"], "duplicate")


    def test_tetsuya_event_is_related_member(self):
        relations = {
            "artists": {
                "larc_en_ciel": {
                    "members": [
                        {"memberId": "tetsuya", "name": "tetsuya", "aliases": ["TETSUYA", "tetsuya"]}
                    ]
                }
            }
        }
        ev = {
            "title": "TETSUYA Acoustic Live Tour 2026",
            "subjectName": "TETSUYA",
            "date": "2026-09-12",
            "venue": "Billboard Live TAIPEI",
            "relationEvidence": "TETSUYA Acoustic Live Tour 2026",
            "evidence": "TETSUYA Acoustic Live Tour 2026"
        }
        c = mod.classify_with_relation("larc_en_ciel", ev, {"tours": [], "performances": [], "lotteries": []}, relations, "event")
        self.assertEqual(c["classification"], "related_member")
        self.assertEqual(c["relation"]["memberId"], "tetsuya")

    def test_tetsuya_ticket_inferred_from_evidence(self):
        relations = {
            "artists": {
                "larc_en_ciel": {
                    "members": [
                        {"memberId": "tetsuya", "name": "tetsuya", "aliases": ["TETSUYA"]}
                    ]
                }
            }
        }
        tw = {
            "name": "CÉLUXE先行抽選",
            "subjectName": None,
            "startAt": "2026-07-11T21:00",
            "endAt": "2026-07-19T23:59",
            "evidence": "TETSUYA Official Fan Club CÉLUXE先行抽選"
        }
        c = mod.classify_with_relation("larc_en_ciel", tw, {"tours": [], "performances": [], "lotteries": []}, relations, "ticket")
        self.assertEqual(c["classification"], "related_member")

    def test_primary_group_fact_stays_primary(self):
        relations = {"artists": {"larc_en_ciel": {"members": [{"memberId": "tetsuya", "name": "tetsuya", "aliases": ["TETSUYA"]}]}}}
        ev = {"title": "35th L'Anniversary TOUR", "date": "2026-10-09", "venue": "国立代々木競技場 第一体育館"}
        relation = mod.relation_for_fact("larc_en_ciel", ev, relations, "event")
        self.assertEqual(relation["type"], "primary")

    def test_short_member_alias_not_substring_matched(self):
        relations = {"artists": {"larc_en_ciel": {"members": [{"memberId": "ken", "name": "ken", "aliases": ["ken"]}]}}}
        ev = {"title": "TOKEN FESTIVAL", "subjectName": None, "relationEvidence": "TOKEN FESTIVAL", "evidence": "TOKEN FESTIVAL"}
        relation = mod.relation_for_fact("larc_en_ciel", ev, relations, "event")
        self.assertEqual(relation["type"], "primary")


    def test_same_date_distinct_title_is_new(self):
        data = {
            "tours":[{"id":"t1","title":'友希 LIVE TOUR 2026 "NO SHOW, NO ME"'}],
            "performances":[{"id":"p1","tourId":"t1","venue":"Yogibo HOLY MOUNTAIN","performanceAt":"2026-09-19T14:30:00+09:00","eventName":'友希 LIVE TOUR 2026 "NO SHOW, NO ME"'}],
            "lotteries":[],
        }
        ev = {"title":'i☆Ris山北早紀と茜屋日海夏の「濃厚♡ぶどうじゅ~しゅ!」イベント',"date":"2026-09-19","venue":None}
        result = mod.classify_event(ev, data)
        self.assertEqual(result["classification"], "new")
        self.assertEqual(result["reason"], "SAME_DATE_DIFFERENT_TITLE")


    def test_same_date_start_time_duplicate_even_bad_venue(self):
        data = {
            "tours": [{"id":"t1","title":"SAKANAQUARIUM 2026-2027"}],
            "performances": [{
                "id":"p1","tourId":"t1","venue":"日本武道館",
                "performanceAt":"2026-09-09T18:30:00+09:00","eventName":None,
            }],
            "lotteries": [],
        }
        ev = {"date":"2026-09-09","venue":None,"startTime":"18:30","title":None}
        c=mod.classify_event(ev,data)
        self.assertEqual(c["classification"],"duplicate")
        self.assertEqual(c["reason"],"SAME_DATE_START_TIME")

    def test_iris_member_relation_from_fact_text(self):
        relations={"artists":{"iris":{"members":[{"memberId":"saki_yamakita","name":"山北早紀","aliases":["山北早紀"]}]}}}
        fact={"title":None,"relationEvidence":"【山北早紀】イベント開催決定","evidence":"山北早紀イベント"}
        r=mod.relation_for_fact("iris",fact,relations,"event")
        self.assertEqual(r["type"],"related_member")


    def test_existing_related_member_event_is_duplicate_not_related_member(self):
        data={
            "tours":[{"id":"t1","title":"Miyu Kubota Fan Meeting in Seoul"}],
            "performances":[{"id":"p1","tourId":"t1","venue":"Seoul","performanceAt":"2026-09-05T18:00:00+09:00","eventName":"Miyu Kubota Fan Meeting in Seoul"}],
            "lotteries":[],
        }
        relations={"artists":{"iris":{"members":[{"memberId":"miyu_kubota","name":"久保田未夢","aliases":["久保田未夢"]}]}}}
        fact={"date":"2026-09-05","title":"Miyu Kubota Fan Meeting in Seoul","venue":None,"startTime":None,"relationEvidence":"【久保田未夢】Miyu Kubota Fan Meeting in Seoul 開催決定","evidence":"久保田未夢"}
        c=mod.classify_with_relation("iris",fact,data,relations,"event")
        self.assertEqual(c["classification"],"duplicate")


if __name__ == "__main__":
    unittest.main()
