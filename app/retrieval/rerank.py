"""L3 후처리 — 중복 제거 · 문서 다양성 · 저정보 청크 강등.

━━ 왜 필요한가 ━━
BM25 순위를 그대로 근거로 쓰면 두 가지 사고가 난다. 둘 다 실배포에서
실제로 발생했다.

1) **같은 문장이 근거를 독점한다.**
   투자설명서 158건에는 세액공제 조항이 글자까지 동일하게 반복된다.
   BM25는 이 중복을 인지하지 못하므로 상위 8건이 전부 같은 문장이 된다.
   근거 예산 8칸을 사실 하나가 다 먹고, 정작 필요한 문서가 밀려난다.
   (Q-001 명퇴 질의에서 근거 8건 중 6건이 동일 보일러플레이트였다.)

2) **연혁·목차가 본문을 밀어낸다.**
   "미래에셋솔로몬중장기국공채증권투자신탁1호" 같은 펀드명이 연혁 목록에
   수십 번 반복되면, 그 청크의 어휘 밀도가 정작 투자대상을 설명하는
   청크보다 높아진다. 그 결과 연혁만 근거로 올라오고, LLM은 근거에 답이
   없으니 그럴듯한 수치를 지어낸다.
   (Q-002 솔로몬 질의에서 근거 2건이 전부 연혁이었고, 답변의 만기 구간은
    근거 어디에도 없는 수치였다.)

━━ 설계 원칙 ━━
· **강등이지 제거가 아니다.** 연혁 청크를 지우면 "이 클래스 언제
  신설됐나요" 같은 질의에 답할 수 없다. 점수만 낮춰 경쟁에서 지게 한다.
· **중복은 제거한다.** 같은 문장이 두 번 들어가서 좋을 일은 없다.
· **다양성 제한은 후보가 부족하면 푼다.** 근거를 못 채우느니 같은 문서를
  더 쓰는 편이 낫다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.ingest.store import ChunkRecord
from app.retrieval.tokenize import content_terms

# 근중복 판정 임계값 (토큰집합 Jaccard). 0.85면 문장 몇 개가 다른 정도는
# 서로 다른 근거로 인정하고, 사실상 같은 조항만 걸러낸다.
DUP_THRESHOLD = 0.85

# 한 문서에서 채택할 최대 청크 수. 후보가 모자라면 자동으로 완화된다.
MAX_PER_DOC = 2

# 저정보 청크에 곱할 점수 계수. 0이 아니라 0.35인 이유는 위 설계 원칙 참고.
LOW_INFO_FACTOR = 0.35

# "2024.02.22 종류A-e 신설" 처럼 날짜로 시작하는 줄
_DATE_LEAD = re.compile(r'^\s*(?:19|20)\d{2}\s*[.\-/년]\s*\d{1,2}')
# 본문 안에 흩어진 날짜 토큰
_DATE_ANY = re.compile(r'(?:19|20)\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}')

# 연혁으로 판정할 날짜 줄 비율
_CHRONO_LINE_RATIO = 0.45
# 한 줄로 뭉쳐 들어온 연혁을 잡기 위한 날짜 개수 하한
_CHRONO_DATE_COUNT = 6


@dataclass
class RerankReport:
    """무엇을 왜 걸렀는지 — think_trace에 그대로 남긴다.

    조용히 거르면 나중에 "왜 이 문서가 근거에 없지"를 추적할 수 없다.
    """
    duplicates_removed: int = 0
    doc_capped: int = 0
    low_info_demoted: int = 0
    relaxed: bool = False

    def as_trace(self) -> str:
        parts: list[str] = []
        if self.duplicates_removed:
            parts.append(f"중복 {self.duplicates_removed}건 제거")
        if self.low_info_demoted:
            parts.append(f"연혁·목차형 {self.low_info_demoted}건 강등")
        if self.doc_capped:
            parts.append(f"문서 편중 {self.doc_capped}건 제외")
        if self.relaxed:
            parts.append("후보 부족으로 다양성 제한 완화")
        return " · ".join(parts)


def is_low_information(text: str) -> bool:
    """연혁·목차처럼 사실 서술이 거의 없는 청크인가.

    판정 근거를 둘 다 본다:
      · 줄 단위 — 날짜로 시작하는 줄이 절반 가까이면 연혁 목록
      · 뭉치 단위 — 줄바꿈이 뭉개진 채 들어와도 날짜가 6개 이상이면 연혁
    """
    if not text:
        return False

    lines = [ln for ln in text.splitlines() if len(ln.strip()) >= 4]
    if len(lines) >= 4:
        dated = sum(1 for ln in lines if _DATE_LEAD.match(ln))
        if dated / len(lines) >= _CHRONO_LINE_RATIO:
            return True

    return len(_DATE_ANY.findall(text)) >= _CHRONO_DATE_COUNT


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def rerank(candidates: Iterable[tuple[ChunkRecord, float]],
           top_k: int,
           *,
           max_per_doc: int = MAX_PER_DOC,
           dup_threshold: float = DUP_THRESHOLD,
           pinned: Optional[set[str]] = None
           ) -> tuple[list[tuple[ChunkRecord, float]], RerankReport]:
    """후보를 재정렬해 상위 top_k를 고른다.

    pinned : 문서 다양성 제한을 면제할 청크 ID(함정 규칙이 지목한 근거 등).
             중복 제거는 pinned에도 적용한다 — 같은 문장을 두 번 넣을
             이유는 어디에도 없기 때문이다.

    반환: (선택된 [(청크, 점수)], 무엇을 걸렀는지 보고)
    """
    pinned = pinned or set()
    report = RerankReport()

    # ── 1. 저정보 청크 점수 강등 ─────────────────────────────
    scored: list[tuple[ChunkRecord, float]] = []
    for rec, score in candidates:
        if is_low_information(rec.text):
            report.low_info_demoted += 1
            score *= LOW_INFO_FACTOR
        scored.append((rec, score))

    scored.sort(key=lambda rs: -rs[1])

    # ── 2. 중복 제거 + 문서 다양성 (탐욕적 선택) ─────────────
    terms_cache: dict[str, set[str]] = {}

    def terms_of(rec: ChunkRecord) -> set[str]:
        if rec.chunk_id not in terms_cache:
            terms_cache[rec.chunk_id] = content_terms(rec.text)
        return terms_cache[rec.chunk_id]

    selected: list[tuple[ChunkRecord, float]] = []
    per_doc: dict[str, int] = {}
    # 다양성 제한 때문에 밀린 후보 — 자리가 남으면 되살린다
    deferred: list[tuple[ChunkRecord, float]] = []

    for rec, score in scored:
        if len(selected) >= top_k:
            break

        # 이미 뽑힌 것과 사실상 같은 문장인가
        my_terms = terms_of(rec)
        if any(jaccard(my_terms, terms_of(sel)) >= dup_threshold
               for sel, _ in selected):
            report.duplicates_removed += 1
            continue

        if (rec.chunk_id not in pinned
                and per_doc.get(rec.doc_id, 0) >= max_per_doc):
            deferred.append((rec, score))
            continue

        selected.append((rec, score))
        per_doc[rec.doc_id] = per_doc.get(rec.doc_id, 0) + 1

    # ── 3. 자리가 남으면 다양성 제한을 완화한다 ──────────────
    # 근거를 못 채우는 것보다 같은 문서를 한 번 더 쓰는 편이 낫다.
    if len(selected) < top_k and deferred:
        report.relaxed = True
        for rec, score in deferred:
            if len(selected) >= top_k:
                break
            my_terms = terms_of(rec)
            if any(jaccard(my_terms, terms_of(sel)) >= dup_threshold
                   for sel, _ in selected):
                report.duplicates_removed += 1
                continue
            selected.append((rec, score))

    selected_ids = {rec.chunk_id for rec, _ in selected}
    report.doc_capped = sum(1 for rec, _ in deferred
                            if rec.chunk_id not in selected_ids)

    selected.sort(key=lambda rs: -rs[1])
    return selected, report
