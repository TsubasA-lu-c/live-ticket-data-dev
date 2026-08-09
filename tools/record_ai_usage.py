#!/usr/bin/env python3
"""AIキューを消化したときの実トークン数を、直近のバッチ統計に書き戻す。

パイプライン側は「渡す予定の文字数」しか知らない。実際に何トークン使ったかは
AIを動かした側（Claude Code）にしか分からないので、終わったあとに記録する。
削減効果を後から検証できるようにするための記録であり、必須ではない。

使い方:
  python3 tools/record_ai_usage.py --input 4200 --output 900 --artists 3
"""
import argparse
import json
import sys
from pathlib import Path

METRICS_FILE = Path("cache/collect_metrics.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI実使用量の記録")
    parser.add_argument("--input", type=int, required=True, help="実際の入力トークン")
    parser.add_argument("--output", type=int, required=True, help="実際の出力トークン")
    parser.add_argument("--artists", type=int, default=None, help="実際にAIを回した組数")
    parser.add_argument("--metrics", type=Path, default=METRICS_FILE)
    args = parser.parse_args()

    if not args.metrics.exists():
        print(f"統計ファイルがありません: {args.metrics}", file=sys.stderr)
        return 1

    lines = [l for l in args.metrics.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        print("記録すべきバッチがありません", file=sys.stderr)
        return 1

    last = json.loads(lines[-1])
    last["aiInputTokensActual"] = args.input
    last["aiOutputTokensActual"] = args.output
    if args.artists is not None:
        last["aiArtistsActual"] = args.artists

    legacy = last.get("legacyTokensEstimated") or 0
    if legacy:
        last["actualTokenReduction"] = round(1 - (args.input + args.output) / legacy, 3)

    lines[-1] = json.dumps(last, ensure_ascii=False)
    args.metrics.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"記録しました: 入力 {args.input:,} / 出力 {args.output:,} トークン")
    if "actualTokenReduction" in last:
        print(f"旧方式比の削減（実測）: 約{last['actualTokenReduction'] * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
