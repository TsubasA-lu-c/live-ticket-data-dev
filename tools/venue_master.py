#!/usr/bin/env python3
"""会場マスタ（config/venues.json）の生成・適用・点検。

配信スキーマは変えない。`performance.venue` は文字列のままで、
マスタは**収集ツール側だけが持つ正規化辞書**として使う。

使い方:
  python3 tools/venue_master.py build          # 既存データからマスタを作る／更新する
  python3 tools/venue_master.py apply --dry-run  # 既存データを正式表記へ寄せる（確認）
  python3 tools/venue_master.py apply          # 実際に書き換える
  python3 tools/venue_master.py check          # 綴りが違う同一会場の候補を出す
"""
import argparse
import collections
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.collect.extract import PREFECTURES, find_prefecture, normalize_venue  # noqa: E402
from tools.collect.venues import MASTER_FILE, VenueMaster, build_from_names  # noqa: E402

ARTIST_GLOB = "data/artist/*.json"

# 差分がこれらならホール単位の粒度違い。**同じ施設でも別の部屋なので寄せない**
_HALL_SUFFIX = re.compile(
    r"(ホール|アリーナ|シアター|劇場|展示場|展示ホール|会議場|棟|館|"
    r"ステージ|スタジオ|グラウンド|コート|第[0-9一二三四五六七八九]|"
    r"[a-z]$|hall|arena|stage)", re.I
)

# 差分がこれらなら地名の付け方の違い。別名として寄せてよい可能性が高い
_PLACE_PREFIX = re.compile(
    "^(" + "|".join(
        [p[:-1] if p != "北海道" else p for p in PREFECTURES] +
        ["仙台", "札幌", "横浜", "名古屋", "神戸", "静岡", "広島", "福岡", "京都",
         "ソウル", "台北", "香港", "上海", "バンコク", "クアラルンプール", "シンガポール"]
    ) + ")$"
)


def _collect_names():
    counts = collections.Counter()
    prefs = {}
    for path in sorted(glob.glob(ARTIST_GLOB)):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for p in data.get("performances") or []:
            name = (p.get("venue") or "").strip()
            if not name:
                continue
            counts[name] += 1
            if name not in prefs:
                pref = find_prefecture(name, name)
                if pref:
                    prefs[name] = pref
    return counts, prefs


def cmd_build(args) -> int:
    counts, prefs = _collect_names()
    existing = VenueMaster.load(args.master)

    # 手で決めた正式表記・別名・都道府県は自動生成で上書きしない。
    # 突き合わせは名前ではなく正規化キーで行う（手動で正式表記を
    # 多数派と別のものにした場合、名前一致では拾えないため）
    manual = {}
    for v in existing.venues:
        if not v.get("manual"):
            continue
        for name in [v["name"]] + list(v.get("aliases") or []):
            manual.setdefault(normalize_venue(name), v)

    venues = build_from_names(counts, prefs)
    used_manual = set()
    for v in venues:
        kept = manual.get(normalize_venue(v["name"]))
        if not kept:
            continue
        used_manual.add(id(kept))
        observed = set(v.get("aliases") or []) | {v["name"]}
        v["name"] = kept["name"]                       # 手動指定の正式表記を優先
        merged = (observed | set(kept.get("aliases") or [])) - {kept["name"]}
        if merged:
            v["aliases"] = sorted(merged)
        v["pref"] = kept.get("pref") or v.get("pref")
        v["manual"] = True

    # 既存データに1件も出てこなくなった手動エントリも残す（改称の履歴になる）
    for v in manual.values():
        if id(v) not in used_manual:
            venues.append(v)

    master = VenueMaster(venues)
    master.save(args.master)

    with_alias = sum(1 for v in venues if v.get("aliases"))
    with_pref = sum(1 for v in venues if v.get("pref"))
    print(f"会場マスタを書き出しました: {args.master}")
    print(f"  会場 {len(venues)}件 / 表記ゆれを持つ会場 {with_alias}件 / 都道府県あり {with_pref}件")
    return 0


def cmd_apply(args) -> int:
    master = VenueMaster.load(args.master)
    if not len(master):
        print("マスタが空です。先に build を実行してください", file=sys.stderr)
        return 1

    changed_files = 0
    changed_rows = 0
    samples = []
    for path in sorted(glob.glob(ARTIST_GLOB)):
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        hits = 0
        for perf in data.get("performances") or []:
            name = (perf.get("venue") or "").strip()
            if not name:
                continue
            canonical = master.canonical(name)
            if canonical != name:
                if len(samples) < 30:
                    samples.append((data.get("artistId"), name, canonical))
                perf["venue"] = canonical
                hits += 1
        if hits and not args.dry_run:
            with p.open("w", encoding="utf-8") as w:
                json.dump(data, w, ensure_ascii=False, indent=2)
                w.write("\n")
        if hits:
            changed_files += 1
            changed_rows += hits

    label = "[DRY] " if args.dry_run else ""
    for artist, before, after in samples:
        print(f"  {label}{artist}: 「{before}」→「{after}」")
    print(f"\n{label}{changed_rows}件 / {changed_files}ファイルを正式表記へ寄せました")
    return 0


def cmd_check(args) -> int:
    """綴りが違うのに同じ会場らしいものを出す（改称・略称の検出）。

    自動では寄せない。**改称かどうかは人にしか判断できない**ため、
    候補を出して config/venues.json の aliases に手で足してもらう。
    """
    counts, _ = _collect_names()
    names = sorted(counts)
    master = VenueMaster.load(args.master)
    known_alias_keys = {normalize_venue(a)
                        for v in master.venues for a in (v.get("aliases") or [])}

    aliases, granularity = [], []
    for i, a in enumerate(names):
        ka = normalize_venue(a)
        for b in names[i + 1:]:
            kb = normalize_venue(b)
            if ka == kb or kb in known_alias_keys or ka in known_alias_keys:
                continue
            if not (len(ka) >= 4 and len(kb) >= 4 and (ka in kb or kb in ka)):
                continue
            short, long_ = (ka, kb) if len(ka) < len(kb) else (kb, ka)
            extra = long_.replace(short, "", 1)
            row = (a, counts[a], b, counts[b], extra)
            # 差分がホール名なら「同じ施設の別の部屋」で、別名ではない
            if _HALL_SUFFIX.search(extra):
                granularity.append(row)
            elif _PLACE_PREFIX.search(extra):
                aliases.append(row)

    if aliases:
        print(f"■ 別名の候補 {len(aliases)}件（地名の付け方だけが違う。寄せてよい可能性が高い）")
        for a, na, b, nb, extra in aliases:
            print(f"    「{a}」({na}件)  ⇔  「{b}」({nb}件)   差分: {extra}")
    if granularity:
        print(f"\n■ 粒度違い {len(granularity)}件（同じ施設の別ホール。**寄せてはいけない**）")
        for a, na, b, nb, extra in granularity:
            print(f"    「{a}」({na}件)  ⇔  「{b}」({nb}件)   差分: {extra}")
    if not aliases and not granularity:
        print("綴りの違う同一会場の候補はありません")
        return 0

    print("\n別名として寄せる場合は config/venues.json の該当エントリに")
    print('  "aliases": [...], "manual": true')
    print("を書き足し、もう一度 build → apply を実行してください")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="会場マスタの管理")
    parser.add_argument("--master", type=Path, default=MASTER_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="既存データからマスタを生成・更新する")
    apply_p = sub.add_parser("apply", help="既存データを正式表記へ寄せる")
    apply_p.add_argument("--dry-run", action="store_true")
    sub.add_parser("check", help="綴りが違う同一会場の候補を出す")

    args = parser.parse_args()
    return {"build": cmd_build, "apply": cmd_apply, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
