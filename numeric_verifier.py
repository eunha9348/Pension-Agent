"""
수치 대조 검증기 (Numeric Grounding Verifier)
==============================================
답변에 등장하는 모든 수치가 ① Prediction Agent 계산 결과 또는
② 검색된 근거문서 원문 중 하나에 실재하는지 대조한다.

설계 원칙
---------
· LLM 호출 없음 — 정규식 + 집합 연산만 사용 (대회 LLM 단일사용 조건과 무관)
· 실패 시 차단이 기본 — 통과시키는 쪽이 아니라 막는 쪽으로 기울인다
· 한국어 금융 문서의 수치 표기 다양성을 흡수 (1,500만원 / 1500만 / 15% / 5.5~3.3%)

한계 (반드시 인지할 것)
----------------------
· 파생 수치는 잡지 못한다. 예) 근거에 "총보수 0.544%"가 있고 답변이
  "10년이면 5.44%"라고 쓰면, 5.44는 어디에도 없으므로 차단된다(정상).
  그러나 답변이 계산 결과를 그대로 인용하면서 문맥을 왜곡하는 경우
  (숫자는 맞고 설명이 틀린 경우)는 이 검증기로 잡을 수 없다.
· 서수·연도·조항번호 등 과세와 무관한 수치도 대조 대상이 되므로
  화이트리스트(연도, 조 항 번호 패턴)로 제외한다.
· 즉 이 검증기는 **수치 환각을 막는 장치이지 의미 오류를 막는 장치가 아니다.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# ── 수치 추출 ────────────────────────────────────────────────

# 1,500 / 1500 / 0.544 / 5.5 형태
_NUM = re.compile(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?')

# 대조 대상에서 제외할 패턴 (연도, 조문번호, 목록 번호 등)
_EXCLUDE_CONTEXT = [
    re.compile(r'(19|20)\d{2}\s*년'),          # 2024년
    re.compile(r'제\s*\d+\s*[조항호목절부장]'),   # 제47조, 제2항
    re.compile(r'\d+\s*페이지'),
    re.compile(r'doc\s*\d+', re.I),
    re.compile(r'R2_KR\w+', re.I),
]

# 무시할 사소한 수치 (순서, 개수 등으로 쓰이는 한 자리 수)
_TRIVIAL_MAX = 12


def _strip_excluded(text: str) -> str:
    """연도·조문번호 등 대조 불필요 구간을 마스킹."""
    out = text
    for pat in _EXCLUDE_CONTEXT:
        out = pat.sub(' ', out)
    return out


def extract_numbers(text: str, include_trivial: bool = False) -> set[float]:
    """텍스트에서 수치를 추출해 float 집합으로 반환."""
    cleaned = _strip_excluded(text)
    result: set[float] = set()
    for raw in _NUM.findall(cleaned):
        try:
            v = float(raw.replace(',', ''))
        except ValueError:
            continue
        if not include_trivial and v.is_integer() and abs(v) <= _TRIVIAL_MAX:
            continue          # 1~12 정수는 순서·개월 등으로 쓰이므로 기본 제외
        result.add(v)
    return result


def _flatten_numbers(obj: Any) -> set[float]:
    """계산 결과 dict/list에서 모든 수치를 재귀 추출."""
    found: set[float] = set()
    if isinstance(obj, bool):
        return found
    if isinstance(obj, (int, float)):
        found.add(float(obj))
    elif isinstance(obj, str):
        found |= extract_numbers(obj, include_trivial=True)
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _flatten_numbers(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            found |= _flatten_numbers(v)
    return found


# ── 허용 변형 ────────────────────────────────────────────────

def _variants(v: float) -> set[float]:
    """같은 값의 표기 변형을 생성.

    · 비율: 0.165 ↔ 16.5  (소수 ↔ 퍼센트)
    · 세율: 0.15 ↔ 0.165  (국세 ↔ 지방소득세 포함)  ※ 문서 표기 차이 흡수
    · 단위: 1500 ↔ 15  (만원 ↔ 억 단위 축약은 다루지 않음 — 오탐 위험)
    """
    out = {v}
    if 0 < v < 1:
        out.add(round(v * 100, 6))
    if 1 <= v <= 100:
        out.add(round(v / 100, 6))
    # 지방소득세 포함/미포함 환산 (세율 범위에서만)
    if 0 < v <= 50:
        out.add(round(v * 1.1, 6))
        out.add(round(v / 1.1, 6))
    return out


def _matches(target: float, allowed: set[float], rel_tol: float = 0.005) -> bool:
    """target이 allowed 안의 어떤 값과 (변형 포함) 일치하는지."""
    for cand in _variants(target):
        for a in allowed:
            if a == 0 and cand == 0:
                return True
            if a != 0 and abs(cand - a) / abs(a) <= rel_tol:
                return True
            if abs(cand - a) < 1e-9:
                return True
    return False


# ── 검증 ─────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    passed: bool
    ungrounded: list[float] = field(default_factory=list)
    checked_count: int = 0
    reason: str = ""

    def as_trace(self) -> str:
        if self.passed:
            return f"수치 대조 검증 통과 — {self.checked_count}개 수치 전부 근거 확인"
        return (f"수치 대조 검증 실패 — 근거 없는 수치 {len(self.ungrounded)}건: "
                f"{sorted(self.ungrounded)[:5]}")


def verify_numeric_grounding(answer: str,
                              calc_results: Iterable[Any] = (),
                              evidence_texts: Iterable[str] = ()) -> VerificationResult:
    """답변의 모든 수치가 계산 결과 또는 근거문서에 실재하는지 검증.

    calc_results   : Prediction Agent가 반환한 dict들의 목록
    evidence_texts : retrieved_context에 포함된 근거문서 원문들

    반환 passed=False 이면 호출 측에서 ① 재생성 1회 시도 후
    ② 그래도 실패하면 fallback 템플릿으로 축퇴시킬 것.
    """
    allowed: set[float] = set()
    for r in calc_results:
        allowed |= _flatten_numbers(r)
    for t in evidence_texts:
        allowed |= extract_numbers(t, include_trivial=True)

    answer_nums = extract_numbers(answer)
    ungrounded = [n for n in answer_nums if not _matches(n, allowed)]

    if not answer_nums:
        return VerificationResult(True, [], 0, "답변에 대조 대상 수치 없음")

    return VerificationResult(
        passed=not ungrounded,
        ungrounded=ungrounded,
        checked_count=len(answer_nums),
        reason=("전부 근거 확인" if not ungrounded
                else f"{len(ungrounded)}개 수치가 계산결과·근거문서 어디에도 없음"),
    )


# ── 출처 태그 검증 ────────────────────────────────────────────

def verify_source_disclosure(answer: str, calc_results: Iterable[Any]) -> dict:
    """계산 결과 중 출처가 '일반 세법'인 항목이 쓰였다면,
    답변이 그 사실을 밝히고 있는지 확인.

    제공자료 밖의 근거를 사용하면서 출처를 숨기면
    평가지표 '근거 완전성'·'근거 기반'에서 감점 요인이 된다.
    """
    external_used, disclosed = False, False
    for r in calc_results:
        if isinstance(r, dict):
            blob = str(r)
            if '일반 세법' in blob or '제공문서 외' in blob or '외부' in blob:
                external_used = True
    for marker in ('일반 세법', '제공 자료 외', '제공자료 외', '별도 확인', '세법 기준'):
        if marker in answer:
            disclosed = True
            break
    return {
        "external_source_used": external_used,
        "disclosed_in_answer": disclosed,
        "ok": (not external_used) or disclosed,
        "action": ("" if (not external_used) or disclosed
                   else "제공자료 외 근거를 사용했으나 답변에 명시되지 않음 — 문구 추가 필요"),
    }
