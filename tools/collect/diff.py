"""差分検出。ハッシュが同じなら、そこで終わり（AIは呼ばない）。

ハッシュ一致で打ち切るだけでなく、変化した行を切り出せるようにする。
AIへ渡すのは**変わった部分だけ**にしたいため。
"""
import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import normalize

NO_CHANGE = "NO_CHANGE"
CHANGED = "CONTENT_CHANGED"
FIRST_SEEN = "FIRST_SEEN"
# テキストは動いたが、日付にも抽選にも関係しない差分。毎回表示が変わるサイト向け
VOLATILE = "VOLATILE_CHANGE"

# 変化行の前後に付ける文脈行数。日付だけ変わった時に会場名を失わないため
CONTEXT_LINES = 2


@dataclass
class DiffResult:
    status: str
    content_hash: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed_text: str = ""

    @property
    def changed(self) -> bool:
        return self.status != NO_CHANGE


def content_hash(normalized_text: str) -> str:
    stable = normalize.hash_source(normalized_text)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def compare(new_text: str, old_text: Optional[str]) -> DiffResult:
    """正規化テキストを前回と比べる。

    old_text が None（初回）は FIRST_SEEN。既存データがある前提の運用なので
    初回でも全文をAIに投げず、通常のパーサーに渡すだけにする。
    """
    new_hash = content_hash(new_text)
    if old_text is None:
        return DiffResult(status=FIRST_SEEN, content_hash=new_hash,
                          added=new_text.split("\n"), changed_text=new_text)

    if content_hash(old_text) == new_hash:
        return DiffResult(status=NO_CHANGE, content_hash=new_hash)

    old_lines = normalize.hash_source(old_text).split("\n")
    new_lines = normalize.hash_source(new_text).split("\n")
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    added: List[str] = []
    removed: List[str] = []
    keep_idx = set()

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(new_lines[j1:j2])
            lo = max(0, j1 - CONTEXT_LINES)
            hi = min(len(new_lines), j2 + CONTEXT_LINES)
            keep_idx.update(range(lo, hi))
        if tag in ("replace", "delete"):
            removed.extend(old_lines[i1:i2])

    changed_text = "\n".join(new_lines[i] for i in sorted(keep_idx) if new_lines[i].strip())
    return DiffResult(
        status=CHANGED,
        content_hash=new_hash,
        added=[a for a in added if a.strip()],
        removed=[r for r in removed if r.strip()],
        changed_text=changed_text,
    )


class SnapshotStore:
    """前回の正規化テキストを保存する。

    ハッシュだけだと「何が変わったか」が出せず、結局ページ全文をAIに渡す
    ことになる。テキスト実体を持つのは、その分岐を潰すため。
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, artist_id: str, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self.root / artist_id / f"{digest}.txt"

    def load(self, artist_id: str, url: str) -> Optional[str]:
        path = self._path(artist_id, url)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def save(self, artist_id: str, url: str, text: str) -> None:
        path = self._path(artist_id, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def drop(self, artist_id: str) -> None:
        """1アーティスト分のスナップショットを消す。"""
        directory = self.root / artist_id
        if not directory.exists():
            return
        for path in directory.glob("*.txt"):
            path.unlink()

    def promote(self, artist_id: str, target: "SnapshotStore") -> int:
        """保留中のスナップショットを確定側へ移す。戻り値は移した件数。"""
        directory = self.root / artist_id
        if not directory.exists():
            return 0
        moved = 0
        dest_dir = target.root / artist_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in directory.glob("*.txt"):
            (dest_dir / path.name).write_text(path.read_text(encoding="utf-8"),
                                              encoding="utf-8")
            path.unlink()
            moved += 1
        return moved

    def prune(self, keep: List[Tuple[str, str]]) -> int:
        """使われなくなったスナップショットを消す。戻り値は削除数。"""
        wanted = {self._path(a, u) for a, u in keep}
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.rglob("*.txt"):
            if path not in wanted:
                path.unlink()
                removed += 1
        return removed
