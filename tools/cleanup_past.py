#!/usr/bin/env python3
"""過去ツアーの掃除スクリプト。全公演が今日以前のツアーを削除する。"""

import json
import glob
import os
import sys
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def cleanup(dry_run: bool = False) -> int:
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    total_removed = 0

    for filepath in sorted(glob.glob("data/artist/*.json")):
        with open(filepath) as f:
            data = json.load(f)

        perf_by_tour: dict[str, list] = {}
        for p in data.get("performances", []):
            perf_by_tour.setdefault(p["tourId"], []).append(p)

        past_tour_ids: set[str] = set()
        for tour in data.get("tours", []):
            tid = tour["id"]
            perfs = perf_by_tour.get(tid, [])
            if not perfs:
                end = tour.get("endDate")
                if end and _parse_dt(end) < today:
                    past_tour_ids.add(tid)
            else:
                if all(_parse_dt(p["performanceAt"]) < today for p in perfs):
                    past_tour_ids.add(tid)

        if not past_tour_ids:
            continue

        orig = len(data.get("tours", []))
        if not dry_run:
            data["tours"] = [t for t in data["tours"] if t["id"] not in past_tour_ids]
            data["performances"] = [p for p in data["performances"] if p["tourId"] not in past_tour_ids]
            data["lotteries"] = [l for l in data["lotteries"] if l["tourId"] not in past_tour_ids]
            with open(filepath, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")

        removed = orig - len(data.get("tours", []))
        total_removed += removed
        artist_id = os.path.basename(filepath).replace(".json", "")
        label = "[DRY]" if dry_run else "[削除]"
        print(f"{label} {artist_id}: {removed}ツアー ({', '.join(past_tour_ids)})")

    print(f"\n合計 {total_removed}ツアー{'(dry run)' if dry_run else '削除'}")
    return total_removed


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    cleanup(dry_run=dry_run)
