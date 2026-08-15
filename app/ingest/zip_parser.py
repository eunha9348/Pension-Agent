"""제공 문서 zip 파서.

━━ 전제 (중요) ━━
제공 문서는 **PDF가 아니다.** 페이지별 JPEG 이미지와 그 OCR 텍스트가 담긴
zip이다. PyPDF·pdfplumber 같은 라이브러리로 열려고 하면 안 된다.
→ 표준 `zipfile`로 구조를 먼저 확인하고, 텍스트 엔트리만 읽는다.
   이미지 엔트리는 **읽지 않는다**(OCR을 우리가 다시 돌리지 않는다).

━━ 실제 zip을 아직 못 받았다 ━━
따라서 폴더 레이아웃을 하나로 단정하지 않고, 흔한 형태를 전부 흡수한다:

  A) doc39/texts/page_001.txt + doc39/images/page_001.jpg
  B) page_001.txt + page_001.jpg          (평면 구조)
  C) ocr.json  또는 doc39.json            ({"pages":[{"page":1,"text":...}]})
  D) 페이지 구분 없는 단일 텍스트 파일

실제 zip을 받으면 `python -m app.ingest.inspect_zip <파일>` 로 구조를 먼저
덤프해서, 아래 감지 규칙에 걸리는지 확인할 것. 안 걸리면 규칙을 추가하면 된다
(파서 나머지는 그대로 쓸 수 있다).
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

TEXT_SUFFIXES = {".txt", ".text"}
JSON_SUFFIXES = {".json"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

# 파일명에서 페이지 번호 회수: page_001.txt / 0012.txt / p-3.txt / 39_page12.txt
_PAGE_NUM = re.compile(r'(?:page|p|pg|쪽|면)?[_\-]?(\d{1,4})\D*$', re.I)


@dataclass
class Page:
    page_no: int
    text: str
    source_entry: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass
class ParsedDocument:
    doc_id: str
    pages: list[Page] = field(default_factory=list)
    image_entries: list[str] = field(default_factory=list)
    source_path: str = ""
    layout: str = ""                       # 감지된 레이아웃 종류
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


# ════════════════════════════════════════════════════════════════
# 구조 확인 — 파싱 전에 반드시 이걸 먼저 본다
# ════════════════════════════════════════════════════════════════

def inspect(zip_path: str | Path) -> dict:
    """zip 내부 구조를 요약한다 (파싱하지 않고 목록만).

    실제 문서를 처음 받았을 때 가장 먼저 실행할 것.
    """
    p = Path(zip_path)
    if not zipfile.is_zipfile(p):
        return {"path": str(p), "error": "zip 파일이 아닙니다 — "
                                         "PDF 라이브러리로 열지 말고 형식을 먼저 확인하십시오"}

    with zipfile.ZipFile(p) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]

    by_suffix: dict[str, int] = {}
    for i in infos:
        by_suffix[Path(i.filename).suffix.lower() or "(없음)"] = \
            by_suffix.get(Path(i.filename).suffix.lower() or "(없음)", 0) + 1

    dirs = sorted({str(Path(i.filename).parent) for i in infos})
    return {
        "path": str(p),
        "entries": len(infos),
        "by_suffix": by_suffix,
        "directories": dirs[:20],
        "sample_names": [i.filename for i in infos[:12]],
        "total_bytes": sum(i.file_size for i in infos),
        "detected_layout": _detect_layout(infos),
    }


def _detect_layout(infos: list[zipfile.ZipInfo]) -> str:
    names = [i.filename for i in infos]
    suffixes = {Path(n).suffix.lower() for n in names}
    if suffixes & JSON_SUFFIXES:
        return "C(JSON 통합)"
    text_names = [n for n in names if Path(n).suffix.lower() in TEXT_SUFFIXES]
    if not text_names:
        return "미상(텍스트 엔트리 없음 — OCR 텍스트가 정말 없는지 확인 필요)"
    if len(text_names) == 1:
        return "D(단일 텍스트)"
    if any("/" in n for n in text_names):
        return "A(디렉터리 분리)"
    return "B(평면)"


# ════════════════════════════════════════════════════════════════
# 파싱
# ════════════════════════════════════════════════════════════════

def _page_no_from_name(name: str, fallback: int) -> int:
    stem = Path(name).stem
    m = _PAGE_NUM.search(stem)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return fallback


def _decode(raw: bytes) -> str:
    """OCR 텍스트 디코딩. UTF-8 실패 시 CP949(한국어 윈도우) 순으로 시도."""
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # 마지막 수단 — 깨진 문자는 버리되, 조용히 넘기지 않고 표시한다
    return raw.decode("utf-8", errors="replace")


def parse_zip(zip_path: str | Path, doc_id: Optional[str] = None) -> ParsedDocument:
    """zip 1개 → 문서 1건.

    이미지 엔트리는 목록만 세고 내용을 읽지 않는다.
    """
    p = Path(zip_path)
    did = doc_id or p.stem
    doc = ParsedDocument(doc_id=did, source_path=str(p))

    if not zipfile.is_zipfile(p):
        doc.warnings.append("zip 파일이 아님 — 건너뜀")
        return doc

    with zipfile.ZipFile(p) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        doc.layout = _detect_layout(infos)

        json_entries, text_entries = [], []
        for i in infos:
            suf = Path(i.filename).suffix.lower()
            if suf in IMAGE_SUFFIXES:
                doc.image_entries.append(i.filename)
            elif suf in JSON_SUFFIXES:
                json_entries.append(i)
            elif suf in TEXT_SUFFIXES:
                text_entries.append(i)

        if json_entries:
            for i in json_entries:
                doc.pages.extend(_pages_from_json(_decode(zf.read(i)), i.filename, doc))
        if not doc.pages and text_entries:
            text_entries.sort(key=lambda i: (_page_no_from_name(i.filename, 0),
                                             i.filename))
            for idx, i in enumerate(text_entries, 1):
                doc.pages.append(Page(
                    page_no=_page_no_from_name(i.filename, idx),
                    text=_decode(zf.read(i)),
                    source_entry=i.filename,
                ))

    if not doc.pages:
        doc.warnings.append(
            f"텍스트를 추출하지 못함 (이미지 {len(doc.image_entries)}건). "
            "OCR 텍스트가 zip에 포함돼 있는지 inspect_zip으로 확인할 것")
    empty = sum(1 for pg in doc.pages if pg.is_empty)
    if empty:
        doc.warnings.append(f"빈 페이지 {empty}건 — OCR 실패 페이지일 수 있음")

    doc.pages.sort(key=lambda pg: pg.page_no)
    return doc


def _pages_from_json(raw: str, entry: str, doc: ParsedDocument) -> list[Page]:
    """레이아웃 C — JSON 하나에 페이지가 모여 있는 형태."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        doc.warnings.append(f"{entry} JSON 파싱 실패: {e}")
        return []

    if isinstance(data, dict):
        seq = data.get("pages") or data.get("page_list") or data.get("data") or []
    elif isinstance(data, list):
        seq = data
    else:
        return []

    pages: list[Page] = []
    for idx, item in enumerate(seq, 1):
        if isinstance(item, str):
            pages.append(Page(idx, item, entry))
        elif isinstance(item, dict):
            text = (item.get("text") or item.get("ocr") or item.get("content")
                    or item.get("ocr_text") or "")
            no = item.get("page") or item.get("page_no") or item.get("index") or idx
            try:
                no = int(no)
            except (TypeError, ValueError):
                no = idx
            pages.append(Page(no, str(text), entry))
    return pages


def iter_corpus(corpus_dir: str | Path) -> Iterator[ParsedDocument]:
    """디렉터리 안의 모든 zip을 파싱."""
    d = Path(corpus_dir)
    if not d.exists():
        return
    for p in sorted(d.glob("*.zip")):
        yield parse_zip(p)
