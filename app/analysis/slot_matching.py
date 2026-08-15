"""슬롯 매칭 — LLM 없이 키워드/엔티티 기반.

두 함수를 제공한다. 둘 다 `coverage_pipeline.build_answer()`가 주입받는다.

  slot_evidence_matcher(slot, chunk) -> bool
      이 근거 청크가 이 요구사항 슬롯을 뒷받침하는가

  answer_covers_slot(answer, slot) -> bool
      생성된 답변이 이 슬롯을 실제로 다뤘는가
      (근거는 있었는데 생성 과정에서 누락되는 경우를 잡는 이중 체크)

━━ 왜 LLM을 안 쓰는가 ━━
① 대회 제약상 HyperCLOVA X 외 모델을 못 쓰고, 이 판정에 HCX를 쓰면
   호출이 슬롯 수만큼 늘어난다(비용·지연).
② 이건 "판단은 코드, 문장은 LLM" 원칙의 판단 쪽이다.

━━ 오탐 방지 ━━
부분 문자열 비교를 쓰지 않는다. 전부 토큰 경계 기준이다
("정해지는"의 "해지"가 걸리는 종류의 오탐 차단).
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from app.analysis.vocab import GENERIC_TERMS, domain_hits, key_terms
from app.core.coverage_pipeline import (ENTITY_KEYS, EvidenceChunk,
                                        RequirementSlot, SlotStatus)

# 슬롯 설명이 짧을 때(핵심어 1~2개) 요구할 최소 겹침 수
_MIN_OVERLAP = 1
# 핵심어가 많을 때 요구할 겹침 비율
_OVERLAP_RATIO = 0.4


def _entity_conflict(query_entities: dict, chunk_entities: dict,
                     is_comparison: bool = False) -> bool:
    """같은 키에 다른 값 → 스코프 불일치.

    비교 질의는 대상이 여럿인 게 정상이므로 예외.
    (coverage_pipeline._entity_conflict와 같은 규칙 — 매칭 단계에서도
     동일하게 적용해야 '비슷하지만 다른 상품'의 근거가 슬롯에 붙지 않는다.)
    """
    if is_comparison:
        return False
    for key in ENTITY_KEYS:
        qv, cv = query_entities.get(key), chunk_entities.get(key)
        if qv and cv and str(qv) != str(cv):
            return True
    return False


def _overlap_ok(slot_terms: set[str], target_terms: set[str]) -> tuple[bool, set[str]]:
    """핵심어 겹침 판정. 반환: (충족 여부, 겹친 용어)"""
    if not slot_terms:
        return False, set()
    hit = slot_terms & target_terms
    if not hit:
        return False, hit

    # 도메인 핵심어가 하나도 안 겹치면 주제가 같다고 보지 않는다
    if not domain_hits(hit):
        return False, hit

    need = max(_MIN_OVERLAP, round(len(slot_terms) * _OVERLAP_RATIO))
    return len(hit) >= min(need, len(slot_terms)), hit


def make_slot_evidence_matcher(
    query_spec: Optional[dict] = None,
    trace_sink: Optional[list] = None,
) -> Callable[[RequirementSlot, EvidenceChunk], bool]:
    """query_spec의 엔티티 정보를 물려 슬롯-근거 매처를 만든다.

    coverage_pipeline이 기대하는 시그니처는 (slot, chunk) -> bool 이므로
    질의 정보는 클로저로 넘긴다.
    """
    spec = query_spec or {}
    query_entities = spec.get("entities", {}) or {}
    is_comparison = spec.get("intent") == "상품_비교"

    def matcher(slot: RequirementSlot, chunk: EvidenceChunk) -> bool:
        if _entity_conflict(query_entities, chunk.entities or {}, is_comparison):
            return False

        slot_terms = key_terms(f"{slot.description} {slot.slot_id}")
        chunk_terms = key_terms(chunk.text)
        # 청크에 태깅된 엔티티 값도 매칭 대상에 포함 (제목·상품명 등)
        for v in (chunk.entities or {}).values():
            chunk_terms |= key_terms(str(v))

        ok, hit = _overlap_ok(slot_terms, chunk_terms)
        if trace_sink is not None and ok:
            trace_sink.append({"slot": slot.slot_id, "doc": chunk.doc_id,
                               "matched_terms": sorted(hit)[:5]})
        return ok

    return matcher


# ════════════════════════════════════════════════════════════════
# 답변 커버리지
# ════════════════════════════════════════════════════════════════

_NUM = re.compile(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?')


def _numbers_in(text: str) -> set[float]:
    out: set[float] = set()
    for raw in _NUM.findall(text or ""):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _calc_key_numbers(result) -> set[float]:
    """계산 결과에서 '답변에 반드시 나와야 할' 수치를 뽑는다.

    보조 정보(분모, 적용세율 코드 등)까지 요구하면 과도하므로,
    dict의 값 중 수치형만 얕게 훑는다. variants 구조도 지원한다.
    """
    nums: set[float] = set()
    if isinstance(result, dict):
        if "variants" in result and isinstance(result["variants"], list):
            for v in result["variants"]:
                nums |= _calc_key_numbers(v.get("result"))
            return nums
        for k, v in result.items():
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float)):
                nums.add(float(v))
            elif isinstance(v, dict):
                nums |= _calc_key_numbers(v)
    elif isinstance(result, (int, float)) and not isinstance(result, bool):
        nums.add(float(result))
    return nums


def _num_present(target: float, answer_nums: set[float]) -> bool:
    """수치 표기 변형을 흡수해 답변에 등장하는지 확인.

    0.165 ↔ 16.5 (비율/퍼센트), 12000 ↔ 1.2 (억 표기) 정도만 허용한다.
    """
    cands = {target}
    if 0 < target < 1:
        cands.add(round(target * 100, 6))
    if target >= 10000:
        cands.add(round(target / 10000, 6))
    for c in cands:
        for a in answer_nums:
            if a == 0 and c == 0:
                return True
            if a and abs(c - a) / abs(a) <= 0.01:
                return True
    return False


def answer_covers_slot(answer: str, slot: RequirementSlot) -> bool:
    """답변이 이 슬롯을 실제로 반영했는지.

    · 계산 슬롯 → 계산 결과의 수치가 답변에 실제로 등장하는가
      (숫자가 없으면 "계산은 했지만 설명에 안 넣은" 상태다)
    · 사실 슬롯 → 슬롯 핵심어가 답변에 등장하는가
    """
    if not answer or not answer.strip():
        return False

    if slot.status == SlotStatus.CALC_DONE and slot.calc_result is not None:
        need = _calc_key_numbers(slot.calc_result)
        # 수치 없는 계산 결과(예: 한도 없음)는 용어 매칭으로 판정
        if need:
            answer_nums = _numbers_in(answer)
            return any(_num_present(n, answer_nums) for n in need)

    slot_terms = key_terms(f"{slot.description} {slot.slot_id}")
    slot_terms -= GENERIC_TERMS
    answer_terms = key_terms(answer)
    ok, _ = _overlap_ok(slot_terms, answer_terms)
    return ok
