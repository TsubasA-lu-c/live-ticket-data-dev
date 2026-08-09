#!/usr/bin/env python3
"""AIが返したJSONを配信データに入れる前に検証する。

**AIの出力をそのまま data/artist/*.json へ書かない。**
このアプリの根幹は申込期限の管理なので、日付の創作・会場の取り違え・
別アーティストの混入は直接ユーザーの損害になる。

入力（cache/ai_result.json）の形:
  {"results": [{"artistId": "yuzu", "events": [...], "lotteries": [...]}]}

使い方:
  python3 tools/validate_ai_result.py                       # cache/ai_result.json
  python3 tools/validate_ai_result.py path/to/result.json
  python3 tools/validate_ai_result.py --out cache/ai_accepted.json
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.collect.merge import validate_ai_events  # noqa: E402

DEFAULT_INPUT = Path("cache/ai_result.json")
DEFAULT_OUTPUT = Path("cache/ai_accepted.json")
ARTISTS_FILE = Path("data/artists.json")
QUEUE_FILE = Path("cache/ai_queue.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI出力の検証")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true",
                        help="warn が1件でもあれば異常終了する")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"入力がありません: {args.input}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # AIが不正なJSONを返した場合はここで止める（部分適用しない）
        print(f"[ERROR] JSONとして読めません: {e}", file=sys.stderr)
        return 2

    names = _artist_names()
    hosts = _allowed_hosts()
    today = date.today()

    accepted_all = []
    error_count = warn_count = 0

    for entry in payload.get("results") or []:
        aid = entry.get("artistId")
        if aid not in names:
            print(f"[ERROR] 未知のartistId: {aid}", file=sys.stderr)
            error_count += 1
            continue

        events, issues = validate_ai_events(
            entry.get("events") or [], names[aid], today=today,
            allowed_hosts=hosts.get(aid),
        )
        for issue in issues:
            print(f"[{issue.level.upper()}] {aid} {issue.code}: {issue.detail}",
                  file=sys.stderr)
            if issue.level == "error":
                error_count += 1
            else:
                warn_count += 1

        accepted_all.append({
            "artistId": aid,
            "artistName": names[aid],
            "events": events,
            "lotteries": entry.get("lotteries") or [],
            "rejected": len(entry.get("events") or []) - len(events),
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"results": accepted_all}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    total_ok = sum(len(r["events"]) for r in accepted_all)
    total_rejected = sum(r["rejected"] for r in accepted_all)
    print(f"\n検証: 採用 {total_ok}件 / 却下 {total_rejected}件 "
          f"（error {error_count} / warn {warn_count}）")
    print(f"出力: {args.out}")

    if error_count:
        return 2
    if args.strict and warn_count:
        return 3
    return 0


def _artist_names() -> dict:
    artists = json.loads(ARTISTS_FILE.read_text(encoding="utf-8"))
    return {a["id"]: a.get("name", a["id"]) for a in artists}


def _allowed_hosts() -> dict:
    """キューに載せた取得元のホストのみ detailUrl として妥当とみなす。"""
    if not QUEUE_FILE.exists():
        return {}
    try:
        queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    hosts = {}
    for item in queue.get("items") or []:
        found = []
        for src in item.get("sources") or []:
            netloc = urlparse(src.get("url", "")).netloc.lower()
            if netloc and netloc not in found:
                found.append(netloc)
        if found:
            hosts[item["artistId"]] = found
    return hosts


if __name__ == "__main__":
    sys.exit(main())
