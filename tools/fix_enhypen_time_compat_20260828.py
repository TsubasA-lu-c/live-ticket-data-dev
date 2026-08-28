#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ARTIST = ROOT / "data/artist/enhypen.json"
MANIFEST = ROOT / "data/manifest.json"
TARGETS = {
    "enhypen_the_sin_bliss_meet_greet_2026_kanto_1010": "2026-10-10T12:00:00+09:00",
    "enhypen_the_sin_bliss_long_sign_2026_kanto_1010": "2026-10-10T12:00:00+09:00",
    "enhypen_the_sin_bliss_2shot_2026_kanto_0905": "2026-09-05T12:00:00+09:00",
    "enhypen_the_sin_bliss_premium_sign_2026_makuhari_1010": "2026-10-10T12:00:00+09:00",
}

def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

def main() -> int:
    data = json.loads(ARTIST.read_text(encoding="utf-8"))
    found = set()
    for p in data["performances"]:
        if p["id"] in TARGETS:
            if p.get("performanceAt") is not None or p.get("performanceTimeEstimated") is not None:
                raise SystemExit(f"unexpected pre-fix state: {p['id']}")
            p["performanceAt"] = TARGETS[p["id"]]
            p["performanceTimeEstimated"] = True
            found.add(p["id"])
    if found != set(TARGETS):
        raise SystemExit(f"missing targets: {set(TARGETS)-found}")
    ARTIST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != 165 or manifest["artists"]["enhypen"]["hash"] != "87aeca74ef15e293":
        raise SystemExit("manifest pre-fix state moved")
    manifest["version"] = 166
    manifest["updatedAt"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    manifest["artists"]["enhypen"]["hash"] = sha16(ARTIST)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert len(data["tours"]) == 9 and len(data["performances"]) == 17 and len(data["lotteries"]) == 23
    print("compat fix OK", manifest["artists"]["enhypen"]["hash"])
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
