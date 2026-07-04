#!/usr/bin/env python3
"""
update_manifest.py — manifest.json 自動更新スクリプト

リポジトリルートから実行:
    python3 tools/update_manifest.py

artists.json と data/artist/*.json の SHA-256 先頭16文字を計算して
manifest.json の hash・version・updatedAt を更新する。
"""

import json
import hashlib
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# リポジトリルートを基準にする
REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
ARTISTS_JSON = DATA_DIR / "artists.json"
ARTIST_DIR = DATA_DIR / "artist"
MANIFEST_JSON = DATA_DIR / "manifest.json"

JST = timezone(timedelta(hours=9))


def sha256_hex16(path: Path) -> str:
    """ファイルの SHA-256 先頭16文字を返す"""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def main():
    print("=== update_manifest.py ===")

    # 既存の manifest.json を読み込む（存在しない場合は初期値）
    if MANIFEST_JSON.exists():
        try:
            manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[ERROR] manifest.json のパースに失敗: {e}")
            sys.exit(1)
    else:
        print("[INFO] manifest.json が存在しないため新規作成します")
        manifest = {
            "version": 0,
            "updatedAt": "",
            "files": {
                "artists": {
                    "hash": ""
                }
            },
            "artists": {}
        }

    # artists.json の hash を計算
    if not ARTISTS_JSON.exists():
        print("[ERROR] data/artists.json が存在しません")
        sys.exit(1)

    artists_hash = sha256_hex16(ARTISTS_JSON)
    print(f"artists.json hash: {artists_hash}")

    # data/artist/*.json の hash を計算（辞書順）
    if not ARTIST_DIR.exists():
        print(f"[ERROR] data/artist/ ディレクトリが存在しません")
        sys.exit(1)

    artist_files = sorted(ARTIST_DIR.glob("*.json"))
    artist_hashes = {}
    for fp in artist_files:
        h = sha256_hex16(fp)
        artist_hashes[fp.stem] = h
        print(f"{fp.name} hash: {h}")

    # version を +1
    old_version = manifest.get("version", 0)
    new_version = old_version + 1

    # updatedAt を現在の JST 時刻に更新
    now_jst = datetime.now(JST)
    updated_at = now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00")

    # manifest を更新（既存のキー構造を保持しつつ更新）
    manifest["version"] = new_version
    manifest["updatedAt"] = updated_at

    # files.artists.hash を更新
    if "files" not in manifest:
        manifest["files"] = {}
    if "artists" not in manifest["files"]:
        manifest["files"]["artists"] = {}
    manifest["files"]["artists"]["hash"] = artists_hash

    # artists.{id}.hash を更新（manifest に元々あるキーのみ更新 + 新規アーティストは追加）
    if "artists" not in manifest:
        manifest["artists"] = {}

    for aid, h in artist_hashes.items():
        if aid not in manifest["artists"]:
            manifest["artists"][aid] = {}
        manifest["artists"][aid]["hash"] = h

    # manifest に存在するが artist/ にないアーティストのエントリは削除
    existing_aids = set(artist_hashes.keys())
    manifest_aids = list(manifest["artists"].keys())
    for aid in manifest_aids:
        if aid not in existing_aids:
            del manifest["artists"][aid]
            print(f"[INFO] manifest から '{aid}' を削除しました（対応ファイルなし）")

    # venues が manifest に存在する場合のみ保持（追加はしない）
    # （manifest の既存キーは上書きせず保持 — 上記の更新で十分）

    # manifest.json を上書き保存（インデント2、末尾改行）
    output = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    MANIFEST_JSON.write_text(output, encoding="utf-8")

    print(f"version: {old_version} → {new_version}")
    print(f"updatedAt: {updated_at}")
    print("✅ manifest.json を更新しました。")


if __name__ == "__main__":
    main()
