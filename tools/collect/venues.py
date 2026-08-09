"""会場名の正規化辞書。

`performance.venue` は配信スキーマ上ただの文字列で、収集のたびに
「バンテリンドーム ナゴヤ」「バンテリンドームナゴヤ」のような揺れが混ざる。
アプリの表示がばらつくうえ、突き合わせの精度も落ちる。

**配信スキーマは変えない**（venueId 参照にするとアプリ2種の同時改修が要る）。
代わりに収集の入口でマスタの正式表記へ寄せて、揺れを発生源で潰す。

マスタに無い会場は素通しする。**未知の会場が出ても収集は止めない。**
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

from .extract import normalize_venue

MASTER_FILE = Path("config/venues.json")


class VenueMaster:
    """正規化キー → 正式表記 の引き当て表。

    キーは `extract.normalize_venue()`（空白・記号・括弧書き・都道府県接頭辞を
    落としたもの）。同じ会場の表記違いは自然に同じキーへ落ちる。
    改称・略称のように綴りから同一と判定できないものだけ `aliases` に書く。
    """

    def __init__(self, venues: Optional[List[Dict]] = None):
        self.venues: List[Dict] = venues or []
        self._by_key: Dict[str, Dict] = {}
        for v in self.venues:
            for name in [v["name"]] + list(v.get("aliases") or []):
                key = normalize_venue(name)
                if key:
                    self._by_key.setdefault(key, v)

    @classmethod
    def load(cls, path: Path = MASTER_FILE) -> "VenueMaster":
        path = Path(path)
        if not path.exists():
            return cls([])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls([])
        return cls(data.get("venues") or [])

    def save(self, path: Path = MASTER_FILE) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"venues": sorted(self.venues, key=lambda v: v["name"])}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    # -- 引き当て ------------------------------------------------------

    def lookup(self, venue: str) -> Optional[Dict]:
        return self._by_key.get(normalize_venue(venue or ""))

    def canonical(self, venue: str) -> str:
        """正式表記を返す。マスタに無ければ入力をそのまま返す。"""
        entry = self.lookup(venue)
        if entry is None:
            return venue
        return entry["name"]

    def prefecture(self, venue: str) -> Optional[str]:
        entry = self.lookup(venue)
        return entry.get("pref") if entry else None

    def __len__(self) -> int:
        return len(self.venues)


def build_from_names(counts: Dict[str, int],
                     prefectures: Optional[Dict[str, str]] = None) -> List[Dict]:
    """会場名の出現回数から、マスタの素案を作る。

    同じ正規化キーに落ちる表記をまとめ、**最も多く使われている表記**を
    正式表記にする。多数派に寄せるのが、既存データの書き換え量が最小になる。
    """
    prefectures = prefectures or {}
    groups: Dict[str, Dict[str, int]] = {}
    for name, n in counts.items():
        # 正式表記に NFKC をかけない。全角括弧「（）」や「＆」を半角に潰してしまい、
        # 日本語の会場名として不自然な表記が正になる
        name = name.strip()
        if not name:
            continue
        key = normalize_venue(name)
        if not key:
            continue
        groups.setdefault(key, {})
        groups[key][name] = groups[key].get(name, 0) + n

    venues: List[Dict] = []
    for key, forms in groups.items():
        # 出現数が同じなら、より短い（= 補足の括弧書きが無い）表記を選ぶ
        canonical = sorted(forms.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
        aliases = sorted(n for n in forms if n != canonical)
        entry: Dict = {"name": canonical, "count": sum(forms.values())}
        pref = prefectures.get(canonical) or next(
            (prefectures[a] for a in aliases if a in prefectures), None)
        entry["pref"] = pref
        if aliases:
            entry["aliases"] = aliases
        venues.append(entry)
    return venues
