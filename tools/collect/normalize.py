"""HTML前処理。AIにHTMLを渡さないための層。

bs4/lxml は入っていない環境なので、標準の html.parser だけで軽量な木を組む。
完全なDOM再現は狙わない。**ライブ情報のテキストとリンクが取れれば十分**。
"""
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# 中身ごと捨てるタグ
DROP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "iframe", "object",
    "embed", "template", "form", "select", "button", "picture", "source",
    "audio", "video", "map", "area",
}

# ライブ情報の解析に不要な領域（中身ごと捨てる）
DROP_ROLE_TAGS = {"nav", "footer", "header", "aside"}

VOID_TAGS = {
    "br", "img", "hr", "input", "meta", "link", "base", "col", "wbr",
    "param", "track", "source", "area", "embed",
}

# class/id に含まれていたら広告・計測・ナビとみなす語
NOISE_PATTERNS = re.compile(
    r"(^|[-_ ])("
    r"ad|ads|adsense|advert|banner|gpt|dfp|taboola|outbrain|"
    r"tracking|analytics|gtm|ga4|pixel|beacon|"
    r"nav|navi|navigation|globalnav|gnav|menu|drawer|hamburger|"
    r"breadcrumb|pankuzu|footer|header|sidebar|social|share|sns|"
    r"cookie|consent|modal|popup|overlay|pagetop|totop|skip"
    r")($|[-_ ])",
    re.I,
)

# 逆に「ここにライブ情報がある」ことを示す語（NOISE より優先して残す）
KEEP_PATTERNS = re.compile(
    r"(live|tour|schedule|event|concert|ticket|news|information|"
    r"performance|gig|date|contents|main|article|entry|post)",
    re.I,
)

# 本文候補として優先的に拾うタグ
MAIN_TAGS = ("main", "article")

BLOCK_TAGS = {
    "p", "div", "li", "tr", "dd", "dt", "section", "article", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "dl", "blockquote",
    "figcaption", "address", "time", "span",
}

_WS = re.compile(r"[ \t　\xa0]+")
_MULTI_NL = re.compile(r"\n{3,}")
# タイムスタンプ・キャッシュバスター等、毎回変わるだけの数字
_VOLATILE_NUM = re.compile(r"\b\d{10,}\b")


@dataclass
class Node:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    text: str = ""  # tag == "#text" のときのみ使う

    def attr(self, name: str) -> str:
        return self.attrs.get(name, "") or ""


@dataclass
class Block:
    """1つの意味のかたまり（見出し・行・カード等）のテキストとリンク。"""

    text: str
    links: List[Tuple[str, str]] = field(default_factory=list)  # (text, url)
    path: str = ""  # 由来タグ（デバッグ用）


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self._stack: List[Node] = [self.root]
        self._drop_depth = 0
        self._drop_tag: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in DROP_TAGS or tag in DROP_ROLE_TAGS:
            self._drop_depth = 1
            self._drop_tag = tag
            return

        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        if _is_hidden(node) or _is_noise(node):
            # 中身ごと落とす。開始タグだけ数えて対応する終了タグまで無視する
            if tag not in VOID_TAGS:
                self._drop_depth = 1
                self._drop_tag = tag
            return

        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth or tag in DROP_TAGS or tag in DROP_ROLE_TAGS:
            return
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        if not (_is_hidden(node) or _is_noise(node)):
            self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = None
            return
        # 対応する開始タグまで巻き戻す。無ければ無視（閉じ忘れHTML対策）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if self._drop_depth or not data.strip():
            return
        self._stack[-1].children.append(Node("#text", text=data))

    def unknown_decl(self, data):
        # RSS/Atom の CDATA は handle_data に来ない
        if self._drop_depth:
            return
        if data.startswith("CDATA["):
            text = data[len("CDATA["):]
            if text.strip():
                self._stack[-1].children.append(Node("#text", text=text))


def _is_hidden(node: Node) -> bool:
    if "hidden" in node.attrs:
        return True
    if node.attr("aria-hidden").lower() == "true":
        return True
    style = node.attr("style").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return False


def _is_noise(node: Node) -> bool:
    """広告・ナビ等のガワか。KEEP語を含むなら残す（誤爆でライブ欄を消さない）。"""
    ident = " ".join([node.attr("class"), node.attr("id"), node.attr("role")])
    if not ident.strip():
        return False
    if KEEP_PATTERNS.search(ident):
        return False
    return bool(NOISE_PATTERNS.search(ident))


def parse(html: str) -> Node:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        # 壊れたHTMLでも、そこまでに組めた木を使う
        pass
    return builder.root


def _find_all(node: Node, tags) -> List[Node]:
    found: List[Node] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.tag in tags:
            found.append(cur)
        stack.extend(reversed(cur.children))
    return found


def _text_len(node: Node) -> int:
    total = 0
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.tag == "#text":
            total += len(cur.text.strip())
        stack.extend(cur.children)
    return total


def select_main(root: Node) -> Node:
    """ライブ情報が載っていそうな領域を選ぶ。

    main/article を最優先し、無ければ id/class が live・schedule・tour 等の
    要素を探す。どれも無ければ body（＝木全体）を返す。
    **迷ったら広く取る**。取りこぼすと収集精度が落ちる側に効くため。
    """
    for tag in MAIN_TAGS:
        nodes = [n for n in _find_all(root, {tag}) if _text_len(n) >= 80]
        if nodes:
            return max(nodes, key=_text_len)

    keyword = re.compile(
        r"(live|tour|schedule|event|concert|ticket|news|contents|main)", re.I
    )
    candidates = []
    for n in _find_all(root, {"div", "section", "table", "ul", "ol", "dl"}):
        ident = " ".join([n.attr("class"), n.attr("id")])
        if ident and keyword.search(ident):
            length = _text_len(n)
            if length >= 80:
                candidates.append((length, n))
    if candidates:
        # 一番テキスト量の多い該当領域を採用
        return max(candidates, key=lambda c: c[0])[1]

    bodies = _find_all(root, {"body"})
    return bodies[0] if bodies else root


def to_blocks(node: Node, base_url: str = "") -> List[Block]:
    """木をブロック（テキスト＋リンク）の並びに落とす。

    ブロック境界は「行としての意味が残る単位」に置く。1本の長文にすると
    日付と会場の対応が壊れ、逆に細切れにすると1公演が分断されるため、
    li / tr / p / 見出し を境界にする。
    """
    blocks: List[Block] = []
    _walk_blocks(node, base_url, blocks)

    merged: List[Block] = []
    for b in blocks:
        text = clean_text(b.text)
        if not text:
            continue
        if merged and merged[-1].text == text:
            # 直前と同一テキストのブロックは重複とみなす
            merged[-1].links.extend(l for l in b.links if l not in merged[-1].links)
            continue
        merged.append(Block(text=text, links=b.links, path=b.path))
    return merged


_BLOCK_BOUNDARY = {"li", "tr", "p", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6",
                   "article", "section", "blockquote", "figcaption", "address"}


def _walk_blocks(node: Node, base_url: str, out: List[Block]) -> None:
    """境界タグごとに1ブロックを作る。境界の無い領域はまとめて1ブロック。"""
    boundary_children = [
        c for c in node.children if c.tag in _BLOCK_BOUNDARY or _has_boundary(c)
    ]
    if not boundary_children:
        text, links = _flatten(node, base_url)
        if text.strip():
            out.append(Block(text=text, links=links, path=node.tag))
        return

    for child in node.children:
        if child.tag == "#text":
            if child.text.strip():
                out.append(Block(text=child.text, path=node.tag))
        elif child.tag in _BLOCK_BOUNDARY and not _has_boundary(child):
            text, links = _flatten(child, base_url)
            if text.strip():
                out.append(Block(text=text, links=links, path=child.tag))
        else:
            _walk_blocks(child, base_url, out)


def _has_boundary(node: Node) -> bool:
    stack = list(node.children)
    while stack:
        cur = stack.pop()
        if cur.tag in _BLOCK_BOUNDARY:
            return True
        stack.extend(cur.children)
    return False


def _flatten(node: Node, base_url: str) -> Tuple[str, List[Tuple[str, str]]]:
    parts: List[str] = []
    links: List[Tuple[str, str]] = []

    def rec(n: Node) -> None:
        if n.tag == "#text":
            parts.append(n.text)
            return
        if n.tag == "br":
            parts.append("\n")
            return
        if n.tag == "img":
            alt = n.attr("alt").strip()
            if alt:
                parts.append(alt)
            return
        if n.tag == "a":
            href = n.attr("href").strip()
            label_start = len(parts)
            for c in n.children:
                rec(c)
            label = clean_text("".join(parts[label_start:]))
            url = _abs_url(href, base_url)
            if url and (label or url):
                pair = (label, url)
                if pair not in links:
                    links.append(pair)
            return
        for c in n.children:
            rec(c)
        if n.tag in BLOCK_TAGS and n.tag not in ("span", "time"):
            parts.append("\n")

    rec(node)
    return "".join(parts), links


def _abs_url(href: str, base_url: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    low = href.lower()
    if low.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return ""
    url = urljoin(base_url, href) if base_url else href
    if urlparse(url).scheme not in ("http", "https"):
        return ""
    return url


def clean_text(text: str) -> str:
    """空白・改行・全角記号を正規化する。差分ハッシュの安定性に直結する。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def normalize_html(html: str, base_url: str = "") -> Tuple[str, List[Block]]:
    """HTML → (正規化テキスト, ブロック列)。

    正規化テキストは差分ハッシュとAIへ渡す素材の両方に使う。
    """
    root = parse(html)
    main = select_main(root)
    blocks = to_blocks(main, base_url)

    seen = set()
    lines: List[str] = []
    for b in blocks:
        for line in b.text.split("\n"):
            key = line.strip()
            if not key or key in seen:
                continue
            # 同一行の重複はナビの残骸であることが多いので1回だけ残す
            seen.add(key)
            lines.append(key)

    text = _MULTI_NL.sub("\n\n", "\n".join(lines))
    return text.strip(), blocks


def hash_source(text: str) -> str:
    """差分判定用に、揮発する数字を落としたテキストを返す。"""
    return _VOLATILE_NUM.sub("", text)
