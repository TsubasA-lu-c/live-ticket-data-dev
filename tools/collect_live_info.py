#!/usr/bin/env python3
"""ライブ情報の機械収集バッチ（AIにWeb巡回させない収集経路）。

取得 → 正規化 → 差分 → 機械解析 → 既存突き合わせ を行い、
機械では解けなかった分だけを cache/ai_queue.json に積む。
このスクリプト自体はAIを一切呼ばない。

使い方:
  python3 tools/collect_live_info.py                # 全アーティスト
  python3 tools/collect_live_info.py yuzu milk      # 指定アーティストのみ
  python3 tools/collect_live_info.py --limit 10     # 先頭10組だけ（試走用）
  python3 tools/collect_live_info.py --no-cache     # cache/スナップショットを更新しない
  python3 tools/collect_live_info.py --report out.json

終了コード:
  0 = 正常（1組も取れなかった場合を除く）
  1 = 対象が0件、または全件が取得失敗
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.collect import merge as mergemod  # noqa: E402
from tools.collect.fetcher import Fetcher  # noqa: E402
from tools.collect.pipeline import AI_QUEUE_FILE, run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ライブ情報の機械収集バッチ")
    parser.add_argument("artist_ids", nargs="*", help="対象アーティストID（省略時は全件）")
    parser.add_argument("--limit", type=int, default=None, help="先頭N組だけ処理する")
    parser.add_argument("--no-cache", action="store_true", help="cache・スナップショットを更新しない")
    parser.add_argument("--interval", type=float, default=None,
                        help="同一ホストへのアクセス間隔（秒）。既定は3.0")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="robots.txt を確認しない（通常は使わない）")
    parser.add_argument("--report", type=Path, default=None, help="結果JSONの出力先")
    parser.add_argument("--quiet", action="store_true", help="1組ごとのログを出さない")
    parser.add_argument("--accept", action="store_true",
                        help="収集・validate が済んだアーティストの指紋を確定する"
                             "（取得はせず artist_ids のみを確定）")
    parser.add_argument("--pending", action="store_true",
                        help="確定待ちのアーティストを一覧表示して終了する")
    args = parser.parse_args()

    if args.pending:
        return _show_pending()

    if args.accept:
        if not args.artist_ids:
            print("--accept には確定するアーティストIDを指定してください", file=sys.stderr)
            return 1
        return _accept(args.artist_ids)

    fetcher = Fetcher(
        host_interval_sec=args.interval if args.interval is not None else 3.0,
        respect_robots=not args.ignore_robots,
    )

    result = run(
        artist_ids=args.artist_ids or None,
        limit=args.limit,
        update_cache=not args.no_cache,
        fetcher=fetcher,
        verbose=not args.quiet,
    )

    metrics = result["metrics"]
    if metrics["artistsProcessed"] == 0:
        print("対象アーティストがありません", file=sys.stderr)
        return 1

    _print_summary(result)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "metrics": metrics,
            "errors": result["errors"],
            "queueArtists": [i["artistId"] for i in result["queue"]],
            "artists": [_artist_report(o) for o in result["outcomes"]],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nレポート: {args.report}")

    return 1 if metrics["fetchFailed"] == metrics["artistsProcessed"] else 0


def _show_pending() -> int:
    """確定待ち（＝変更を検知したが収集が終わっていない）アーティストを出す。"""
    from tools.collect.pipeline import Pipeline

    pipeline = Pipeline(update_cache=False)
    if not pipeline.pending:
        print("確定待ちのアーティストはありません")
        return 0
    print(f"確定待ち {len(pipeline.pending)}組（次回も差分検出の対象に残ります）")
    for aid, entry in pipeline.pending.items():
        print(f"  {aid}: {entry.get('lastResult')} "
              f"(aiFallback={entry.get('aiFallback')}, {entry.get('lastRunAt')})")
    print("\n収集とvalidateが済んだら:")
    print("  python3 tools/collect_live_info.py --accept " + " ".join(pipeline.pending))
    return 0


def _accept(artist_ids: list) -> int:
    from tools.collect.pipeline import Pipeline

    pipeline = Pipeline()
    accepted = pipeline.accept(artist_ids)
    missing = [a for a in artist_ids if a not in accepted]
    pipeline.save_state()

    if accepted:
        print(f"確定しました（{len(accepted)}組）: " + ", ".join(accepted))
    for aid in missing:
        print(f"[WARN] {aid} は確定待ちにありません（既に確定済みか未実行）", file=sys.stderr)
    return 0 if accepted or not artist_ids else 1


def _artist_report(outcome) -> dict:
    counts = mergemod.summarize(outcome.statuses)
    return {
        "artistId": outcome.artist_id,
        "fetchOk": outcome.fetch_ok,
        "changed": outcome.changed,
        "parserOk": outcome.parser_ok,
        "aiReason": outcome.ai_reason,
        "counts": counts,
        "newEvents": [
            s.event.to_dict() for s in outcome.statuses
            if s.status == mergemod.NEW and s.event
        ],
        "missingOnSite": [
            s.existing_id for s in outcome.statuses if s.status == mergemod.REMOVED
        ],
        "errors": outcome.errors,
    }


def _print_summary(result: dict) -> None:
    m = result["metrics"]
    print("\n=== 収集サマリ ===")
    print(f"  処理組数        : {m['artistsProcessed']}")
    print(f"  取得失敗        : {m['fetchFailed']}")
    print(f"  変化なし        : {m['noChange']}")
    print(f"  変化あり        : {m['sitesChanged']}")
    print(f"  パーサー成功    : {m['parserSuccess']}")
    print(f"  新規公演候補    : {m['newEventCandidates']}")
    print(f"  AIフォールバック: {m['aiFallbackCount']} 組 {m['aiFallbackArtists']}")
    print(f"  AI入力概算      : {m['aiInputChars']:,}文字 / 約{m['aiInputTokensEstimated']:,}トークン")
    if m["aiFreeRatio"] is not None:
        print(f"  AIを呼ばず完了  : {m['aiFreeRatio'] * 100:.1f}%")
    if m["estimatedTokenReduction"] is not None:
        print(f"  旧方式比の削減  : 約{m['estimatedTokenReduction'] * 100:.1f}%")

    if result["errors"]:
        print("\n=== エラー一覧 ===")
        for e in result["errors"]:
            for msg in e["errors"]:
                print(f"  {e['artistId']}: {msg}")

    if result["queue"]:
        print(f"\nAIキュー: {AI_QUEUE_FILE}（{len(result['queue'])}組）")
        print("  → Claude Code 側でこのファイルだけを読んで構造化する（Web巡回はさせない）")
        print("  → 反映と validate が済んだら指紋を確定すること:")
        print("     python3 tools/collect_live_info.py --accept "
              + " ".join(i["artistId"] for i in result["queue"]))


if __name__ == "__main__":
    sys.exit(main())
