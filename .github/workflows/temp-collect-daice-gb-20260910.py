import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
now = datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S+09:00')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def require_ids(items, ids, label):
    found = {x['id'] for x in items}
    missing = set(ids) - found
    if missing:
        raise AssertionError(f'{label} missing expected ids: {sorted(missing)}')


def append_absent(items, obj, label):
    ids = [x['id'] for x in items]
    if obj['id'] in ids:
        raise AssertionError(f'{label} already exists: {obj["id"]}')
    items.append(obj)


# ---------- Da-iCE ----------
daice_path = Path('data/artist/da_ice.json')
daice = load(daice_path)
require_ids(daice['tours'], [
    'da_ice_day_hall_phase_2026',
    'da_ice_dream_festival_2026',
    'da_ice_fes_2026',
], 'Da-iCE tours')
require_ids(daice['performances'], [
    'da_ice_dream_festival_2026_1122',
    'da_ice_fes_2026_anation_1004',
], 'Da-iCE performances')

hall = next(x for x in daice['tours'] if x['id'] == 'da_ice_day_hall_phase_2026')
hall['prices'] = [
    {'label': '指定席（ラミネートパス付き）', 'amount': 13600},
    {'label': '指定席', 'amount': 12500},
    {'label': '着席指定席（ラミネートパス付き・北海道公演のみ）', 'amount': 13600},
    {'label': '着席指定席（北海道公演のみ）', 'amount': 12500},
]
hall['sourceUrl'] = 'https://www.da-ice.jp/news/detail.php?id=1135399'
hall['lastVerifiedAt'] = now

side_a = [
    'da_ice_day_hall_phase_2026_osaka_1013',
    'da_ice_day_hall_phase_2026_aichi_1022',
    'da_ice_day_hall_phase_2026_fukuoka_1025',
    'da_ice_day_hall_phase_2026_yokosuka_1102',
    'da_ice_day_hall_phase_2026_omiya_1104',
    'da_ice_day_hall_phase_2026_kyoto_1108',
    'da_ice_day_hall_phase_2026_tokyo_1117',
    'da_ice_day_hall_phase_2026_kagawa_1124',
    'da_ice_day_hall_phase_2026_kanazawa_1126',
    'da_ice_day_hall_phase_2026_hiroshima_1207',
    'da_ice_day_hall_phase_2026_sapporo_1215',
]
side_b = [
    'da_ice_day_hall_phase_2026_osaka_1014',
    'da_ice_day_hall_phase_2026_gifu_1021',
    'da_ice_day_hall_phase_2026_oita_1027',
    'da_ice_day_hall_phase_2026_wakayama_1029',
    'da_ice_day_hall_phase_2026_ehime_1106',
    'da_ice_day_hall_phase_2026_tokyo_1118',
    'da_ice_day_hall_phase_2026_sendai_1129',
    'da_ice_day_hall_phase_2026_kurashiki_1201',
    'da_ice_day_hall_phase_2026_nagano_1203',
    'da_ice_day_hall_phase_2026_takasaki_1205',
    'da_ice_day_hall_phase_2026_ichikawa_1210',
]
require_ids(daice['performances'], side_a + side_b, 'Da-iCE HALL performances')

hall_source = 'https://www.da-ice.jp/news/detail.php?id=1135399'
append_absent(daice['lotteries'], {
    'id': 'da_ice_day_hall_phase_2026_official_2nd_side_a',
    'tourId': 'da_ice_day_hall_phase_2026',
    'type': 'オフィシャル2次先行（Side A）',
    'entryStartAt': '2026-08-28T15:00:00+09:00',
    'entryEndAt': '2026-09-09T23:59:00+09:00',
    'resultAt': None,
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': side_a,
    'source': 'system',
    'sourceUrl': hall_source,
    'lastVerifiedAt': now,
}, 'Da-iCE HALL official 2nd')
append_absent(daice['lotteries'], {
    'id': 'da_ice_day_hall_phase_2026_ai_2nd_side_b',
    'tourId': 'da_ice_day_hall_phase_2026',
    'type': 'a-i2次先行（Side B）',
    'entryStartAt': '2026-08-28T15:00:00+09:00',
    'entryEndAt': '2026-09-09T23:59:00+09:00',
    'resultAt': None,
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': side_b,
    'source': 'system',
    'sourceUrl': hall_source,
    'lastVerifiedAt': now,
}, 'Da-iCE HALL a-i 2nd')
append_absent(daice['lotteries'], {
    'id': 'da_ice_day_hall_phase_2026_cube_side_b',
    'tourId': 'da_ice_day_hall_phase_2026',
    'type': 'Da-iCE CUBE先行（Side B）',
    'entryStartAt': '2026-08-28T15:00:00+09:00',
    'entryEndAt': '2026-09-09T23:59:00+09:00',
    'resultAt': None,
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': side_b,
    'source': 'system',
    'sourceUrl': hall_source,
    'lastVerifiedAt': now,
}, 'Da-iCE HALL CUBE')

append_absent(daice['lotteries'], {
    'id': 'da_ice_dream_festival_2026_official_2nd',
    'tourId': 'da_ice_dream_festival_2026',
    'type': 'オフィシャル2次先行',
    'entryStartAt': '2026-09-07T12:00:00+09:00',
    'entryEndAt': '2026-09-13T23:59:00+09:00',
    'resultAt': None,
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': ['da_ice_dream_festival_2026_1122'],
    'source': 'system',
    'sourceUrl': 'https://www.dreamfestival.jp/',
    'lastVerifiedAt': now,
}, 'Da-iCE Dream Festival official 2nd')

append_absent(daice['lotteries'], {
    'id': 'da_ice_fes_2026_anation_s_upgrade',
    'tourId': 'da_ice_fes_2026',
    'type': '一般指定席（光るウチワ付き）S席アップグレード抽選',
    'entryStartAt': '2026-09-09T15:00:00+09:00',
    'entryEndAt': '2026-09-13T23:59:00+09:00',
    'resultAt': '2026-09-17T15:00:00+09:00',
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': ['da_ice_fes_2026_anation_1004'],
    'source': 'system',
    'sourceUrl': 'https://www.da-ice.jp/en/news/detail.php?id=1135885',
    'lastVerifiedAt': now,
}, 'Da-iCE a-nation S upgrade')

save(daice_path, daice)

# ---------- Golden Bomber ----------
gb_path = Path('data/artist/golden_bomber.json')
gb = load(gb_path)
tour_id = 'golden_bomber_hiroshima_continew_fes_2026'
perf_id = 'golden_bomber_hiroshima_continew_fes_2026_hiroshima_1206'
lottery_id = 'golden_bomber_hiroshima_continew_fes_2026_official_3rd'
if any(x['id'] == tour_id for x in gb['tours']):
    raise AssertionError(f'Golden Bomber tour already exists: {tour_id}')
if any(x['id'] == perf_id for x in gb['performances']):
    raise AssertionError(f'Golden Bomber performance already exists: {perf_id}')
if any(x['id'] == lottery_id for x in gb['lotteries']):
    raise AssertionError(f'Golden Bomber lottery already exists: {lottery_id}')

gb['tours'].append({
    'id': tour_id,
    'artistId': 'golden_bomber',
    'title': 'HIROSHIMA CONTI-NeW FeS 2026',
    'startDate': '2026-12-06T00:00:00+09:00',
    'endDate': '2026-12-06T00:00:00+09:00',
    'prices': None,
    'source': 'system',
    'sourceUrl': 'https://www.hiroshima-continew.com/',
    'lastVerifiedAt': now,
})
gb['performances'].append({
    'id': perf_id,
    'tourId': tour_id,
    'venue': '広島グリーンアリーナ',
    'performanceAt': '2026-12-06T11:30:00+09:00',
    'doorOpenAt': '2026-12-06T10:00:00+09:00',
    'kind': 'fes',
    'eventName': 'HIROSHIMA CONTI-NeW FeS 2026',
    'source': 'system',
    'sourceUrl': 'https://eplus.jp/sf/detail/4401630001-P0030002P021002',
    'lastVerifiedAt': now,
})
gb['lotteries'].append({
    'id': lottery_id,
    'tourId': tour_id,
    'type': 'オフィシャル三次先行（先着）',
    'entryStartAt': '2026-09-10T12:00:00+09:00',
    'entryEndAt': '2026-10-01T23:59:00+09:00',
    'resultAt': None,
    'paymentStartAt': None,
    'paymentEndAt': None,
    'performanceIds': [perf_id],
    'source': 'system',
    'sourceUrl': 'https://eplus.jp/sf/detail/4401630001-P0030002P021002',
    'lastVerifiedAt': now,
})
save(gb_path, gb)

# ---------- Artist verification metadata ----------
artists_path = Path('data/artists.json')
artists = load(artists_path)
by_id = {x['id']: x for x in artists}
for aid in ('da_ice', 'golden_bomber'):
    if aid not in by_id:
        raise AssertionError(f'artist not found in artists.json: {aid}')
    by_id[aid]['lastVerifiedAt'] = now
save(artists_path, artists)

# ---------- Deterministic validation ----------
for path in sorted(Path('data').rglob('*.json')):
    json.loads(path.read_text(encoding='utf-8'))

for path in (daice_path, gb_path):
    data = load(path)
    tour_ids = {x['id'] for x in data['tours']}
    perf_ids = {x['id'] for x in data['performances']}
    lottery_ids = {x['id'] for x in data['lotteries']}
    assert len(tour_ids) == len(data['tours']), f'duplicate tour ids in {path}'
    assert len(perf_ids) == len(data['performances']), f'duplicate performance ids in {path}'
    assert len(lottery_ids) == len(data['lotteries']), f'duplicate lottery ids in {path}'
    for p in data['performances']:
        assert p['tourId'] in tour_ids, f'orphan performance {p["id"]}'
    for lottery in data['lotteries']:
        assert lottery['tourId'] in tour_ids, f'orphan lottery tour {lottery["id"]}'
        for pid in lottery.get('performanceIds', []):
            assert pid in perf_ids, f'orphan lottery performance {lottery["id"]}: {pid}'

daice = load(daice_path)
daice_lotteries = {x['id']: x for x in daice['lotteries']}
assert daice_lotteries['da_ice_dream_festival_2026_official_2nd']['entryStartAt'] == '2026-09-07T12:00:00+09:00'
assert daice_lotteries['da_ice_dream_festival_2026_official_2nd']['entryEndAt'] == '2026-09-13T23:59:00+09:00'
assert daice_lotteries['da_ice_fes_2026_anation_s_upgrade']['resultAt'] == '2026-09-17T15:00:00+09:00'

gb = load(gb_path)
gb_perf = {x['id']: x for x in gb['performances']}[perf_id]
gb_lottery = {x['id']: x for x in gb['lotteries']}[lottery_id]
assert gb_perf['performanceAt'] == '2026-12-06T11:30:00+09:00'
assert gb_perf['doorOpenAt'] == '2026-12-06T10:00:00+09:00'
assert gb_lottery['entryStartAt'] == '2026-09-10T12:00:00+09:00'
assert gb_lottery['entryEndAt'] == '2026-10-01T23:59:00+09:00'

# ---------- Manifest ----------
manifest_path = Path('data/manifest.json')
manifest = load(manifest_path)
old_version = int(manifest.get('version', 0))
manifest['version'] = old_version + 1
manifest['updatedAt'] = now
artists_hash = hashlib.sha256(artists_path.read_bytes()).hexdigest()[:16]
manifest.setdefault('files', {}).setdefault('artists', {})['hash'] = artists_hash
manifest.setdefault('artists', {})
for path in sorted(Path('data/artist').glob('*.json')):
    aid = path.stem
    manifest['artists'].setdefault(aid, {})['hash'] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
existing = {p.stem for p in Path('data/artist').glob('*.json')}
for aid in list(manifest['artists']):
    if aid not in existing:
        del manifest['artists'][aid]
save(manifest_path, manifest)

print(f'verifiedAt: {now}')
print(f'manifest version: {old_version} -> {manifest["version"]}')
print('Da-iCE: 5 lottery records added; HALL prices backfilled')
print('Golden Bomber: HIROSHIMA CONTI-NeW FeS 2026 tour/performance/presale added')
