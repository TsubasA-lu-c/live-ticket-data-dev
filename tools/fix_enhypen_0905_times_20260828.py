#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ARTIST = ROOT / "data" / "artist" / "enhypen.json"
MANIFEST = ROOT / "data" / "manifest.json"
EXPECTED_BASE_HASH = "54b4a6702d514ade"
TARGET_IDS = {
    "enhypen_the_sin_bliss_meet_greet_2026_kanto_0905",
    "enhypen_the_sin_bliss_heart_touch_2026_kanto_0905",
}


def sha16(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def main() -> int:
    raw = ARTIST.read_bytes()
    if sha16(raw) != EXPECTED_BASE_HASH:
        raise SystemExit(f"ENHYPEN baseline moved: expected {EXPECTED_BASE_HASH}, got {sha16(raw)}")

    data = json.loads(raw)
    if (len(data["tours"]), len(data["performances"]), len(data["lotteries"])) != (9, 17, 23):
        raise SystemExit("unexpected ENHYPEN baseline counts")

    found = set()
    for perf in data["performances"]:
        if perf["id"] not in TARGET_IDS:
            continue
        found.add(perf["id"])
        if perf.get("performanceAt") != "2026-09-05T18:00:00+09:00":
            raise SystemExit(f"unexpected original performanceAt: {perf['id']}={perf.get('performanceAt')}")
        if perf.get("performanceTimeEstimated") not in (None, False):
            raise SystemExit(f"unexpected existing estimated flag: {perf['id']}")
        perf["performanceAt"] = "2026-09-05T12:00:00+09:00"
        perf["performanceDate"] = "2026-09-05"
        perf["performanceTimeEstimated"] = True

    if found != TARGET_IDS:
        raise SystemExit(f"target performance ids missing: {sorted(TARGET_IDS - found)}")

    if (len(data["tours"]), len(data["performances"]), len(data["lotteries"])) != (9, 17, 23):
        raise SystemExit("counts changed unexpectedly")

    ARTIST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artist_hash = sha16(ARTIST.read_bytes())
    if artist_hash == EXPECTED_BASE_HASH:
        raise SystemExit("artist hash did not change")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != 167:
        raise SystemExit(f"manifest baseline version moved: {manifest.get('version')}")
    if manifest["artists"]["enhypen"]["hash"] != EXPECTED_BASE_HASH:
        raise SystemExit("manifest ENHYPEN baseline hash moved")

    manifest["version"] = 168
    manifest["updatedAt"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    manifest["artists"]["enhypen"]["hash"] = artist_hash
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Deterministic structural checks.
    reloaded = json.loads(ARTIST.read_text(encoding="utf-8"))
    for perf in reloaded["performances"]:
        if perf["id"] in TARGET_IDS:
            assert perf["performanceAt"] == "2026-09-05T12:00:00+09:00"
            assert perf["performanceDate"] == "2026-09-05"
            assert perf["performanceTimeEstimated"] is True

    print("ENHYPEN 2026-09-05 unknown-time correction OK")
    print(f"artist hash={artist_hash} manifest version={manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
