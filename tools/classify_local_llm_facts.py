#!/usr/bin/env python3
"""
事実抽出結果を既存data/artistと決定論的に照合する。
本番dataは変更せず、duplicate/new/review/related_memberだけを出力。

related_member は「情報として保持するが、親グループの公演として混ぜない」ための分類。
"""

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def norm(value: Optional[str]) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[「」『』【】\[\]()（）<>＜＞・,:：;；~〜～'\"“”‘’_-]", "", s)
    s = re.sub(r"(東京都|北海道|大阪府|京都府|神奈川県|千葉県|埼玉県|兵庫県|愛知県)", "", s)
    return s


def similar(a: Optional[str], b: Optional[str]) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def existing_tour_titles(data: Dict[str, Any]) -> Dict[str, str]:
    return {t.get("id"): t.get("title") for t in data.get("tours", []) or []}


def _alias_exact(value: Optional[str], aliases) -> bool:
    nv = norm(value)
    return bool(nv and any(nv == norm(a) for a in aliases or []))


def _alias_in_text(text: str, alias: str) -> bool:
    a = norm(alias)
    t = norm(text)
    if not a or not t:
        return False
    # 短い英字名(ken等)は誤爆しやすいため本文substringでは判定しない。
    if len(a) < 4 and re.fullmatch(r"[a-z]+", a):
        return False
    return a in t


def relation_for_fact(
    artist_id: str,
    fact: Dict[str, Any],
    relations: Dict[str, Any],
    kind: str,
) -> Dict[str, Any]:
    cfg = (relations.get("artists", {}) or {}).get(artist_id) or {}
    members = cfg.get("members", []) or []
    subject = fact.get("subjectName")

    if kind == "event":
        text = " ".join(str(fact.get(k) or "") for k in (
            "title", "subjectName", "relationEvidence", "evidence"
        ))
    else:
        text = " ".join(str(fact.get(k) or "") for k in (
            "name", "subjectName", "evidence"
        ))

    for member in members:
        aliases = member.get("aliases", []) or [member.get("name")]
        aliases = [a for a in aliases if a]
        subject_match = _alias_exact(subject, aliases)
        text_match = any(_alias_in_text(text, a) for a in aliases)
        if subject_match or text_match:
            return {
                "type": "related_member",
                "parentArtistId": artist_id,
                "memberId": member.get("memberId"),
                "memberName": member.get("name"),
                "matchedBy": "subjectName" if subject_match else "factText",
            }

    return {"type": "primary", "parentArtistId": artist_id}


def classify_event(ev: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    date = ev.get("date")
    title = ev.get("title")
    venue = ev.get("venue")
    start_time = ev.get("startTime")
    tour_titles = existing_tour_titles(data)

    same_date = [
        p for p in data.get("performances", []) or []
        if str(p.get("performanceAt") or "")[:10] == date
    ]

    scored = []
    for p in same_date:
        venue_score = similar(venue, p.get("venue")) if venue else 0.0
        text_candidates = [p.get("eventName"), tour_titles.get(p.get("tourId"))]
        title_score = max((similar(title, x) for x in text_candidates), default=0.0) if title else 0.0
        scored.append((max(venue_score, title_score), venue_score, title_score, p))

    if scored:
        if start_time:
            time_matches = [
                p for p in same_date
                if str(p.get("performanceAt") or "")[11:16] == str(start_time)
            ]
            if len(time_matches) == 1:
                return {
                    "classification": "duplicate",
                    "reason": "SAME_DATE_START_TIME",
                    "matchedPerformanceId": time_matches[0].get("id"),
                    "score": 1.0,
                }

        scored.sort(key=lambda x: x[0], reverse=True)
        _, venue_score, title_score, perf = scored[0]
        if venue and venue_score >= 0.84:
            return {"classification": "duplicate", "reason": "SAME_DATE_VENUE", "matchedPerformanceId": perf.get("id"), "score": round(venue_score, 3)}
        if title and title_score >= 0.86:
            return {"classification": "duplicate", "reason": "SAME_DATE_TITLE", "matchedPerformanceId": perf.get("id"), "score": round(title_score, 3)}
        if len(same_date) == 1 and not venue and title and title_score >= 0.65:
            return {"classification": "duplicate", "reason": "SAME_DATE_SINGLE_TITLE_NEAR", "matchedPerformanceId": perf.get("id"), "score": round(title_score, 3)}
        if title and scored:
            best_title_score = max(x[2] for x in scored)
            if best_title_score < 0.35:
                return {
                    "classification": "new",
                    "reason": "SAME_DATE_DIFFERENT_TITLE",
                    "sameDatePerformanceIds": [p.get("id") for p in same_date],
                    "score": round(best_title_score, 3),
                }
        return {"classification": "review", "reason": "SAME_DATE_EXISTING_AMBIGUOUS", "sameDatePerformanceIds": [p.get("id") for p in same_date]}

    return {"classification": "new", "reason": "NO_EXISTING_SAME_DATE"}


def classify_ticket(tw: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    start = str(tw.get("startAt") or "")
    end = str(tw.get("endAt") or "")
    name = tw.get("name")

    candidates = []
    for lot in data.get("lotteries", []) or []:
        es = str(lot.get("entryStartAt") or "")
        ee = str(lot.get("entryEndAt") or "")
        if start and es[:16] == start[:16]:
            name_score = similar(name, lot.get("type")) if name else 0.0
            end_match = (not end) or (ee[:16] == end[:16])
            candidates.append((name_score, end_match, lot))

    if candidates:
        candidates.sort(key=lambda x: (x[1], x[0]), reverse=True)
        name_score, end_match, lot = candidates[0]
        if end_match and (not name or name_score >= 0.45):
            return {"classification": "duplicate", "reason": "SAME_ENTRY_WINDOW", "matchedLotteryId": lot.get("id"), "score": round(name_score, 3)}
        return {"classification": "review", "reason": "SAME_START_DIFFERENT_TYPE_OR_END", "matchedLotteryId": lot.get("id"), "score": round(name_score, 3)}

    return {"classification": "new", "reason": "NO_EXISTING_ENTRY_START"}


def classify_with_relation(
    artist_id: str,
    fact: Dict[str, Any],
    data: Dict[str, Any],
    relations: Dict[str, Any],
    kind: str,
) -> Dict[str, Any]:
    relation = relation_for_fact(artist_id, fact, relations, kind)
    base = classify_event(fact, data) if kind == "event" else classify_ticket(fact, data)
    if relation["type"] == "related_member":
        if base.get("classification") == "duplicate":
            base["relation"] = relation
            base["reason"] = "RELATED_MEMBER_" + str(base.get("reason") or "DUPLICATE")
            return base
        return {
            "classification": "related_member",
            "reason": "MEMBER_ACTIVITY_NOT_PARENT_ACTIVITY",
            "relation": relation,
            "baseClassification": base.get("classification"),
            "baseReason": base.get("reason"),
        }
    base["relation"] = relation
    return base


def main() -> int:
    ap = argparse.ArgumentParser(description="local LLM facts既存データ・メンバー関係照合")
    ap.add_argument("--facts", type=Path, required=True)
    ap.add_argument("--artist-dir", type=Path, default=Path("data/artist"))
    ap.add_argument("--relations", type=Path, default=Path("config/artist_relations.json"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    facts = load(args.facts)
    relations = load(args.relations) if args.relations.exists() else {"schemaVersion": 1, "artists": {}}
    result = {"schemaVersion": 2, "artists": []}
    counts = {"duplicate": 0, "new": 0, "review": 0, "related_member": 0}

    for item in facts.get("items", []):
        aid = item.get("artistId")
        data_path = args.artist_dir / f"{aid}.json"
        data = load(data_path) if data_path.exists() else {"artistId": aid, "tours": [], "performances": [], "lotteries": []}
        out = {"artistId": aid, "artistName": item.get("artistName"), "events": [], "ticketWindows": []}

        for ev in item.get("events", []) or []:
            c = classify_with_relation(aid, ev, data, relations, "event")
            counts[c["classification"]] += 1
            out["events"].append({"fact": ev, **c})

        for tw in item.get("ticketWindows", []) or []:
            c = classify_with_relation(aid, tw, data, relations, "ticket")
            counts[c["classification"]] += 1
            out["ticketWindows"].append({"fact": tw, **c})

        result["artists"].append(out)

    result["counts"] = counts
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"duplicate={counts['duplicate']} "
        f"new={counts['new']} "
        f"review={counts['review']} "
        f"related_member={counts['related_member']}"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
