"""문서 메타데이터 생성 — 인용 정확도와 직결된다.

생성 항목
  type     : 투자설명서 | 제도안내 | 세제안내 | 약관   (citation_system 추론 재사용)
  title    : 문서 첫머리에서 추정
  entities : product_name / product_code / fund_class / plan_type / tax_year
  sections : [{"locator": "제12조", "chunk_id": ...}]
  legacy   : detect_legacy_tax_content() 결과

━━ 왜 인제스트 시점에 만드는가 ━━
citation_system.build_citations()는 doc_meta가 있으면 추론보다 우선 적용한다.
검색 때마다 본문으로 유형을 추론하면 같은 문서가 질의마다 다르게 분류될 수 있다.
"""

from __future__ import annotations

import re
from typing import Optional

from app.analysis.product_facts import extract_product_facts
from app.core.citation_system import infer_doc_type
from app.core.pension_calc_functions import (CLASS_ACCOUNT_REQUIREMENT,
                                             RESTRICTED_CLASSES,
                                             detect_legacy_tax_content)
from app.ingest.chunker import Chunk
from app.ingest.zip_parser import ParsedDocument

_KNOWN_CLASSES = sorted(set(CLASS_ACCOUNT_REQUIREMENT) | set(RESTRICTED_CLASSES),
                        key=len, reverse=True)

_PRODUCT_NAME = re.compile(
    r'([가-힣A-Za-z0-9]*(?:증권자?투자신탁|투자신탁|펀드|자산배분|타겟데이트)'
    r'[가-힣A-Za-z0-9\[\]()]*)')
_PRODUCT_CODE = re.compile(r'\b(KR[0-9A-Z]{10,12}|R2_KR\w+)\b')
_TAX_YEAR = re.compile(r'((?:19|20)\d{2})\s*년\s*(?:1월\s*1일\s*)?이후')

_PLAN_TYPES = ["연금저축", "IRP", "개인형퇴직연금", "DC", "DB", "퇴직연금"]


def extract_entities(text: str, doc_id: str = "") -> dict[str, str]:
    """문서 전체 텍스트에서 엔티티 추출.

    ⚠️ 여기서 뽑은 엔티티는 근거 필터링(entity conflict)에 쓰인다.
       과하게 잡으면 정상 근거가 걸러지므로, 확신이 서는 패턴만 쓴다.
    """
    ent: dict[str, str] = {}

    if m := _PRODUCT_NAME.search(text):
        ent["product_name"] = m.group(1).strip()

    codes = _PRODUCT_CODE.findall(text) or _PRODUCT_CODE.findall(doc_id)
    if codes:
        ent["product_code"] = codes[0]

    # ⚠️ \b 를 쓰면 안 된다. "C-P는"에서 P 다음이 한글이라 파이썬 정규식은
    #    단어 경계로 보지 않는다. 라틴문자/숫자/하이픈만 경계로 취급한다.
    classes = [c for c in _KNOWN_CLASSES
               if re.search(rf'(?<![A-Za-z0-9-]){re.escape(c)}(?![A-Za-z0-9-])', text)]
    if classes:
        # 여러 클래스가 나오는 건 투자설명서에서 정상 — 목록으로 남긴다
        ent["fund_class"] = ",".join(sorted(set(classes)))

    plans = [p for p in _PLAN_TYPES if p in text]
    if len(plans) == 1:
        # 여러 제도를 함께 설명하는 문서는 plan_type을 비운다
        # (하나로 못 박으면 엔티티 충돌로 정상 근거가 걸러진다)
        ent["plan_type"] = plans[0]

    if m := _TAX_YEAR.search(text):
        ent["tax_year"] = m.group(1)

    return ent


def guess_title(doc: ParsedDocument) -> str:
    for page in doc.pages[:2]:
        for line in page.text.splitlines():
            s = line.strip()
            if 4 <= len(s) <= 60 and not s.startswith(("①", "·", "-", "|")):
                return s
    return doc.doc_id


def build_doc_metadata(doc: ParsedDocument, chunks: list[Chunk]) -> dict:
    """문서 1건의 메타데이터."""
    full = doc.full_text
    entities = extract_entities(full, doc.doc_id)
    legacy = detect_legacy_tax_content(full, doc.doc_id)

    # ── 상품 팩트 전수 파싱 ──────────────────────────────────
    #
    # ⚠️ 여기가 "딥크롤링"의 자리다. 검색된 청크가 아니라 **문서 전문**을
    #    훑으므로, 검색이 표 청크를 못 건져도 위험등급·상품분류·수익률·
    #    시장잔고가 사라지지 않는다. products.py::extract_class_expenses는
    #    근거 청크만 보기 때문에 검색 순위에 사실의 존재 여부가 걸려 있었다.
    #
    #    투자설명서가 아닌 문서(제도안내·약관)에서는 빈 값이 정상이므로,
    #    못 찾았다고 해서 오류가 아니다.
    facts = extract_product_facts(full, doc.doc_id)

    return {
        "doc_id": doc.doc_id,
        "type": infer_doc_type(doc.doc_id, full),
        "title": guess_title(doc),
        "entities": entities,
        "product_facts": facts.as_dict(),
        "sections": [{"locator": c.locator, "chunk_id": c.chunk_id}
                     for c in chunks if c.locator],
        "legacy": legacy,
        "page_count": doc.page_count,
        "chunk_count": len(chunks),
        "source_path": doc.source_path,
        "layout": doc.layout,
        "warnings": doc.warnings,
    }


def apply_chunk_metadata(chunks: list[Chunk], doc_meta: dict) -> list[Chunk]:
    """문서 엔티티를 청크에 내리고, 청크 단위 구법 판정을 덧붙인다.

    구법 판정은 문서 단위로만 하면 과잉 차단이 된다 — 구버전 투자설명서라도
    세제 외 내용은 멀쩡하기 때문. 그래서 청크 단위로도 따로 본다.
    """
    doc_entities = doc_meta.get("entities", {})
    for c in chunks:
        merged = {**doc_entities, **(c.entities or {})}
        verdict = detect_legacy_tax_content(c.text, c.doc_id)
        if verdict["is_legacy_suspect"]:
            merged["legacy"] = "true"
        c.entities = merged
    return chunks
