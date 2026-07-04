#!/usr/bin/env python3
"""
migrate_to_per_artist.py

Migrates live-ticket-data from single-file format to per-artist format.

Before:
  data/tours.json
  data/performances.json
  data/lotteries.json

After:
  data/artist/{artistId}.json  (one file per artist)
"""

import json
import hashlib
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
ARTIST_DIR = os.path.join(DATA_DIR, 'artist')


def sha256_hex16(content: str) -> str:
    """Return first 16 hex characters of SHA-256 of the given string."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]


def read_json(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json(path: str, data, indent: int = 2):
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
        f.write('\n')
    return content


def main():
    # ---- 1. Load existing data ----
    artists = read_json(os.path.join(DATA_DIR, 'artists.json'))
    tours = read_json(os.path.join(DATA_DIR, 'tours.json'))
    performances = read_json(os.path.join(DATA_DIR, 'performances.json'))
    lotteries = read_json(os.path.join(DATA_DIR, 'lotteries.json'))

    artist_ids = [a['id'] for a in artists]
    print(f"Artists: {len(artist_ids)}")
    print(f"Tours:   {len(tours)}")
    print(f"Performances: {len(performances)}")
    print(f"Lotteries:    {len(lotteries)}")

    # ---- 2. Build lookup maps ----
    # tourId -> artistId
    tour_to_artist = {t['id']: t['artistId'] for t in tours}

    # artistId -> [tours]
    tours_by_artist = {aid: [] for aid in artist_ids}
    for t in tours:
        aid = t['artistId']
        if aid in tours_by_artist:
            tours_by_artist[aid].append(t)
        else:
            print(f"  WARNING: tour {t['id']} has unknown artistId '{aid}'")

    # artistId -> [performances]  (via tourId -> artistId)
    performances_by_artist = {aid: [] for aid in artist_ids}
    for p in performances:
        tid = p['tourId']
        aid = tour_to_artist.get(tid)
        if aid:
            if aid in performances_by_artist:
                performances_by_artist[aid].append(p)
            else:
                print(f"  WARNING: performance {p['id']} maps to unknown artistId '{aid}'")
        else:
            print(f"  WARNING: performance {p['id']} has unknown tourId '{tid}'")

    # artistId -> [lotteries]  (via tourId -> artistId)
    lotteries_by_artist = {aid: [] for aid in artist_ids}
    for lot in lotteries:
        tid = lot['tourId']
        aid = tour_to_artist.get(tid)
        if aid:
            if aid in lotteries_by_artist:
                lotteries_by_artist[aid].append(lot)
            else:
                print(f"  WARNING: lottery {lot['id']} maps to unknown artistId '{aid}'")
        else:
            print(f"  WARNING: lottery {lot['id']} has unknown tourId '{tid}'")

    # ---- 3. Create data/artist/ directory ----
    os.makedirs(ARTIST_DIR, exist_ok=True)

    # ---- 4. Write per-artist files ----
    artist_hashes = {}
    generated_files = []
    for aid in artist_ids:
        artist_data = {
            "artistId": aid,
            "tours": tours_by_artist[aid],
            "performances": performances_by_artist[aid],
            "lotteries": lotteries_by_artist[aid],
        }
        out_path = os.path.join(ARTIST_DIR, f"{aid}.json")
        content = write_json(out_path, artist_data)
        h = sha256_hex16(content)
        artist_hashes[aid] = h
        generated_files.append(f"  data/artist/{aid}.json  (hash: {h})")
        print(f"  Generated: data/artist/{aid}.json")

    print(f"\nGenerated {len(generated_files)} artist files.")

    # ---- 5. Update artists.json: add genre field, ensure aliases field ----
    # Canonical field order: id, name, aliases, genre, imageUrl, source, sourceUrl, lastVerifiedAt
    updated_artists = []
    for a in artists:
        entry = {
            "id": a["id"],
            "name": a["name"],
            "aliases": a.get("aliases", None),
            "genre": a.get("genre", None),
            "imageUrl": a.get("imageUrl", None),
            "source": a.get("source"),
            "sourceUrl": a.get("sourceUrl"),
            "lastVerifiedAt": a.get("lastVerifiedAt"),
        }
        updated_artists.append(entry)

    artists_content = write_json(os.path.join(DATA_DIR, 'artists.json'), updated_artists)
    artists_hash = sha256_hex16(artists_content)
    print(f"\nUpdated artists.json  (hash: {artists_hash})")

    # ---- 6. Update manifest.json ----
    # Compute venues hash if file exists
    venues_path = os.path.join(DATA_DIR, 'venues.json')
    files_section = {
        "artists": {"hash": artists_hash},
    }
    if os.path.exists(venues_path):
        with open(venues_path, 'r', encoding='utf-8') as f:
            venues_content = f.read()
        files_section["venues"] = {"hash": sha256_hex16(venues_content)}

    manifest = {
        "version": 8,
        "updatedAt": "2026-06-14T00:00:00+09:00",
        "files": files_section,
        "artists": {aid: {"hash": h} for aid, h in artist_hashes.items()},
    }
    write_json(os.path.join(DATA_DIR, 'manifest.json'), manifest)
    print("Updated manifest.json")

    # ---- 7. Summary ----
    print("\n=== Summary ===")
    print(f"  data/artist/ files: {len(generated_files)}")
    for f in generated_files:
        print(f)

    print("\n=== manifest.json (sample - first 3 artists) ===")
    sample_artists = list(manifest['artists'].items())[:3]
    sample = {
        "version": manifest['version'],
        "updatedAt": manifest['updatedAt'],
        "files": manifest['files'],
        "artists": {k: v for k, v in sample_artists},
        "... (remaining artists)": "..."
    }
    print(json.dumps(sample, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
