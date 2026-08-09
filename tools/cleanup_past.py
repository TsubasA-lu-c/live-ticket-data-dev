#!/usr/bin/env python3
"""過去ツアーの掃除スクリプト。

全公演が終わったツアーを、**最終公演から一定期間あけてから**削除する。
即座に消さないのは、アプリが過去公演も「これまで」としてマイチケットに
登録できるため（2026-08-10 確定）。消してしまうと、行ったライブを後から
記録する手段がなくなる。無期限に残すとファイルが年々肥大化するので、
実際に登録したくなる範囲だけ残す。

使い方:
  python3 tools/cleanup_past.py                # 6ヶ月経過分を削除
  python3 tools/cleanup_past.py --dry-run      # 削除せず対象を表示
  python3 tools/cleanup_past.py --months 12    # 保持期間を変える
"""

import argparse
import json
import glob
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# 終了したツアーを配信に残す期間（月）。§5.1 参照
RETENTION_MONTHS = 6


def _months_ago(base: datetime, months: int) -> datetime:
    """base から months ヶ月前。月末日は月初へずらさず丸める。"""
    year, month = base.year, base.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(base.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0)
                         else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return base.replace(year=year, month=month, day=day)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def cleanup(dry_run: bool = False, months: int = RETENTION_MONTHS,
            now: datetime = None) -> int:
    today = (now or datetime.now(JST)).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = _months_ago(today, months)
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
                if end and _parse_dt(end) < cutoff:
                    past_tour_ids.add(tid)
            else:
                # 最終公演が保持期間より前なら削除。1公演でも期間内なら残す
                if max(_parse_dt(p["performanceAt"]) for p in perfs) < cutoff:
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

    print(f"\n合計 {total_removed}ツアー{'(dry run)' if dry_run else '削除'}"
          f"（{months}ヶ月保持 / 基準日 {cutoff.date()} より前が対象）")
    return total_removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="終了ツアーの掃除")
    parser.add_argument("--dry-run", action="store_true", help="削除せず対象を表示")
    parser.add_argument("--months", type=int, default=RETENTION_MONTHS,
                        help=f"終了後の保持期間（月・既定 {RETENTION_MONTHS}）")
    args = parser.parse_args()
    cleanup(dry_run=args.dry_run, months=args.months)
