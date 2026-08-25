"""코퍼스 로더 — 파일 형식별 판독기 디스패치.

━━ 왜 필요한가 ━━
`zip_parser`는 "페이지별 JPEG + OCR 텍스트 zip"만 다룬다. 그런데 실제로
받게 되는 자료 묶음에는 다른 형식이 섞여 있을 수 있고, 그때 조용히
건너뛰면 **인덱스에 문서가 없는 채로 서비스가 뜬다.** 그게 최악이다.

그래서 이 파일은 두 가지를 한다:
  1. 다룰 수 있는 형식은 최대한 다룬다 (표준 라이브러리 우선)
  2. 다룰 수 없는 파일은 **조용히 넘기지 않고 목록으로 보고한다**

━━ 형식별 지원 ━━
  .zip                    ✅ zip_parser (JPEG+OCR 구조)
  .txt .md .text          ✅ 그대로
  .csv .tsv               ✅ 표 형태를 유지하며 텍스트화
  .json                   ✅ 텍스트 필드 회수
  .html .htm              ✅ 태그 제거 (표준 라이브러리 파서)
  .xlsx                   ✅ openpyxl 있으면 시트별 표로 (없으면 보고)
  .pdf                    ⚠️ pypdf 있으면 페이지별로 (없으면 보고)
                             ※ 스캔 PDF는 텍스트가 없어 OCR 결과가 필요하다
  .hwp .hwpx .doc .docx   ❌ 변환 후 넣을 것 (아래 안내 참조)
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from app.ingest.zip_parser import Page, ParsedDocument, parse_zip

TEXTUAL = {".txt", ".text", ".md"}
TABULAR = {".csv", ".tsv"}
MARKUP = {".html", ".htm", ".xhtml"}
JSONISH = {".json", ".jsonl"}

# 변환이 필요한 형식 → 안내 문구
NEEDS_CONVERSION = {
    ".hwp": "한글 파일 — PDF 또는 텍스트로 변환 후 넣어 주세요",
    ".hwpx": "한글 파일 — PDF 또는 텍스트로 변환 후 넣어 주세요",
    ".doc": "워드 문서 — PDF 또는 텍스트로 변환 후 넣어 주세요",
    ".docx": "워드 문서 — PDF 또는 텍스트로 변환 후 넣어 주세요",
    ".ppt": "슬라이드 — PDF로 변환 후 넣어 주세요",
    ".pptx": "슬라이드 — PDF로 변환 후 넣어 주세요",
    ".jpg": "낱장 이미지 — OCR 텍스트가 없으면 판독할 수 없습니다",
    ".jpeg": "낱장 이미지 — OCR 텍스트가 없으면 판독할 수 없습니다",
    ".png": "낱장 이미지 — OCR 텍스트가 없으면 판독할 수 없습니다",
}


class _TextExtractor(HTMLParser):
    """태그를 제거하고 본문만 남긴다. script/style은 버린다."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        out = " ".join(self.parts)
        out = re.sub(r'[ \t]+', ' ', out)
        return re.sub(r'\n\s*\n+', '\n', out).strip()


# 같은 글자가 2회 연속인 쌍이 4번 이상 이어지는 구간.
# PDF 텍스트 레이어가 이중으로 그려졌을 때 나타난다 —
#   원문 "…× 120 ÷ (11 − 연금수령연차)…"
#   추출 "…× 112200 ((1111 -- 연연금금수수령령연연차차))…"
# 4쌍 이상을 요구하므로 "1100원", "가입자가" 같은 정상 표현은 걸리지 않는다
# (mock 코퍼스 26개 파일 전수 확인 결과 오탐 0건).
_DOUBLE_CHAR = r'[가-힣0-9A-Za-z(),.\-−×÷%]'
# ① 오염 판정용 — 4쌍 이상 연속. 정상 문서에서는 사실상 나오지 않는다.
_DOUBLED_STRONG = re.compile(rf'(?:({_DOUBLE_CHAR})\1){{4,}}')
# ② 복구용 — 2쌍 이상. ①로 오염이 확인된 텍스트 안에서만 쓴다.
_DOUBLED_WEAK = re.compile(rf'(?:({_DOUBLE_CHAR})\1){{2,}}')


def looks_doubled(text: str) -> bool:
    """이중 렌더링으로 깨진 텍스트인가 (고신뢰 판정)."""
    return bool(text) and _DOUBLED_STRONG.search(text) is not None


def repair_doubled_glyphs(text: str) -> str:
    """이중으로 그려진 PDF 텍스트에서 중복 글자를 걷어낸다.

    ━━ 왜 2단계인가 ━━
    전역으로 중복을 지우면 "1100원", "가입"처럼 정상 표기가 망가진다.
    그렇다고 임계값을 높게(4쌍) 잡으면 "112200"(=120), "평가가액액"(=평가액)
    처럼 **짧게 끊긴 구간**을 놓친다. 실제 깨짐은 이 둘이 섞여 나온다:
        원문 "평가액 × 120 ÷ (11 − 연금수령연차)"
        추출 "평가가액액 × 112200 ((1111 -- 연연금금수수령령연연차차))"

    그래서 ① 4쌍 이상이라는 **높은 문턱으로 오염된 텍스트를 먼저 특정**하고,
    ② 그 안에서만 2쌍 이상을 복구한다. 정상 텍스트는 ①에서 걸러져
    아예 손대지 않으므로 오탐이 번지지 않는다.
    """
    if not looks_doubled(text):
        return text
    return _DOUBLED_WEAK.sub(lambda m: m.group(0)[::2], text)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _paginate(text: str, doc: ParsedDocument, chars_per_page: int = 3000) -> None:
    """페이지 개념이 없는 형식은 일정 길이로 나눠 페이지 번호를 붙인다.

    (청킹은 조항 단위로 다시 하므로 여기서는 대략적으로만 나눈다)
    """
    text = text.strip()
    if not text:
        return
    for i in range(0, len(text), chars_per_page):
        doc.pages.append(Page(len(doc.pages) + 1, text[i:i + chars_per_page]))


# ── 형식별 판독 ──────────────────────────────────────────────

def _load_text(path: Path, doc: ParsedDocument) -> None:
    _paginate(_decode(path.read_bytes()), doc)


def _load_tabular(path: Path, doc: ParsedDocument) -> None:
    """표는 파이프 구분으로 바꿔 청커가 '표'로 인식하게 한다."""
    raw = _decode(path.read_bytes())
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    lines = [" | ".join(row) for row in csv.reader(io.StringIO(raw), delimiter=delim)]
    _paginate("\n".join(lines), doc)


def _load_json(path: Path, doc: ParsedDocument) -> None:
    raw = _decode(path.read_bytes())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # jsonl 가능성
        rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
        data = rows if rows else None
    if data is None:
        doc.warnings.append("JSON 파싱 실패")
        return

    collected: list[str] = []

    def walk(node):
        if isinstance(node, str):
            if len(node.strip()) > 1:
                collected.append(node.strip())
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    _paginate("\n".join(collected), doc)


def _load_html(path: Path, doc: ParsedDocument) -> None:
    parser = _TextExtractor()
    parser.feed(_decode(path.read_bytes()))
    _paginate(parser.text(), doc)


def _safe_import(module: str, attr: str = ""):
    """선택적 의존성을 안전하게 불러온다. 실패하면 (None, 사유).

    ⚠️ `except Exception`으로는 부족하다. 네이티브 확장(pyo3/cffi 등)이
       깨져 있으면 `BaseException`을 상속한 PanicException이 올라와
       인제스트 전체가 죽는다. 실제로 이 컨테이너의 pypdf가 그랬다.
       판독기 하나가 고장 났다고 코퍼스 전체를 못 읽으면 안 된다.
    """
    try:
        mod = __import__(module, fromlist=[attr] if attr else [])
        return (getattr(mod, attr) if attr else mod), ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:      # noqa: BLE001 — 위 주석 참조
        return None, f"{type(e).__name__}: {e}"


def _load_xlsx(path: Path, doc: ParsedDocument) -> None:
    openpyxl, err = _safe_import("openpyxl")
    if openpyxl is None:
        doc.warnings.append(f"xlsx 판독기를 불러오지 못했습니다({err}) — "
                            f"pip install openpyxl")
        return
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for sheet in wb.worksheets:
        lines = [f"【{sheet.title}】"]
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
        doc.pages.append(Page(len(doc.pages) + 1, "\n".join(lines)))
    wb.close()


def _load_pdf(path: Path, doc: ParsedDocument) -> None:
    """텍스트 레이어가 있는 PDF만 판독된다.

    ⚠️ 스캔본 PDF는 텍스트 레이어가 없어 빈 페이지만 나온다. 그 경우
       OCR 텍스트가 따로 있어야 하며, 없으면 이 파일은 쓸 수 없다.
       (이 상황은 경고로 남기고 조용히 넘어가지 않는다)
    """
    PdfReader, err = _safe_import("pypdf", "PdfReader")
    if PdfReader is None:
        doc.warnings.append(f"PDF 판독기를 불러오지 못했습니다({err}) — "
                            f"pip install pypdf 후 재시도하십시오")
        return
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        doc.warnings.append(f"PDF 열기 실패: {e}")
        return
    empty = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            empty += 1
        doc.pages.append(Page(i, text))
    if empty and empty == len(doc.pages):
        doc.warnings.append(
            f"PDF {empty}쪽 전부에서 텍스트를 얻지 못했습니다 — "
            f"스캔본으로 보입니다. OCR 텍스트 파일이 별도로 필요합니다")


_LOADERS = [
    (TEXTUAL, _load_text),
    (TABULAR, _load_tabular),
    (JSONISH, _load_json),
    (MARKUP, _load_html),
    ({".xlsx", ".xlsm"}, _load_xlsx),
    ({".pdf"}, _load_pdf),
]


# ── 진입점 ───────────────────────────────────────────────────

def load_file(path: Path) -> ParsedDocument:
    """파일 1건 → ParsedDocument. 지원하지 않으면 warnings에 사유를 남긴다."""
    suffix = path.suffix.lower()

    if suffix == ".zip":
        return parse_zip(path)

    doc = ParsedDocument(doc_id=path.stem, source_path=str(path),
                         layout=f"단일파일({suffix or '확장자 없음'})")

    if suffix in NEEDS_CONVERSION:
        doc.warnings.append(f"지원하지 않는 형식: {NEEDS_CONVERSION[suffix]}")
        return doc

    for suffixes, loader in _LOADERS:
        if suffix in suffixes:
            try:
                loader(path, doc)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:   # 한 파일 때문에 전체가 죽으면 안 된다
                doc.warnings.append(f"판독 중 오류: {type(e).__name__}: {e}")
            if not doc.pages and not doc.warnings:
                doc.warnings.append("텍스트를 얻지 못했습니다")
            return doc

    doc.warnings.append(f"알 수 없는 형식 '{suffix}' — 판독기가 없습니다")
    return doc


def is_ingestible(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in NEEDS_CONVERSION:
        return False
    if suffix == ".zip":
        return zipfile.is_zipfile(path)
    return any(suffix in suffixes for suffixes, _ in _LOADERS)


def corpus_files(corpus_dir: str | Path) -> list[Path]:
    """코퍼스 디렉터리의 모든 파일 (하위 디렉터리 포함, 숨김 파일 제외)."""
    d = Path(corpus_dir)
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*")
                  if p.is_file() and not p.name.startswith("."))


def iter_documents(corpus_dir: str | Path) -> Iterator[ParsedDocument]:
    """코퍼스의 모든 파일을 문서로 변환. 판독 실패도 warnings를 달고 넘어온다."""
    for path in corpus_files(corpus_dir):
        yield load_file(path)
