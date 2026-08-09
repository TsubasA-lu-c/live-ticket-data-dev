"""AIを使わないライブ情報の抽出。

優先順位は仕様どおり
  A. 構造化データ（JSON-LD / microdata の schema.org Event）
  B. DOM構造（一覧の行・カード）
  C. テキスト解析
だが、**日本のアーティストサイトに schema.org/Event はほぼ無い**
（2026-08に40ページ実測して0件）。実際に効くのはBとCなので、
正規化ブロック列に対する日付＋会場の同定を本体として組んである。
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional, Tuple

from .normalize import Block, clean_text

# ---------------------------------------------------------------- 日付

_JP_DATE = re.compile(
    r"(?<![\d\-/.])"
    r"(?:(?P<y>20\d{2})\s*[年./\-]\s*)?"
    r"(?P<m>1[0-2]|0?[1-9])\s*[月./\-]\s*"
    r"(?P<d>3[01]|[12]\d|0?[1-9])\s*日?"
    r"(?![\d\-])"
    r"\s*(?:[(（]\s*[月火水木金土日祝]{1,3}\s*[)）])?"
)

# 電話番号は日付に化ける。「019-622-4770」が 9月6日 として拾われた実例がある
# （2026-08, 刀ミュのライブビューイング上映館一覧）。日付を探す前に潰す。
_PHONE = re.compile(
    r"(?<![\d])(?:"
    r"0\d{1,4}[-‐−ー]\d{1,4}[-‐−ー]\d{3,4}"
    r"|0\d{9,10}"
    r"|\d{2,4}[-‐−ー]\d{3,4}[-‐−ー]\d{4}"
    r")(?![\d])"
)


def mask_phones(text: str) -> str:
    return _PHONE.sub(" ", text)

# 「10月3日(土)・4日(日)」「10/3・4」のような追加日
_EXTRA_DAY = re.compile(
    r"[・,、/／]\s*(?P<d>3[01]|[12]\d|0?[1-9])\s*日"
    r"\s*(?:[(（]\s*[月火水木金土日祝]{1,3}\s*[)）])?"
)

# 「10月3日(土)〜5日(月)」の連日
_DAY_RANGE = re.compile(
    r"[〜~ー−–—-]\s*(?:(?P<m2>1[0-2]|0?[1-9])\s*月\s*)?(?P<d2>3[01]|[12]\d|0?[1-9])\s*日"
)

_TIME = r"([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)"
_OPEN_TIME = re.compile(r"(?:OPEN|開場|開館)\s*[:：]?\s*" + _TIME, re.I)
_START_TIME = re.compile(r"(?:START|開演|開始)\s*[:：]?\s*" + _TIME, re.I)
_TIME_PAIR = re.compile(_TIME + r"\s*[/／・]\s*" + _TIME)
_ANY_TIME = re.compile(_TIME)

# ---------------------------------------------------------------- 会場

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
# 「[東京]」「大阪・」のような略記から正式名へ
_PREF_SHORT = {p[:-1] if p != "北海道" else p: p for p in PREFECTURES}

# 会場名らしさを示す語。1つでも含めば会場行の候補にする
VENUE_TOKENS = re.compile(
    r"(ホール|アリーナ|ドーム|スタジアム|スタヂアム|劇場|シアター|体育館|"
    r"野球場|競技場|球場|能楽堂|"
    # 「メッセ」は「メッセージ」に誤爆する（乃木坂46のページで実際に起きた）
    r"文化会館|市民会館|公会堂|会館|フォーラム|メッセ(?!ージ)|プラザ|センター|"
    r"パーク|ガーデン|スタジオ|ロフト|クラブ|チッタ|キューブ|コロシアム|"
    r"武道館|国技館|城ホール|ZEPP|Zepp|LIQUIDROOM|LIQUID ROOM|BLITZ|"
    r"CLUB|HALL|ARENA|DOME|STADIUM|THEATER|THEATRE|LIVE ?HOUSE|"
    r"WWW|O-EAST|O-WEST|O-nest|Spotify O-|渋谷|新木場|幕張|さいたま|横浜|"
    r"日本ガイシ|ぴあアリーナ|Kアリーナ|ベルーナ|東京ガーデン|有明|"
    r"サンプラザ|NHK|オリンパス|ゼビオ|ロームシアター|カルッツ|"
    r"COAST|SOUND|ANIMA|Veats|Spotify)"
)

# 会場名から都道府県を引く表（頻出会場のみ。無ければ null のままにする）
VENUE_TO_PREF = {
    "日本武道館": "東京都", "東京ドーム": "東京都", "国立競技場": "東京都",
    "東京国際フォーラム": "東京都", "NHKホール": "東京都", "中野サンプラザ": "東京都",
    "有明アリーナ": "東京都", "有明コロシアム": "東京都", "両国国技館": "東京都",
    "明治神宮野球場": "東京都", "味の素スタジアム": "東京都", "東京体育館": "東京都",
    "サンドーム福井": "福井県",
    "武蔵野の森": "東京都", "東京ガーデンシアター": "東京都", "LINE CUBE SHIBUYA": "東京都",
    "渋谷公会堂": "東京都", "Zepp Haneda": "東京都", "Zepp DiverCity": "東京都",
    "Zepp Shinjuku": "東京都", "Zepp Tokyo": "東京都", "新木場STUDIO COAST": "東京都",
    "日本ガイシホール": "愛知県", "Zepp Nagoya": "愛知県", "愛知県体育館": "愛知県",
    "バンテリンドーム": "愛知県", "IGアリーナ": "愛知県",
    "大阪城ホール": "大阪府", "京セラドーム大阪": "大阪府", "Zepp Osaka": "大阪府",
    "Zepp Namba": "大阪府", "フェスティバルホール": "大阪府", "大阪国際会議場": "大阪府",
    "横浜アリーナ": "神奈川県", "Kアリーナ横浜": "神奈川県", "ぴあアリーナMM": "神奈川県",
    "パシフィコ横浜": "神奈川県", "日産スタジアム": "神奈川県", "神奈川県民ホール": "神奈川県",
    "さいたまスーパーアリーナ": "埼玉県", "ベルーナドーム": "埼玉県",
    "幕張メッセ": "千葉県", "ZOZOマリンスタジアム": "千葉県", "LaLa arena": "千葉県",
    "福岡PayPayドーム": "福岡県", "マリンメッセ福岡": "福岡県", "Zepp Fukuoka": "福岡県",
    "西日本総合展示場": "福岡県",
    "札幌ドーム": "北海道", "真駒内": "北海道", "Zepp Sapporo": "北海道",
    "ゼビオアリーナ仙台": "宮城県", "セキスイハイムスーパーアリーナ": "宮城県",
    "ロームシアター京都": "京都府", "神戸ワールド記念ホール": "兵庫県",
    "広島グリーンアリーナ": "広島県", "アスティとくしま": "徳島県",
    "沖縄コンベンションセンター": "沖縄県",
}

# 会場ではないと分かっている語（誤検出よけ）
VENUE_STOPWORDS = re.compile(
    r"(チケット|受付|抽選|先行|申込|発売|一般|会員|入金|当落|"
    r"配信|グッズ|物販|リリース|発表|お知らせ|详细|詳細|一覧|"
    r"プレイガイド|注意事項|お問い合わせ|ファンクラブ|"
    # 表の見出し行・案内文が会場として拾われた実例（2026-08, 刀ミュ）
    r"都道府県|劇場名|電話番号|上映館|上映|販売|購入|座席|料金|"
    r"入場|終了|開催|決定|special|SPECIAL|"
    # 「OFFICIAL FAN CLUB 「Ringo Jam」」が CLUB に一致して会場になった
    r"FAN ?CLUB|FANCLUB|会員|OFFICIAL SHOP|STORE|グッズ)"
)

# 会場名に混ざり得ない記号。案内文をここで落とす
_NOT_VENUE_CHARS = re.compile(r"[。※！!？?…]|\d{3,}")

# ---------------------------------------------------------------- 抽選

LOTTERY_KEYWORDS = re.compile(
    r"(先行|抽選|受付|申込|申し込み|エントリー|当落|当選|落選|入金|"
    r"支払|決済|一般発売|先着|FC|ファンクラブ|プレオーダー|オフィシャル)"
)


@dataclass
class ExtractedEvent:
    date: str = ""                      # YYYY-MM-DD
    venue: str = ""
    prefecture: Optional[str] = None
    title: Optional[str] = None
    open_time: Optional[str] = None     # HH:MM
    start_time: Optional[str] = None    # HH:MM
    detail_url: Optional[str] = None
    source_url: str = ""
    source_text: str = ""               # 根拠となった1行（報告・検証用）
    method: str = ""                    # jsonld / dom / text
    confidence: float = 0.0
    year_inferred: bool = False         # 年が書かれておらず推測した

    def key(self) -> Tuple[str, str]:
        return (self.date, normalize_venue(self.venue))

    def to_dict(self) -> Dict:
        return {
            "date": self.date,
            "venue": self.venue,
            "prefecture": self.prefecture,
            "title": self.title,
            "openTime": self.open_time,
            "startTime": self.start_time,
            "detailUrl": self.detail_url,
            "sourceUrl": self.source_url,
            "sourceText": self.source_text,
            "method": self.method,
            "confidence": round(self.confidence, 2),
            "yearInferred": self.year_inferred,
        }


@dataclass
class ExtractResult:
    events: List[ExtractedEvent] = field(default_factory=list)
    method: str = "none"
    lottery_blocks: List[str] = field(default_factory=list)
    date_lines_without_venue: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """パーサー成功とみなせるか（日付＋会場が揃った公演が1件以上）。"""
        return any(e.confidence >= 0.6 for e in self.events)


# ---------------------------------------------------------------- 正規化

def normalize_venue(venue: str) -> str:
    """重複判定用に会場名を丸める。表記ゆれで別公演にしないため。"""
    v = unicodedata.normalize("NFKC", venue or "").lower()
    v = re.sub(r"[\s　]+", "", v)
    v = re.sub(r"[(（\[【].*?[)）\]】]", "", v)          # 括弧書きの補足を落とす
    v = re.sub(r"[・･,、/／|｜:：\-−–—~〜]", "", v)
    v = re.sub(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)", "", v)
    for suffix in ("公演", "会場", "にて"):
        if v.endswith(suffix):
            v = v[: -len(suffix)]
    return v


def normalize_time(hh: str, mm: str) -> str:
    return f"{int(hh):02d}:{int(mm):02d}"


def _to_iso(year: int, month: int, day: int) -> Optional[str]:
    try:
        return _date(year, month, day).isoformat()
    except ValueError:
        return None


def find_dates(text: str, default_year: Optional[int] = None,
               today: Optional[_date] = None) -> List[str]:
    return [iso for iso, _ in find_dates_flagged(text, default_year, today)]


def find_dates_flagged(text: str, default_year: Optional[int] = None,
                       today: Optional[_date] = None) -> List[Tuple[str, bool]]:
    """テキストから (YYYY-MM-DD, 年を推測したか) の並びを返す。

    年が書かれていない「10/3(金)」は、**今日以降で最も近い年**に寄せる。
    ライブ告知は基本的に未来のものなので、この寄せ方でおおむね当たる。
    ただし過去の公演を報じる記事では外れる（8月時点の「7月4日」を翌年にしてしまう）ため、
    推測したことを呼び出し側に伝えて、機械だけで確定させないようにする。
    """
    today = today or _date.today()
    found: List[Tuple[str, bool]] = []
    seen = set()
    text = mask_phones(unicodedata.normalize("NFKC", text))

    def add(iso: Optional[str], inferred: bool) -> bool:
        if not iso or iso in seen:
            return False
        seen.add(iso)
        found.append((iso, inferred))
        return True

    for m in _JP_DATE.finditer(text):
        year = int(m.group("y")) if m.group("y") else None
        month, day = int(m.group("m")), int(m.group("d"))
        inferred = year is None
        if inferred:
            year = default_year or today.year
            iso = _to_iso(year, month, day)
            if iso and iso < today.isoformat():
                iso = _to_iso(year + 1, month, day)
        else:
            iso = _to_iso(year, month, day)
        if not add(iso, inferred):
            continue

        tail = text[m.end(): m.end() + 40]
        base_year, base_month = int(iso[:4]), int(iso[5:7])

        rng = _DAY_RANGE.match(tail)
        if rng:
            end_month = int(rng.group("m2")) if rng.group("m2") else base_month
            end_day = int(rng.group("d2"))
            cur = _date(base_year, base_month, day)
            end_iso = _to_iso(base_year, end_month, end_day)
            if end_iso and end_iso > iso:
                end = _date.fromisoformat(end_iso)
                # 連日公演は最大10日まで展開する（それ以上は掲載期間の表記が疑わしい）
                while (end - cur).days > 0 and (end - cur).days <= 10:
                    cur = _date.fromordinal(cur.toordinal() + 1)
                    add(cur.isoformat(), inferred)
            continue

        for extra in _EXTRA_DAY.finditer(tail):
            add(_to_iso(base_year, base_month, int(extra.group("d"))), inferred)

    return found


def find_times(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(開場, 開演) を返す。ラベルが無い「17:00/18:00」も対応する。"""
    text = unicodedata.normalize("NFKC", text)
    open_m = _OPEN_TIME.search(text)
    start_m = _START_TIME.search(text)
    open_t = normalize_time(*open_m.groups()) if open_m else None
    start_t = normalize_time(*start_m.groups()) if start_m else None

    if open_t is None and start_t is None:
        pair = _TIME_PAIR.search(text)
        if pair:
            a, b, c, d = pair.groups()
            open_t, start_t = normalize_time(a, b), normalize_time(c, d)
    return open_t, start_t


def find_prefecture(text: str, venue: str = "") -> Optional[str]:
    text = unicodedata.normalize("NFKC", text)
    for pref in PREFECTURES:
        if pref in text:
            return pref
    for short, full in _PREF_SHORT.items():
        # 「[東京]」「大阪・」のように区切り記号を伴う場合のみ採用する
        if re.search(r"[\[\(【「/／・|｜\s]" + re.escape(short) + r"[\]\)】」/／・|｜\s:：]", text):
            return full
    for name, pref in VENUE_TO_PREF.items():
        if name.lower() in (venue or "").lower() or name.lower() in text.lower():
            return pref
    return None


def find_venue(text: str) -> Optional[str]:
    """行から会場名を切り出す。取れなければ None（推測で埋めない）。"""
    raw = clean_text(text).replace("\n", " ")
    if not raw:
        return None

    # 電話番号・日付・時刻・都道府県表記を落としてから会場語を探す
    stripped = _JP_DATE.sub(" ", mask_phones(unicodedata.normalize("NFKC", raw)))
    stripped = re.sub(_TIME, " ", stripped)
    stripped = re.sub(r"(OPEN|START|開場|開演|開始|会場)\s*[:：]?", " ", stripped, flags=re.I)

    # 「[東京] 日本武道館」のような括弧書きの地域表記は区切りとして扱う。
    # 括弧ごと会場名に混ぜると重複判定のキーが崩れる
    for chunk in re.split(r"[／/｜|・,、\t\[\]【】（）()]|\s{2,}", stripped):
        chunk = chunk.strip(" :：-−–—")
        # 会場名が30字を超えることはまずない。長い塊は案内文とみなす
        if len(chunk) < 2 or len(chunk) > 30:
            continue
        if VENUE_STOPWORDS.search(chunk) or _NOT_VENUE_CHARS.search(chunk):
            continue
        if not VENUE_TOKENS.search(chunk):
            continue
        for pref in PREFECTURES:
            if chunk.startswith(pref):
                chunk = chunk[len(pref):].strip(" :：-")
        return chunk.strip()
    return None


# ---------------------------------------------------------------- A. 構造化データ

_JSONLD_BLOCK = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)


def extract_jsonld_events(html: str, page_url: str) -> List[ExtractedEvent]:
    """schema.org/Event を拾う。存在すれば最も信頼できる。"""
    events: List[ExtractedEvent] = []
    for m in _JSONLD_BLOCK.finditer(html or ""):
        payload = m.group(1).strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for node in _iter_jsonld_nodes(data):
            ev = _jsonld_to_event(node, page_url)
            if ev:
                events.append(ev)
    return events


def _iter_jsonld_nodes(data):
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            yield cur
            for value in cur.values():
                if isinstance(value, (list, dict)):
                    stack.append(value)


def _jsonld_to_event(node: Dict, page_url: str) -> Optional[ExtractedEvent]:
    types = node.get("@type")
    types = [types] if isinstance(types, str) else (types or [])
    if not any("event" in str(t).lower() for t in types):
        return None

    start = str(node.get("startDate") or "")
    date_part = start[:10]
    if not re.match(r"^20\d{2}-\d{2}-\d{2}$", date_part):
        return None

    location = node.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    venue = ""
    address_text = ""
    if isinstance(location, dict):
        venue = str(location.get("name") or "")
        address = location.get("address")
        if isinstance(address, dict):
            address_text = " ".join(
                str(address.get(k) or "") for k in ("addressRegion", "addressLocality", "streetAddress")
            )
        elif isinstance(address, str):
            address_text = address
    elif isinstance(location, str):
        venue = location

    if not venue:
        return None

    start_time = None
    if len(start) >= 16 and "T" in start:
        start_time = start[11:16]

    return ExtractedEvent(
        date=date_part,
        venue=clean_text(venue),
        prefecture=find_prefecture(address_text + " " + venue, venue),
        title=clean_text(str(node.get("name") or "")) or None,
        start_time=start_time,
        detail_url=str(node.get("url") or "") or None,
        source_url=page_url,
        source_text=f"JSON-LD Event: {venue} {start}",
        method="jsonld",
        confidence=0.95,
    )


# ---------------------------------------------------------------- B/C. ブロック解析

_HEADING_PATHS = {"h1", "h2", "h3", "h4"}

# 会場見出しが何ブロック先の日付まで有効か。
# 10 にしていたとき、乃木坂46のページで8月福岡公演の会場が8月東京公演の日付にまで
# 引きずられた。**間違った会場を入れるくらいなら取らないほうがよい**ので短くする。
VENUE_CONTEXT_SPAN = 5

# 会場コンテキストとして採用してよい行の長さ。これを超える行は説明文か記事見出し
VENUE_LINE_MAX_CHARS = 40


def extract_from_blocks(blocks: List[Block], page_url: str,
                        today: Optional[_date] = None) -> ExtractResult:
    """正規化ブロック列から公演を組み立てる。

    1ブロックに日付と会場が揃っていればその場で確定。
    揃っていない（dt/dd や2カラムのテーブルで分かれている）場合だけ、
    後続2ブロックまで会場を探しに行く。
    """
    today = today or _date.today()
    result = ExtractResult(method="text")
    current_title: Optional[str] = None
    # 「会場見出し → 日付を複数行」というレイアウトのための会場コンテキスト。
    # 乃木坂46のツアーページがこの形で、後ろだけを見ていると全公演を取りこぼす
    current_venue: Optional[str] = None
    current_venue_idx = -999
    seen: Dict[Tuple[str, str], ExtractedEvent] = {}

    for idx, block in enumerate(blocks):
        text = block.text
        if not text:
            continue

        if block.path in _HEADING_PATHS and len(text) <= 120:
            current_title = text.replace("\n", " ").strip()

        dates = find_dates_flagged(text, today=today)

        if not dates:
            # 日付を伴わない会場行は、以降の日付行が参照する会場として覚えておく。
            # ただし**見出し、または会場名だけの短い行に限る**。
            # ニュース一覧の記事見出し（会場名を含む長い文）を会場として覚えると、
            # 記事の投稿日を公演日として拾ってしまう
            # （2026-08, Mrs. GREEN APPLE のnewsページで実際に発生）
            if block.path in _HEADING_PATHS or len(text) <= VENUE_LINE_MAX_CHARS:
                standalone_venue = find_venue(text)
                if standalone_venue:
                    current_venue, current_venue_idx = standalone_venue, idx

        # 抽選候補は「抽選語 かつ 日付か時刻がある」行だけにする。
        # 「チケット」「受付」だけの行はナビや定型文で、AIに渡しても得るものがない
        if LOTTERY_KEYWORDS.search(text) and (dates or _ANY_TIME.search(text)):
            snippet = text.replace("\n", " ").strip()
            if snippet not in result.lottery_blocks:
                result.lottery_blocks.append(snippet)

        if not dates:
            continue

        venue = find_venue(text)
        venue_block_text = text
        from_context = False

        # 直前の会場見出しを、後ろを探すより先に使う。
        # 逆順にすると「会場A / 日付1 / 日付2 / 会場B」の日付2が会場Bを掴む
        if not venue and current_venue and idx - current_venue_idx <= VENUE_CONTEXT_SPAN:
            venue, from_context = current_venue, True

        if not venue:
            for look in blocks[idx + 1: idx + 3]:
                if look.path in _HEADING_PATHS:
                    break  # 見出しから先は別の節
                if find_dates(look.text, today=today):
                    break  # 次の公演行に入ったので打ち切る
                # 記事見出しのような長い文は会場行とみなさない
                if len(look.text) > VENUE_LINE_MAX_CHARS:
                    continue
                venue = find_venue(look.text)
                if venue:
                    venue_block_text = look.text
                    from_context = True
                    break

        if not venue:
            line = text.replace("\n", " ").strip()
            if line not in result.date_lines_without_venue:
                result.date_lines_without_venue.append(line)
            continue

        combined = text + " " + venue_block_text
        open_t, start_t = find_times(combined)

        if from_context and not (open_t or start_t):
            # 会場が同じ行に無く、時刻の裏付けも無い日付は公演と断定できない。
            # **間違った会場を入れるくらいなら AI に回す**
            line = text.replace("\n", " ").strip()
            if line not in result.date_lines_without_venue:
                result.date_lines_without_venue.append(line)
            continue

        detail = _pick_detail_url(block, page_url)

        for d, year_inferred in dates:
            ev = ExtractedEvent(
                date=d,
                year_inferred=year_inferred,
                venue=venue,
                prefecture=find_prefecture(combined, venue),
                title=current_title,
                open_time=open_t,
                start_time=start_t,
                detail_url=detail,
                source_url=page_url,
                source_text=clean_text(combined).replace("\n", " ")[:200],
                method="dom" if block.path in ("li", "tr", "dd", "dt") else "text",
                confidence=_score(d, venue, start_t, len(dates), from_context,
                                  year_inferred),
            )
            key = ev.key()
            prev = seen.get(key)
            if prev is None or ev.confidence > prev.confidence:
                seen[key] = ev

    result.events = sorted(seen.values(), key=lambda e: (e.date, e.venue))
    if result.events:
        result.method = result.events[0].method
    return result


def _score(date_str: str, venue: str, start_time: Optional[str], date_count: int,
           venue_from_context: bool = False, year_inferred: bool = False) -> float:
    """日付＋会場が揃っていれば 0.7 を基点に、材料が増えるほど上げる。"""
    score = 0.7
    if start_time:
        score += 0.15
    if len(venue) >= 4:
        score += 0.05
    if venue_from_context:
        # 同じ行に会場が無く、上の見出しから引いた分は確度を落とす
        score -= 0.1
    if year_inferred:
        # 年が書かれていない日付は、過去記事を未来として読む危険がある
        score -= 0.15
    if date_count > 3:
        # 1行から日付が大量に取れるのは掲載期間や年表の可能性が高い
        score -= 0.25
    return max(0.0, min(1.0, score))


def _pick_detail_url(block: Block, page_url: str) -> Optional[str]:
    for label, url in block.links:
        if not url or url.rstrip("/") == (page_url or "").rstrip("/"):
            continue
        return url
    return None


def extract(html: str, blocks: List[Block], page_url: str,
            today: Optional[_date] = None) -> ExtractResult:
    """A→B/C の順で試す。構造化データがあればそれを優先して混ぜる。"""
    result = extract_from_blocks(blocks, page_url, today=today)

    jsonld = extract_jsonld_events(html, page_url)
    if jsonld:
        by_key = {e.key(): e for e in result.events}
        for ev in jsonld:
            by_key[ev.key()] = ev  # 構造化データで上書きする
        result.events = sorted(by_key.values(), key=lambda e: (e.date, e.venue))
        result.method = "jsonld"
    return result
