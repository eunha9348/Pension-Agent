"""청킹 — 조항 단위 우선, 표는 통째로 유지.

━━ 표를 쪼개면 안 되는 이유 ━━
세율 대조가 표에서 이뤄진다. "만 80세 이상 | 3.3%" 행에서 세율만 잘려나가면
근거로서 무의미해지고, 최악의 경우 다른 행의 세율과 섞여 오답이 된다.
그래서 표 블록은 크기와 무관하게 한 청크로 유지한다.

━━ 경계 규칙 ━━
· 조항/섹션 헤더에서 자른다: 제12조 · 【연금수령한도】 · Ⅲ. · 1) 등
· 헤더가 없으면 문단 단위로 묶되 목표 길이를 넘으면 자른다
· 표 라인이 연속되면 하나의 표 블록으로 보고 절대 자르지 않는다
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.ingest.zip_parser import ParsedDocument, Page

# 목표 청크 길이(문자). 너무 길면 근거 인용이 뭉툭해지고,
# 너무 짧으면 조항의 조건절이 잘려 오해를 만든다.
TARGET_CHARS = 700
MAX_CHARS = 1600

# 조항·섹션 헤더
_HEADER_PATTERNS = [
    re.compile(r'^\s*제\s*\d+\s*[조항관장절편](?:의\s*\d+)?'),
    re.compile(r'^\s*【[^】]{1,40}】'),
    re.compile(r'^\s*\[[^\]]{1,40}\]\s*$'),
    re.compile(r'^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*[.、]'),
    re.compile(r'^\s*제\s*\d+\s*부\s'),
    re.compile(r'^\s*\d+\s*[.)]\s*[가-힣A-Za-z]'),
    re.compile(r'^\s*[①②③④⑤⑥⑦⑧⑨⑩]'),
]

# 표로 볼 라인: 파이프 구분, 또는 탭/다중공백으로 나뉜 2열 이상
_TABLE_LINE = re.compile(r'^[^|\n]{1,60}\|')
_TABLE_LINE_SPACED = re.compile(r'^\S.{0,40}?\s{3,}\S')


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    ordinal: int
    text: str
    page_from: int
    page_to: int
    locator: Optional[str] = None
    is_table: bool = False
    entities: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id, "doc_id": self.doc_id,
            "ordinal": self.ordinal, "text": self.text,
            "page_from": self.page_from, "page_to": self.page_to,
            "locator": self.locator, "is_table": self.is_table,
            "entities": self.entities,
        }


def _is_header(line: str) -> bool:
    return any(p.match(line) for p in _HEADER_PATTERNS)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return bool(_TABLE_LINE.match(s) or _TABLE_LINE_SPACED.match(s))


def _locator_of(text: str) -> Optional[str]:
    """청크의 위치 표시(조항/섹션). 인용 각주에 붙는다."""
    for line in text.splitlines()[:3]:
        s = line.strip()
        if not s:
            continue
        m = re.match(r'(제\s*\d+\s*[조항관장절편](?:의\s*\d+)?)', s)
        if m:
            return m.group(1).replace(" ", "")
        m = re.match(r'(【[^】]{1,40}】)', s)
        if m:
            return m.group(1)
    return None


@dataclass
class _Block:
    lines: list[str] = field(default_factory=list)
    page_from: int = 0
    page_to: int = 0
    is_table: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def size(self) -> int:
        return sum(len(l) for l in self.lines)


def _blocks_from_pages(pages: list[Page]) -> list[_Block]:
    """페이지들을 헤더/표 경계로 1차 분할."""
    blocks: list[_Block] = []
    cur = _Block()

    def flush():
        nonlocal cur
        if cur.text:
            blocks.append(cur)
        cur = _Block()

    for page in pages:
        for raw in page.text.splitlines():
            line = raw.rstrip()
            table_line = _is_table_line(line)

            # 표 시작/종료 시 블록을 가른다 — 표는 별도 블록으로 유지
            if table_line != cur.is_table and cur.text:
                flush()
                cur.is_table = table_line
            elif not cur.lines:
                cur.is_table = table_line

            if _is_header(line) and cur.text and not table_line:
                flush()
                cur.is_table = False

            if not cur.lines:
                cur.page_from = page.page_no
            cur.page_to = page.page_no
            cur.lines.append(line)

    flush()
    return [b for b in blocks if b.text]


def chunk_document(doc: ParsedDocument,
                   entities: Optional[dict] = None) -> list[Chunk]:
    """문서 → 청크 목록."""
    entities = entities or {}
    blocks = _blocks_from_pages(doc.pages)

    chunks: list[Chunk] = []
    buf: list[_Block] = []

    def emit():
        if not buf:
            return
        text = "\n".join(b.text for b in buf).strip()
        if not text:
            buf.clear()
            return
        ordinal = len(chunks) + 1
        chunks.append(Chunk(
            chunk_id=f"{doc.doc_id}#c{ordinal:03d}",
            doc_id=doc.doc_id,
            ordinal=ordinal,
            text=text,
            page_from=min(b.page_from for b in buf),
            page_to=max(b.page_to for b in buf),
            locator=_locator_of(text),
            is_table=any(b.is_table for b in buf),
            entities=dict(entities),
        ))
        buf.clear()

    for b in blocks:
        if b.is_table:
            # 표는 앞 문맥(헤더)과 함께 두되, 크기와 무관하게 쪼개지 않는다
            if sum(x.size for x in buf) > TARGET_CHARS:
                emit()
            buf.append(b)
            emit()
            continue

        if sum(x.size for x in buf) + b.size > MAX_CHARS and buf:
            emit()
        buf.append(b)
        if sum(x.size for x in buf) >= TARGET_CHARS:
            emit()

    emit()
    return chunks
