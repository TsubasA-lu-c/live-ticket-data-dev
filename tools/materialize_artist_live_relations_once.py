#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path("data")
DISPLAY = "relationDisplay"
MIRROR = "relationMirror"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mirror_id(target: str, source_id: str) -> str:
    return f"{target}__rel__{source_id}"


def label(name: str, title: str) -> str:
    prefix = f"【{name}】"
    return title if title.startswith(prefix) else prefix + title


def dematerialize(doc: dict) -> dict:
    result = copy.deepcopy(doc)
    tours = []
    for source in result.get("tours", []) or []:
        if not isinstance(source, dict):
            continue
        display = source.get(DISPLAY)
        if isinstance(display, dict) and display.get("mirrored") is True:
            continue
        item = copy.deepcopy(source)
        if isinstance(display, dict):
            official = display.get("officialTitle")
            if isinstance(official, str) and official:
                item["title"] = official
            item.pop(DISPLAY, None)
        tours.append(item)
    result["tours"] = tours
    result["performances"] = [
        copy.deepcopy(item)
        for item in (result.get("performances", []) or [])
        if isinstance(item, dict) and not isinstance(item.get(MIRROR), dict)
    ]
    result["lotteries"] = [
        copy.deepcopy(item)
        for item in (result.get("lotteries", []) or [])
        if isinstance(item, dict) and not isinstance(item.get(MIRROR), dict)
    ]
    return result


def incoming(root: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for relation in root.get("relations", []) or []:
        if not isinstance(relation, dict) or relation.get("approvedByUser") is not True:
            continue
        if relation.get("type") == "same_person_alias":
            ids = [x for x in (relation.get("artistIds") or []) if isinstance(x, str) and x]
            for target in ids:
                result.setdefault(target, set()).update(source for source in ids if source != target)
        elif relation.get("type") == "group_member_included":
            parent = relation.get("parentArtistId")
            members = [x for x in (relation.get("memberArtistIds") or []) if isinstance(x, str) and x]
            if isinstance(parent, str) and parent:
                result.setdefault(parent, set()).update(source for source in members if source != parent)
    return result


def materialize() -> list[Path]:
    artists = read(DATA / "artists.json")
    names = {item["id"]: item["name"] for item in artists if isinstance(item, dict)}
    relations = read(DATA / "artist_relations.json")
    paths = sorted((DATA / "artist").glob("*.json"))
    actual = {path.stem: read(path) for path in paths}
    canonical = {artist_id: dematerialize(doc) for artist_id, doc in actual.items()}
    output = copy.deepcopy(canonical)

    for target in sorted(incoming(relations)):
        target_doc = output[target]
        for tour in target_doc.get("tours", []) or []:
            official = tour["title"]
            tour["title"] = label(names[target], official)
            tour[DISPLAY] = {
                "sourceArtistId": target,
                "sourceTourId": tour["id"],
                "officialTitle": official,
                "mirrored": False,
            }

        for source in sorted(incoming(relations)[target]):
            source_doc = canonical[source]
            performances = [x for x in source_doc.get("performances", []) or [] if isinstance(x, dict)]
            lotteries = [x for x in source_doc.get("lotteries", []) or [] if isinstance(x, dict)]
            for source_tour in source_doc.get("tours", []) or []:
                source_tour_id = source_tour["id"]
                official = source_tour["title"]
                target_tour_id = mirror_id(target, source_tour_id)
                tour = copy.deepcopy(source_tour)
                tour["id"] = target_tour_id
                tour["artistId"] = target
                tour["title"] = label(names[source], official)
                tour[DISPLAY] = {
                    "sourceArtistId": source,
                    "sourceTourId": source_tour_id,
                    "officialTitle": official,
                    "mirrored": True,
                }
                target_doc.setdefault("tours", []).append(tour)

                perf_map = {}
                for source_perf in performances:
                    if source_perf.get("tourId") != source_tour_id:
                        continue
                    source_perf_id = source_perf["id"]
                    target_perf_id = mirror_id(target, source_perf_id)
                    perf_map[source_perf_id] = target_perf_id
                    perf = copy.deepcopy(source_perf)
                    perf["id"] = target_perf_id
                    perf["tourId"] = target_tour_id
                    perf[MIRROR] = {"sourceArtistId": source, "sourceId": source_perf_id}
                    target_doc.setdefault("performances", []).append(perf)

                for source_lottery in lotteries:
                    if source_lottery.get("tourId") != source_tour_id:
                        continue
                    source_lottery_id = source_lottery["id"]
                    source_pids = source_lottery.get("performanceIds") or []
                    missing = [pid for pid in source_pids if pid not in perf_map]
                    if missing:
                        raise RuntimeError(f"{source_lottery_id}: missing performance mapping {missing}")
                    lottery = copy.deepcopy(source_lottery)
                    lottery["id"] = mirror_id(target, source_lottery_id)
                    lottery["tourId"] = target_tour_id
                    lottery["performanceIds"] = [perf_map[pid] for pid in source_pids]
                    lottery[MIRROR] = {"sourceArtistId": source, "sourceId": source_lottery_id}
                    target_doc.setdefault("lotteries", []).append(lottery)

    changed = []
    for artist_id, doc in output.items():
        if doc != actual[artist_id]:
            path = DATA / "artist" / f"{artist_id}.json"
            write(path, doc)
            changed.append(path)
    return changed


def hash16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def update_manifest() -> None:
    path = DATA / "manifest.json"
    manifest = read(path)
    manifest["version"] = int(manifest.get("version", 0)) + 1
    jst = timezone(timedelta(hours=9))
    manifest["updatedAt"] = datetime.now(jst).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    manifest.setdefault("files", {}).setdefault("artists", {})["hash"] = hash16(DATA / "artists.json")
    entries = manifest.setdefault("artists", {})
    artist_paths = sorted((DATA / "artist").glob("*.json"))
    existing = {p.stem for p in artist_paths}
    for artist_path in artist_paths:
        entries.setdefault(artist_path.stem, {})["hash"] = hash16(artist_path)
    for artist_id in list(entries):
        if artist_id not in existing:
            del entries[artist_id]
    write(path, manifest)


if __name__ == "__main__":
    changed = materialize()
    update_manifest()
    print("materialized:", ", ".join(str(path) for path in changed) or "no artist files changed")
