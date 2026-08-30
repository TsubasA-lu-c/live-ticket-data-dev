#!/usr/bin/env python3
import base64
import json
import re
import zlib
from pathlib import Path

ROOT = Path('.')
BASELINE = 'dceebe70c8b1f55055e73ecd4608bd41ad498ca9'
VERIFIED = '2026-08-30T00:00:00+09:00'
EXPECTED_PREFIX = [
    '04_limited_sazabys','and_team','ateez','be_first','boku_aozora',
    'boynextdoor','bullet_train','candy_tune','cutie_street','da_ice',
    'equal_love','fruits_zipper','hana','illit','ini','jo1','kroi',
    'macaroni_empitsu','masayoshi_oishi','mazzel','milk','miura_daichi',
    'momoiro_clover_z','monaki','nakajima_kento','nogizaka46',
    'not_equal_me','oishi_masayoshi','one_n_only','one_ok_rock','saucy_dog',
]


def recover_prefix():
    encoded = ''.join(
        p.read_text(encoding='utf-8').strip()
        for p in sorted((ROOT / '.github/reviewed-updates-20260830-final').glob('chunk-*.txt'))
    )
    raw = base64.b64decode(encoded + '=' * ((4 - len(encoded) % 4) % 4))
    decoded = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw).decode('utf-8')
    marker = re.search(r'"artists"\s*:\s*\{', decoded)
    if not marker:
        raise SystemExit('[ERROR] trusted payload has no artists object')
    pos = marker.end()
    dec = json.JSONDecoder()
    artists = {}
    incomplete = None

    def skip(i):
        while i < len(decoded) and decoded[i] in ' \t\r\n,':
            i += 1
        return i

    while True:
        pos = skip(pos)
        if pos >= len(decoded) or decoded[pos] == '}':
            break
        try:
            key, end = dec.raw_decode(decoded, pos)
        except json.JSONDecodeError:
            break
        pos = skip(end)
        if pos >= len(decoded) or decoded[pos] != ':':
            raise SystemExit(f'[ERROR] malformed trusted payload after artist key {key!r}')
        pos = skip(pos + 1)
        try:
            value, end = dec.raw_decode(decoded, pos)
        except json.JSONDecodeError:
            incomplete = key
            break
        artists[key] = value
        pos = end

    if list(artists) != EXPECTED_PREFIX:
        raise SystemExit(f'[ERROR] trusted artist prefix mismatch: {list(artists)}')
    if incomplete != 'sky_hi':
        raise SystemExit(f'[ERROR] expected truncated sky_hi, got {incomplete!r}')
    return artists


def tail_ops():
    v = VERIFIED
    sky_tour = 'sky_hi_event_2026_toyosu_pit'
    sky_perf = [
        'sky_hi_event_2026_toyosu_pit_toyosu_1211',
        'sky_hi_event_2026_toyosu_pit_toyosu_1212',
        'sky_hi_event_2026_toyosu_pit_toyosu_1213',
    ]
    haneda = [
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1027',
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1028',
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1104',
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1105',
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1110',
        'yabai_tshirts_yasan_mtp_tour_2026_haneda_1111',
    ]
    return {
        'sky_hi': {
            'toursUpsert': [{
                'id': sky_tour, 'artistId': 'sky_hi', 'title': 'SKY-HI Birthday Bash 2026 -X-',
                'startDate': '2026-12-11T00:00:00+09:00', 'endDate': '2026-12-13T00:00:00+09:00',
                'prices': [{'label':'スタンディング','amount':9000,'currency':'JPY'}],
                'source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v,
            }],
            'performancesUpsert': [
                {'id':sky_perf[0],'tourId':sky_tour,'venue':'豊洲PIT','performanceAt':'2026-12-11T19:00:00+09:00','doorOpenAt':'2026-12-11T18:00:00+09:00','kind':'oneman','eventName':'SKY-HI X UVERworld','source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
                {'id':sky_perf[1],'tourId':sky_tour,'venue':'豊洲PIT','performanceAt':'2026-12-12T18:30:00+09:00','doorOpenAt':'2026-12-12T17:30:00+09:00','kind':'oneman','eventName':'SKY-HI X FRIENDS','source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
                {'id':sky_perf[2],'tourId':sky_tour,'venue':'豊洲PIT','performanceAt':'2026-12-13T18:30:00+09:00','doorOpenAt':'2026-12-13T17:30:00+09:00','kind':'oneman','eventName':'SKY-HI X SKY-HI','source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
            ],
            'lotteriesUpsert': [
                {'id':sky_tour+'_flyers_architect','tourId':sky_tour,'type':'FLYERS／B-Town〈Architect〉先行','entryStartAt':'2026-08-24T18:00:00+09:00','entryEndAt':'2026-08-31T23:59:00+09:00','resultAt':'2026-09-05T12:00:00+09:00','paymentStartAt':None,'paymentEndAt':None,'performanceIds':sky_perf,'source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
                {'id':sky_tour+'_resident','tourId':sky_tour,'type':'B-Town〈Resident〉先行','entryStartAt':'2026-09-05T15:00:00+09:00','entryEndAt':'2026-09-10T23:59:00+09:00','resultAt':'2026-09-17T12:00:00+09:00','paymentStartAt':None,'paymentEndAt':None,'performanceIds':sky_perf,'source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
                {'id':sky_tour+'_second','tourId':sky_tour,'type':'FLYERS 2次／B-Town各2次先行','entryStartAt':'2026-09-17T15:00:00+09:00','entryEndAt':'2026-09-23T23:59:00+09:00','resultAt':'2026-09-30T12:00:00+09:00','paymentStartAt':None,'paymentEndAt':None,'performanceIds':sky_perf,'source':'system','sourceUrl':'https://skyhi.tokyo/','lastVerifiedAt':v},
            ],
        },
        'wanima': {
            'toursUpsert': [
                {'id':'wanima_what_a_wonderful_world_2026','artistId':'wanima','title':'What a Wonderful World !! 26','startDate':'2026-11-07T00:00:00+09:00','endDate':'2026-11-07T00:00:00+09:00','prices':None,'source':'system','sourceUrl':'https://www800.asia/2026/about/','lastVerifiedAt':v},
                {'id':'wanima_kishidan_banpaku_2026','artistId':'wanima','title':'サントリー オールフリー presents 氣志團万博2026 ～房総爆音リゾート～','startDate':'2026-11-08T00:00:00+09:00','endDate':'2026-11-08T00:00:00+09:00','prices':None,'source':'system','sourceUrl':'https://www.red-hot.ne.jp/play/detail.php?pid=py28189','lastVerifiedAt':v},
            ],
            'performancesUpsert': [
                {'id':'wanima_what_a_wonderful_world_2026_1107','tourId':'wanima_what_a_wonderful_world_2026','venue':'宜野湾港マリーナ・トロピカルビーチ特設会場','performanceAt':'2026-11-07T13:00:00+09:00','doorOpenAt':'2026-11-07T11:00:00+09:00','kind':'fes','eventName':'What a Wonderful World !! 26','source':'system','sourceUrl':'https://www800.asia/2026/about/','lastVerifiedAt':v},
                {'id':'wanima_kishidan_banpaku_2026_1108','tourId':'wanima_kishidan_banpaku_2026','venue':'幕張メッセ国際展示場 4～7ホール','performanceAt':'2026-11-08T10:30:00+09:00','doorOpenAt':'2026-11-08T09:00:00+09:00','kind':'fes','eventName':'サントリー オールフリー presents 氣志團万博2026 ～房総爆音リゾート～','source':'system','sourceUrl':'https://www.red-hot.ne.jp/play/detail.php?pid=py28189','lastVerifiedAt':v},
            ],
        },
        'yabai_tshirts_yasan': {
            'lotteriesUpsert': [
                {'id':'yabai_tshirts_yasan_mtp_tour_2026_eplus_preorder_haneda','tourId':'yabai_tshirts_yasan_mtp_tour_2026','type':'Zepp Haneda公演 プレオーダー先行（e+）','entryStartAt':'2026-08-07T12:00:00+09:00','entryEndAt':'2026-08-19T23:59:00+09:00','resultAt':None,'paymentStartAt':None,'paymentEndAt':None,'performanceIds':haneda,'source':'system','sourceUrl':'https://yabaitshirtsyasan.com/mtp_tour/','lastVerifiedAt':v},
                {'id':'yabai_tshirts_yasan_mtp_tour_2026_pia_prereserve_haneda','tourId':'yabai_tshirts_yasan_mtp_tour_2026','type':'Zepp Haneda公演 プレリザーブ先行（ぴあ）','entryStartAt':'2026-08-07T12:00:00+09:00','entryEndAt':'2026-08-19T23:59:00+09:00','resultAt':None,'paymentStartAt':None,'paymentEndAt':None,'performanceIds':haneda,'source':'system','sourceUrl':'https://yabaitshirtsyasan.com/mtp_tour/','lastVerifiedAt':v},
            ],
        },
        'yuzu': {
            'toursUpsert': [{
                'id':'yuzu_wonderlivet_2026','artistId':'yuzu','title':'WONDERLIVET 2026',
                'startDate':'2026-11-22T00:00:00+09:00','endDate':'2026-11-22T00:00:00+09:00',
                'prices':None,'source':'system','sourceUrl':'https://yuzu-official.com/','lastVerifiedAt':v,
            }],
            'performancesUpsert': [{
                'id':'yuzu_wonderlivet_2026_1122','tourId':'yuzu_wonderlivet_2026',
                'venue':'KINTEX 第2展示場 HALL 7・8・9・10','performanceAt':'2026-11-22T12:00:00+09:00',
                'doorOpenAt':None,'kind':'fes','eventName':'WONDERLIVET 2026','source':'system',
                'sourceUrl':'https://yuzu-official.com/','lastVerifiedAt':v,
                'performanceDate':'2026-11-22','performanceTimeEstimated':True,
            }],
        },
    }


def upsert(items, updates):
    items = list(items or [])
    index = {item['id']: i for i, item in enumerate(items)}
    for item in list(updates or []):
        item_id = item['id']
        if item_id in index:
            items[index[item_id]] = item
        else:
            index[item_id] = len(items)
            items.append(item)
    return items


def main():
    reviewed = recover_prefix()
    reviewed.update(tail_ops())
    if len(reviewed) != 35:
        raise SystemExit(f'[ERROR] expected 35 reviewed artists, got {len(reviewed)}')
    if 'xg' in reviewed or 'yuuri' in reviewed:
        raise SystemExit('[ERROR] excluded old candidates leaked into reviewed payload')

    for artist_id, ops in reviewed.items():
        path = ROOT / 'data' / 'artist' / f'{artist_id}.json'
        if not path.exists():
            raise SystemExit(f'[ERROR] missing artist file: {path}')
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['tours'] = upsert(doc.get('tours'), ops.get('toursUpsert'))
        doc['performances'] = upsert(doc.get('performances'), ops.get('performancesUpsert'))
        doc['lotteries'] = upsert(doc.get('lotteries'), ops.get('lotteriesUpsert'))
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    artists_path = ROOT / 'data' / 'artists.json'
    registry = json.loads(artists_path.read_text(encoding='utf-8'))
    reviewed_ids = set(reviewed)
    found = set()
    for artist in registry:
        if artist.get('id') in reviewed_ids:
            artist['lastVerifiedAt'] = VERIFIED
            found.add(artist['id'])
    if found != reviewed_ids:
        raise SystemExit(f'[ERROR] artists.json missing reviewed ids: {sorted(reviewed_ids - found)}')
    artists_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    normalized = []
    for path in sorted((ROOT / 'data' / 'artist').glob('*.json')):
        doc = json.loads(path.read_text(encoding='utf-8'))
        dirty = False
        for perf in doc.get('performances', []):
            if perf.get('performanceAt') is not None:
                continue
            performance_date = perf.get('performanceDate')
            if not performance_date:
                raise SystemExit(f'[ERROR] {path}: null performanceAt without performanceDate: {perf.get("id")}')
            perf['performanceAt'] = f'{performance_date}T12:00:00+09:00'
            perf['performanceTimeEstimated'] = True
            normalized.append((path.stem, perf.get('id')))
            dirty = True
        if dirty:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    print(f'[OK] reviewed artists applied: {len(reviewed)}')
    print(f'[OK] performanceAt compatibility normalizations: {len(normalized)}')
    for artist_id, perf_id in normalized:
        print(f'  normalized {artist_id}:{perf_id}')


if __name__ == '__main__':
    main()
