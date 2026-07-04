#!/usr/bin/env python3
"""
validate.py — live-ticket-data 品質チェックスクリプト

リポジトリルートから実行:
    python3 tools/validate.py

全チェックを通過したときのみ終了コード 0 を返す。
"""

import json
import os
import sys
import hashlib
from datetime import datetime
from pathlib import Path

# リポジトリルートを基準にする
REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
ARTISTS_JSON = DATA_DIR / "artists.json"
ARTIST_DIR = DATA_DIR / "artist"
MANIFEST_JSON = DATA_DIR / "manifest.json"

errors = []
warnings = []


def add_error(msg: str):
    errors.append(f"[ERROR] {msg}")


def add_warning(msg: str):
    warnings.append(f"[WARNING] {msg}")


def sha256_hex16(path: Path) -> str:
    """ファイルの SHA-256 先頭16文字を返す"""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def check_iso8601(value: str, context: str) -> bool:
    """ISO8601 形式かどうか検証する。不正なら error を追加して False を返す"""
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        add_error(f"日時フォーマット不正: '{value}' は ISO8601 でパースできません ({context})")
        return False


# ===== チェック A: JSON パース =====
def check_a_json_parse():
    """全 JSON ファイルがパース可能か"""
    results = {}  # path -> parsed data or None

    # artists.json
    if not ARTISTS_JSON.exists():
        add_error("artists.json が存在しません")
        results["artists"] = None
    else:
        try:
            results["artists"] = json.loads(ARTISTS_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            add_error(f"artists.json のパースに失敗: {e}")
            results["artists"] = None

    # artist ディレクトリ
    if not ARTIST_DIR.exists():
        add_error(f"data/artist/ ディレクトリが存在しません。mkdir -p {ARTIST_DIR} で作成してください")
        return results

    artist_files = sorted(ARTIST_DIR.glob("*.json"))
    results["artist_files"] = {}
    for fp in artist_files:
        try:
            results["artist_files"][fp.stem] = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            add_error(f"{fp.name} のパースに失敗: {e}")
            results["artist_files"][fp.stem] = None

    n = len(artist_files)
    if not errors:
        print(f"[OK] JSONパース (artists.json + {n} artist files)")
    else:
        print(f"[FAIL] JSONパース — エラーあり")

    return results


# ===== チェック B: artists.json 必須フィールド =====
def check_b_artists_required(artists_data):
    REQUIRED = ["id", "name", "source", "sourceUrl", "lastVerifiedAt"]
    if artists_data is None:
        add_error("artists.json が読み込めないため必須フィールドチェックをスキップ")
        return

    err_count = 0
    for entry in artists_data:
        for field in REQUIRED:
            if field not in entry or entry[field] is None:
                add_error(f"artists.json: id='{entry.get('id', '???')}' に必須フィールド '{field}' が存在しないか null です")
                err_count += 1

    if err_count == 0:
        print(f"[OK] artists.json 必須フィールド ({len(artists_data)}件)")
    else:
        print(f"[FAIL] artists.json 必須フィールド — {err_count}件のエラー")


# ===== チェック C: ID一覧の一致 =====
def check_c_id_consistency(artists_data, artist_files_data):
    if artists_data is None or artist_files_data is None:
        add_error("データ読み込み失敗のため ID 一致チェックをスキップ")
        return

    artists_ids = {entry["id"] for entry in artists_data if "id" in entry}
    file_ids = set(artist_files_data.keys())

    # artists.json に id があるが対応ファイルが無い
    missing_files = artists_ids - file_ids
    for aid in sorted(missing_files):
        add_error(f"ID一致: artists.json に '{aid}' があるが data/artist/{aid}.json が存在しません")

    # ファイルがあるが artists.json に id が無い
    missing_in_list = file_ids - artists_ids
    for aid in sorted(missing_in_list):
        add_error(f"ID一致: data/artist/{aid}.json が存在するが artists.json に '{aid}' がありません")

    if not missing_files and not missing_in_list:
        print(f"[OK] ID一覧の一致 (artists.json ↔ data/artist/ ディレクトリ, {len(artists_ids)}件)")
    else:
        print(f"[FAIL] ID一覧の一致 — {len(missing_files) + len(missing_in_list)}件の不一致")


# ===== チェック D: artist/{id}.json の構造 =====
def check_d_artist_structure(artist_files_data):
    REQUIRED = {
        "artistId": str,
        "tours": list,
        "performances": list,
        "lotteries": list,
    }
    if artist_files_data is None:
        add_error("データ読み込み失敗のため artist ファイル構造チェックをスキップ")
        return

    err_count = 0
    for aid, data in artist_files_data.items():
        if data is None:
            continue
        for field, expected_type in REQUIRED.items():
            if field not in data:
                add_error(f"artist/{aid}.json: 必須フィールド '{field}' が存在しません")
                err_count += 1
            elif not isinstance(data[field], expected_type):
                add_error(f"artist/{aid}.json: '{field}' の型が不正 (期待: {expected_type.__name__})")
                err_count += 1

    if err_count == 0:
        print(f"[OK] artist/{{id}}.json の構造 ({len(artist_files_data)}件)")
    else:
        print(f"[FAIL] artist/{{id}}.json の構造 — {err_count}件のエラー")


# ===== チェック E: Tour 必須フィールド =====
def check_e_tour_required(artist_files_data):
    REQUIRED = ["id", "artistId", "title", "source", "sourceUrl", "lastVerifiedAt"]
    if artist_files_data is None:
        return

    err_count = 0
    total = 0
    for aid, data in artist_files_data.items():
        if data is None:
            continue
        for tour in data.get("tours", []):
            total += 1
            for field in REQUIRED:
                if field not in tour or tour[field] is None:
                    add_error(f"Tour 必須フィールド: tour id='{tour.get('id', '???')}' に '{field}' が存在しないか null ({aid})")
                    err_count += 1

    if err_count == 0:
        print(f"[OK] Tour 必須フィールド ({total}件)")
    else:
        print(f"[FAIL] Tour 必須フィールド — {err_count}件のエラー")


# ===== チェック F: Performance 必須フィールド =====
def check_f_performance_required(artist_files_data):
    # フィールドが存在しないこと自体がエラー（null は許容: 会場未発表等）
    REQUIRED_EXIST = ["id", "tourId", "venue", "performanceAt", "source"]
    # null 不可（ID・親参照・日時の根幹はnull禁止）
    REQUIRED_NONNULL = ["id", "tourId", "performanceAt", "source"]

    if artist_files_data is None:
        return

    err_count = 0
    total = 0
    for aid, data in artist_files_data.items():
        if data is None:
            continue
        for perf in data.get("performances", []):
            total += 1
            for field in REQUIRED_EXIST:
                if field not in perf:
                    add_error(f"Performance 必須フィールド: performance id='{perf.get('id', '???')}' に '{field}' が存在しません ({aid})")
                    err_count += 1
            for field in REQUIRED_NONNULL:
                if perf.get(field) is None:
                    add_error(f"Performance 必須フィールド: performance id='{perf.get('id', '???')}' の '{field}' が null です ({aid})")
                    err_count += 1

    if err_count == 0:
        print(f"[OK] Performance 必須フィールド ({total}件)")
    else:
        print(f"[FAIL] Performance 必須フィールド — {err_count}件のエラー")


# ===== チェック G: Lottery 必須フィールド =====
def check_g_lottery_required(artist_files_data):
    # フィールドが存在しないこと自体がエラー（null は許容: 未発表情報があるため）
    REQUIRED_EXIST = ["id", "tourId", "type", "entryStartAt", "entryEndAt", "resultAt", "source"]
    # null 不可（ID・親参照・受付終了日・種別はnull禁止）
    REQUIRED_NONNULL = ["id", "tourId", "type", "source"]

    if artist_files_data is None:
        return

    err_count = 0
    warn_count = 0
    total = 0
    for aid, data in artist_files_data.items():
        if data is None:
            continue
        for lottery in data.get("lotteries", []):
            total += 1
            lid = lottery.get("id", "???")
            for field in REQUIRED_EXIST:
                if field not in lottery:
                    add_error(f"Lottery 必須フィールド: lottery id='{lid}' に '{field}' が存在しません ({aid})")
                    err_count += 1
            for field in REQUIRED_NONNULL:
                if lottery.get(field) is None:
                    add_error(f"Lottery 必須フィールド: lottery id='{lid}' の '{field}' が null です ({aid})")
                    err_count += 1
            # sourceUrl がない場合は警告（チケットサイト確認の根拠として必要）
            if not lottery.get("sourceUrl"):
                add_warning(f"lottery '{lid}' に sourceUrl がありません。チケットサイトURLを記録してください ({aid})")
                warn_count += 1

    if err_count == 0:
        print(f"[OK] Lottery 必須フィールド ({total}件)")
    else:
        print(f"[FAIL] Lottery 必須フィールド — {err_count}件のエラー")


# ===== チェック H: ID参照整合性 =====
def check_h_id_references(artists_data, artist_files_data):
    if artists_data is None or artist_files_data is None:
        add_error("データ読み込み失敗のため ID 参照整合性チェックをスキップ")
        return

    artist_ids = {entry["id"] for entry in artists_data if "id" in entry}
    err_count = 0

    for aid, data in artist_files_data.items():
        if data is None:
            continue

        # Tour.artistId が artists.json に存在するか
        tour_ids = set()
        for tour in data.get("tours", []):
            tid = tour.get("id")
            if tid:
                tour_ids.add(tid)
            art_id = tour.get("artistId")
            if art_id and art_id not in artist_ids:
                add_error(f"ID参照整合性: tour '{tid}' の artistId '{art_id}' が artists.json に存在しません ({aid})")
                err_count += 1

        # Performance.tourId が同ファイルの tours に存在するか
        for perf in data.get("performances", []):
            pid = perf.get("id", "???")
            tour_id_ref = perf.get("tourId")
            if tour_id_ref and tour_id_ref not in tour_ids:
                add_error(f"ID参照整合性: performance '{pid}' の tourId '{tour_id_ref}' が tours に存在しません ({aid})")
                err_count += 1

        # Lottery.tourId が同ファイルの tours に存在するか
        for lottery in data.get("lotteries", []):
            lid = lottery.get("id", "???")
            tour_id_ref = lottery.get("tourId")
            if tour_id_ref and tour_id_ref not in tour_ids:
                add_error(f"ID参照整合性: lottery '{lid}' の tourId '{tour_id_ref}' が tours に存在しません ({aid})")
                err_count += 1

    if err_count == 0:
        print("[OK] ID参照整合性")
    else:
        print(f"[FAIL] ID参照整合性 — {err_count}件のエラー")


# ===== チェック H2: Lottery.performanceIds 整合性 =====
def check_h2_lottery_performance_ids(artist_files_data):
    """
    performanceIds の整合性チェック。
    - フィールド未定義: エラー
    - null: OK（ツアー全公演を対象とする意図的な指定）
    - 空配列 []: エラー（null か 対象ID列挙を使うこと）
    - 存在しない performanceId の参照: エラー
    """
    if artist_files_data is None:
        return

    _MISSING = object()
    err_count = 0
    total = 0

    for aid, data in artist_files_data.items():
        if data is None:
            continue

        perf_ids = {p.get("id") for p in data.get("performances", []) if p.get("id")}

        for lottery in data.get("lotteries", []):
            total += 1
            lid = lottery.get("id", "???")
            pids = lottery.get("performanceIds", _MISSING)

            if pids is _MISSING:
                add_error(f"performanceIds: lottery '{lid}' に performanceIds フィールドがありません ({aid})")
                err_count += 1
                continue

            if pids is None:
                # null = ツアー全公演を対象とする意図的な指定（OK）
                continue

            if not isinstance(pids, list):
                add_error(f"performanceIds: lottery '{lid}' の performanceIds が配列ではありません ({aid})")
                err_count += 1
                continue

            if len(pids) == 0:
                # 空配列は禁止。null（全公演）か 対象IDの列挙を使うこと
                add_error(
                    f"performanceIds: lottery '{lid}' の performanceIds が空配列です。"
                    f"ツアー全公演対象なら null を、対象が限定されるなら公演IDを列挙してください ({aid})"
                )
                err_count += 1
                continue

            for pid in pids:
                if pid not in perf_ids:
                    add_error(
                        f"performanceIds: lottery '{lid}' の performanceId '{pid}' が performances に存在しません ({aid})"
                    )
                    err_count += 1

    if err_count == 0:
        print(f"[OK] Lottery.performanceIds 整合性 ({total}件)")
    else:
        print(f"[FAIL] Lottery.performanceIds 整合性 — {err_count}件のエラー")


# ===== チェック I: 日時フォーマット =====
def check_i_datetime_format(artist_files_data):
    # 日時フィールドの対象
    TOUR_DT_FIELDS = ["startDate", "endDate", "lastVerifiedAt"]
    PERF_DT_FIELDS = ["performanceAt", "doorOpenAt", "lastVerifiedAt"]
    LOTTERY_DT_FIELDS = ["entryStartAt", "entryEndAt", "resultAt", "paymentStartAt", "paymentEndAt", "lastVerifiedAt"]

    if artist_files_data is None:
        return

    err_count = 0

    for aid, data in artist_files_data.items():
        if data is None:
            continue

        for tour in data.get("tours", []):
            tid = tour.get("id", "???")
            for field in TOUR_DT_FIELDS:
                val = tour.get(field)
                if val is not None:
                    if not check_iso8601(val, f"tour '{tid}' .{field} ({aid})"):
                        err_count += 1

        for perf in data.get("performances", []):
            pid = perf.get("id", "???")
            for field in PERF_DT_FIELDS:
                val = perf.get(field)
                if val is not None:
                    if not check_iso8601(val, f"performance '{pid}' .{field} ({aid})"):
                        err_count += 1

        for lottery in data.get("lotteries", []):
            lid = lottery.get("id", "???")
            for field in LOTTERY_DT_FIELDS:
                val = lottery.get(field)
                if val is not None:
                    if not check_iso8601(val, f"lottery '{lid}' .{field} ({aid})"):
                        err_count += 1

    if err_count == 0:
        print("[OK] 日時フォーマット (ISO8601)")
    else:
        print(f"[FAIL] 日時フォーマット — {err_count}件のエラー")


# ===== チェック J: グローバルID一意性 =====
def check_j_global_id_uniqueness(artist_files_data):
    if artist_files_data is None:
        return

    tour_ids = {}      # id -> artist_id
    perf_ids = {}
    lottery_ids = {}
    err_count = 0

    for aid, data in artist_files_data.items():
        if data is None:
            continue

        for tour in data.get("tours", []):
            tid = tour.get("id")
            if tid:
                if tid in tour_ids:
                    add_error(f"グローバルID重複: tour id '{tid}' が '{tour_ids[tid]}' と '{aid}' で重複")
                    err_count += 1
                else:
                    tour_ids[tid] = aid

        for perf in data.get("performances", []):
            pid = perf.get("id")
            if pid:
                if pid in perf_ids:
                    add_error(f"グローバルID重複: performance id '{pid}' が '{perf_ids[pid]}' と '{aid}' で重複")
                    err_count += 1
                else:
                    perf_ids[pid] = aid

        for lottery in data.get("lotteries", []):
            lid = lottery.get("id")
            if lid:
                if lid in lottery_ids:
                    add_error(f"グローバルID重複: lottery id '{lid}' が '{lottery_ids[lid]}' と '{aid}' で重複")
                    err_count += 1
                else:
                    lottery_ids[lid] = aid

    if err_count == 0:
        print(f"[OK] グローバルID一意性 ({len(tour_ids)} tours, {len(perf_ids)} performances, {len(lottery_ids)} lotteries)")
    else:
        print(f"[FAIL] グローバルID一意性 — {err_count}件の重複")


# ===== チェック K: manifest hash 整合性 =====
def check_k_manifest_hash(artist_files_data):
    if not MANIFEST_JSON.exists():
        add_warning("manifest.json が存在しません (update_manifest.py を実行してください)")
        print("[WARNING] manifest hash 整合性 — manifest.json が存在しません")
        return

    try:
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        add_warning(f"manifest.json のパースに失敗: {e}")
        print("[WARNING] manifest hash 整合性 — manifest.json のパースに失敗")
        return

    warn_count = 0

    # artists.json hash
    if ARTISTS_JSON.exists():
        expected_hash = sha256_hex16(ARTISTS_JSON)
        actual_hash = manifest.get("files", {}).get("artists", {}).get("hash")
        if actual_hash != expected_hash:
            add_warning(f"manifest hash 不一致: files.artists.hash '{actual_hash}' ≠ 実際 '{expected_hash}' (update_manifest.py を実行してください)")
            warn_count += 1

    # artists.{id}.hash
    manifest_artists = manifest.get("artists", {})
    if artist_files_data:
        for aid, data in artist_files_data.items():
            fp = ARTIST_DIR / f"{aid}.json"
            if fp.exists():
                expected_hash = sha256_hex16(fp)
                actual_hash = manifest_artists.get(aid, {}).get("hash")
                if actual_hash != expected_hash:
                    add_warning(f"manifest hash 不一致: artists.{aid}.hash '{actual_hash}' ≠ 実際 '{expected_hash}'")
                    warn_count += 1

    if warn_count == 0:
        print("[OK] manifest hash 整合性")
    else:
        print(f"[WARNING] manifest hash 整合性 — {warn_count}件の不一致 (push 前に update_manifest.py を実行してください)")


# ===== チェック L: データ鮮度チェック =====
def check_l_data_freshness(artist_files_data):
    """
    lastVerifiedAt が古いレコードを警告する。
    - Lottery: 60日以上前 → WARNING（抽選情報は変化が早い）
    - Tour/Performance: 90日以上前 → WARNING
    """
    if artist_files_data is None:
        return

    now = datetime.now().astimezone()
    LOTTERY_THRESHOLD_DAYS = 60
    TOUR_PERF_THRESHOLD_DAYS = 90
    warn_count = 0

    for aid, data in artist_files_data.items():
        if data is None:
            continue

        for tour in data.get("tours", []):
            val = tour.get("lastVerifiedAt")
            if val:
                try:
                    verified = datetime.fromisoformat(val)
                    if (now - verified).days > TOUR_PERF_THRESHOLD_DAYS:
                        add_warning(
                            f"鮮度: tour '{tour.get('id', '???')}' の lastVerifiedAt が {(now - verified).days}日前です。公式サイトで再確認してください ({aid})"
                        )
                        warn_count += 1
                except ValueError:
                    pass

        for perf in data.get("performances", []):
            val = perf.get("lastVerifiedAt")
            if val:
                try:
                    verified = datetime.fromisoformat(val)
                    if (now - verified).days > TOUR_PERF_THRESHOLD_DAYS:
                        add_warning(
                            f"鮮度: performance '{perf.get('id', '???')}' の lastVerifiedAt が {(now - verified).days}日前です ({aid})"
                        )
                        warn_count += 1
                except ValueError:
                    pass

        for lottery in data.get("lotteries", []):
            val = lottery.get("lastVerifiedAt")
            if val:
                try:
                    verified = datetime.fromisoformat(val)
                    if (now - verified).days > LOTTERY_THRESHOLD_DAYS:
                        add_warning(
                            f"鮮度: lottery '{lottery.get('id', '???')}' の lastVerifiedAt が {(now - verified).days}日前です。チケットサイトで再確認してください ({aid})"
                        )
                        warn_count += 1
                except ValueError:
                    pass

    if warn_count == 0:
        print("[OK] データ鮮度 (lastVerifiedAt)")
    else:
        print(f"[WARNING] データ鮮度 — {warn_count}件が古くなっています")


# ===== メイン =====
def main():
    print("=== validate.py ===")

    # A: JSON パース（全データを読み込む）
    parsed = check_a_json_parse()
    artists_data = parsed.get("artists")
    artist_files_data = parsed.get("artist_files")

    # B: artists.json 必須フィールド
    check_b_artists_required(artists_data)

    # C: ID一覧の一致
    check_c_id_consistency(artists_data, artist_files_data)

    # D: artist/{id}.json の構造
    check_d_artist_structure(artist_files_data)

    # E: Tour 必須フィールド
    check_e_tour_required(artist_files_data)

    # F: Performance 必須フィールド
    check_f_performance_required(artist_files_data)

    # G: Lottery 必須フィールド
    check_g_lottery_required(artist_files_data)

    # H: ID参照整合性
    check_h_id_references(artists_data, artist_files_data)

    # H2: Lottery.performanceIds 整合性
    check_h2_lottery_performance_ids(artist_files_data)

    # I: 日時フォーマット
    check_i_datetime_format(artist_files_data)

    # J: グローバルID一意性
    check_j_global_id_uniqueness(artist_files_data)

    # K: manifest hash 整合性
    check_k_manifest_hash(artist_files_data)

    # L: データ鮮度チェック
    check_l_data_freshness(artist_files_data)

    print()

    # 警告の表示
    for w in warnings:
        print(w)
    if warnings:
        print()

    # エラーの表示
    for e in errors:
        print(e)

    if errors:
        print(f"\nエラー {len(errors)}件。push 前に修正してください。")
        sys.exit(1)
    else:
        print("✅ 全チェック通過。push OK。")
        sys.exit(0)


if __name__ == "__main__":
    main()
