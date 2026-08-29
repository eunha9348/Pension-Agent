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

# ⚠️ 작은 수라도 **단위가 붙으면 주장이다** — 반드시 대조한다.
#
# 실제 사고: "단기는 만기 3개월~3년, 중장기는 1년~7년에 투자합니다"라는
# 답변이 나왔는데 근거 문서 어디에도 없는 수치였다. 그런데 3·7·1이 전부
# _TRIVIAL_MAX 이하라 **검증 시도조차 되지 않았고**, 트레이스에는
# "0개 수치 전부 근거 확인"이 통과로 찍혔다(Q-002 실패).
#
# 연차·나이·등급·기간은 이 도메인에서 답변의 핵심 주장이다. 반면
# 조문번호(제12조)·목록순서(1.)는 여전히 제외해야 하므로, '단위가 붙었는가'로
# 가른다. 조문·연도는 아래 _EXCLUDE_CONTEXT에서 이미 걸러진 뒤다.
_UNIT_BEARING = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:%|퍼센트|년차|년|개월|달|세|등급|배|회|건|명|주|일)')


def _strip_excluded(text: str) -> str:
    """연도·조문번호 등 대조 불필요 구간을 마스킹."""
    out = text
    for pat in _EXCLUDE_CONTEXT:
        out = pat.sub(' ', out)
    return out


def _unit_bearing_values(cleaned: str) -> set[float]:
    """단위가 붙은 수치 — 작아도 검증 대상이다."""
    out: set[float] = set()
    for raw in _UNIT_BEARING.findall(cleaned):
        try:
            out.add(float(raw.replace(',', '')))
        except ValueError:
            continue
    return out


def extract_numbers(text: str, include_trivial: bool = False) -> set[float]:
    """텍스트에서 수치를 추출해 float 집합으로 반환.

    include_trivial=False 여도 **단위가 붙은 작은 수는 포함**한다.
    (근거 쪽 추출은 include_trivial=True로 부르므로 영향이 없고,
     답변 쪽 추출에서만 대조 범위가 넓어진다.)
    """
    cleaned = _strip_excluded(text)
    keep_small = set() if include_trivial else _unit_bearing_values(cleaned)

    result: set[float] = set()
    for raw in _NUM.findall(cleaned):
        try:
            v = float(raw.replace(',', ''))
        except ValueError:
            continue
        if (not include_trivial and v.is_integer() and abs(v) <= _TRIVIAL_MAX
                and v not in keep_small):
            continue          # 순서·개월 등으로 쓰이는 작은 정수는 제외
        result.add(v)
    return result


def _flatten_numbers(obj: Any) -> set[float]:
    """계산 결과 dict/list에서 모든 수치를 재귀 추출.

    ━━ 만원 단위 값은 원 단위 표기도 함께 허용한다 ━━
    계산함수는 전부 만원 단위인데(CLAUDE.md), 답변은 사람이 읽는 글이라
    "66만원"을 "660,000원"으로 쓰는 일이 흔하다. 그러면 대조 집합에 66만
    있고 660000은 없어서, **맞는 값을 날조로 판정한다.** 실측(L10)에서
    660000이 '근거 없는 수치'로 잡혀 답변이 축퇴됐다.

    ⚠️ 단위를 아는 키에서만 환산한다. 모든 수에 ×10000을 적용하면
       연차·나이 같은 값까지 거대한 후보를 만들어 날조를 통과시킨다.
    """
    from app.generation.render import _UNIT_MANWON

    found: set[float] = set()
    if isinstance(obj, bool):
        return found
    if isinstance(obj, (int, float)):
        found.add(float(obj))
    elif isinstance(obj, str):
        found |= extract_numbers(obj, include_trivial=True)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found |= _flatten_numbers(v)
            if (k in _UNIT_MANWON and isinstance(v, (int, float))
                    and not isinstance(v, bool)):
                found.add(float(v) * 10_000)      # 만원 → 원
                # ⚠️ **표시 반올림도 허용해야 한다.** format_manwon은 만원
                #    단위에서 정수로 반올림한다(76.56 → "77만원"). 그런데
                #    대조 집합에는 원본 76.56만 있어서, 시스템이 스스로
                #    표시한 값을 '근거 없는 수치'로 판정했다.
                #    상대오차 0.5%로는 못 흡수한다 — 77 vs 76.56은 0.575%로
                #    간발의 차로 걸린다(2026-08-29 실측).
                #
                #    이건 오차 허용을 늘리는 것과 다르다. render_calc_result가
                #    표시한 값은 **계산함수 출력에서 결정론적으로 파생된 것**
                #    이므로 정의상 근거가 있다. LLM이 지어낸 수가 아니다.
                #    (허용 오차를 키우면 진짜 날조까지 통과하므로 그 길로
                #     가면 안 된다.)
                rounded = float(round(float(v)))
                found.add(rounded)
                found.add(rounded * 10_000)
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
        # ⚠️ 대조를 **건너뛴 것**과 대조해서 **통과한 것**을 같은 말로
        #    쓰면 안 된다. 예전에는 검사 대상이 0건일 때도 "통과"로 찍혀서,
        #    사실 아무것도 확인하지 않은 답변이 검증을 통과한 것처럼 보였다.
        #    트레이스를 근거로 신뢰를 판단하는 심사에서는 치명적이다.
        if self.checked_count == 0:
            return "수치 대조 — 답변에 대조할 수치가 없어 검사하지 않음 (통과 아님)"
        if self.passed:
            return f"수치 대조 검증 통과 — {self.checked_count}개 수치 전부 근거 확인"
        return (f"수치 대조 검증 실패 — 근거 없는 수치 {len(self.ungrounded)}건: "
                f"{sorted(self.ungrounded)[:5]}")


@dataclass
class PresenceResult:
    """계산 결과가 답변에 실렸는지 (verify_numeric_grounding의 반대 방향)."""
    passed: bool
    missing: list[tuple[str, float, str]] = field(default_factory=list)
    required_count: int = 0

    def as_trace(self) -> str:
        if self.required_count == 0:
            return "계산값 표기 — 답변에 실려야 할 계산 결과가 없음"
        if self.passed:
            return (f"계산값 표기 확인 — 계산 결과 {self.required_count}건이 "
                    f"모두 답변에 실림")
        items = ", ".join(f"{label} {shown}" for label, _v, shown in self.missing[:4])
        return (f"계산값 표기 누락 — 계산했으나 답변에 없는 값 "
                f"{len(self.missing)}건: {items}")

    def instruction(self) -> str:
        """L5' 재생성에 붙일 시정 지시."""
        lines = [f"· {label}: {shown}" for label, _v, shown in self.missing]
        return ("아래 계산 결과가 답변 본문에 빠져 있습니다. 사용자가 물은 "
                "수치이므로 반드시 문장 안에 그대로 적으십시오. "
                "값을 바꾸거나 새로 계산하지 마십시오.\n" + "\n".join(lines))


# 계산 결과 dict에서 '답변에 실려야 할 값'을 고르는 기준.
# 분모·연차 같은 중간값까지 요구하면 멀쩡한 답변이 실패하므로 금액·비율만 본다.
_PRESENCE_SKIP = {"source", "rate_source", "DEPRECATED", "note", "기준", "action",
                  "doc_id", "markers", "is_legacy_suspect", "reason", "params",
                  "label", "denominator", "unlimited", "eligible", "comparable",
                  "choice_required"}


# 제도가 정한 **상수 한도**. 계산값이 아니라 맥락이다.
#
# ⚠️ 이 값들을 언제나 요구하면 안 된다. calc_private_contribution_limit은
#    납입액을 몰라도 한도를 내보내도록 고쳐졌는데(E-01), 그 결과 납입액을
#    아는 질의에서도 세 한도가 전부 '답변에 실려야 할 값'으로 잡혔다.
#    실측: "연금저축에 1200만원을 넣으면 전부 세액공제 되나요?"(A08)에
#    답변이 600만원·99만원·79만원을 정확히 제시했는데도, 묻지도 않은
#    900만원·1,800만원이 없다는 이유로 CALC_NOT_SHOWN → REVISE → 재생성
#    실패 → PARTIAL 강등까지 갔다. 맞는 답이 규칙 때문에 깎인 것이다.
_LIMIT_CONSTANTS = {"연금저축_단독_한도", "연금저축_IRP_합산_한도",
                    "연간_총납입한도"}

# 이것이 있으면 '계산이 이뤄진 질의'다 — 그때 한도는 맥락이 된다.
_COMPUTED_KEYS = {"A_tax_credit", "T_withholding", "limit", "difference",
                  "산출세액", "합계"}


def _presence_targets(result: Any, prefix: str = "") -> list[tuple[str, float, str]]:
    """계산 결과에서 (라벨, 값, 표기) 목록을 뽑는다. variants 구조도 훑는다."""
    from app.generation.render import _UNIT_MANWON, _UNIT_RATE, format_value, label_of

    out: list[tuple[str, float, str]] = []
    if not isinstance(result, dict):
        return out

    if isinstance(result.get("variants"), list):
        for v in result["variants"]:
            tag = str(v.get("label") or "조건")
            out.extend(_presence_targets(v.get("result"), f"{tag} "))
        return out

    # 계산값이 나온 질의에서는 상수 한도를 요구하지 않는다.
    # 계산값이 없으면(= 한도만 안내하는 질의) 한도가 곧 답이므로 요구한다.
    computed = any(k in result for k in _COMPUTED_KEYS)

    for key, value in result.items():
        if computed and key in _LIMIT_CONSTANTS:
            continue
        if key in _PRESENCE_SKIP or not isinstance(value, (int, float)):
            continue
        if isinstance(value, bool):
            continue
        # 금액·비율로 분류된 키만 요구한다 (개수·연차는 설명에 없어도 된다)
        if key not in _UNIT_MANWON and key not in _UNIT_RATE:
            continue
        out.append((prefix + label_of(key), float(value), format_value(key, value)))
    return out


def verify_calc_presence(answer: str,
                         calc_results: Iterable[Any] = ()) -> PresenceResult:
    """계산한 수치가 답변 문장에 실제로 실렸는지 검증.

    ━━ 왜 필요한가 (verify_numeric_grounding으로는 못 잡는다) ━━
    기존 검증기는 **답변의 수치 → 근거**만 본다. 즉 날조는 막지만,
    LLM이 계산 결과를 아예 안 쓰고 원론적인 설명만 늘어놓는 경우는
    그대로 통과한다. 실제로 그랬다:

       질의  "1억원, 연금수령 1년차 — 얼마까지 인출 가능?"
       계산  연금수령한도 = 1,200만원   ← 정확히 계산됨
       답변  "연금수령한도는 계좌평가액과 연금수령연차로 산정됩니다..."
             → 숫자가 없다. 그런데 대조할 수치도 없으니 검증 '통과'.

    "계산은 함수, 설명은 LLM"이 성립하려면 함수의 출력이 사용자에게
    **도달해야** 한다. 도달 여부를 확인하지 않으면 그 원칙은 절반만 지켜진다.

    ━━ 표기 흔들림을 흡수한다 ━━
    "1,200만원" · "1200만원" · "1억 2,000만원"을 모두 같은 값으로 본다
    (parse_amount_expressions가 억 단위까지 해석한다).
    """
    from app.analysis.units import parse_amount_expressions

    targets: list[tuple[str, float, str]] = []
    for r in calc_results:
        targets.extend(_presence_targets(r))

    if not targets:
        return PresenceResult(True, [], 0)

    # 답변에 등장하는 값 — 금액 표현과 일반 수치 양쪽에서 모은다
    present: set[float] = {v for _s, _e, v in parse_amount_expressions(answer)}
    present |= extract_numbers(answer, include_trivial=True)

    missing = [t for t in targets if not _matches(t[1], present)]
    return PresenceResult(passed=not missing, missing=missing,
                          required_count=len(targets))


def verify_numeric_grounding(answer: str,
                              calc_results: Iterable[Any] = (),
                              evidence_texts: Iterable[str] = (),
                              question: str = "") -> VerificationResult:
    """답변의 모든 수치가 계산 결과·근거문서·질의에 실재하는지 검증.

    calc_results   : Prediction Agent가 반환한 dict들의 목록
    evidence_texts : retrieved_context에 포함된 근거문서 원문들
    question       : 사용자 질의 원문

    ━━ 질의를 허용 근거에 넣는 이유 ━━
    사용자가 말한 숫자를 답변이 되짚는 것은 날조가 아니다. 그런데 예전에는
    질의가 대조 집합에 없어서, "만 65세가 연 1200만원 받으면"이라는 질문에
    "만 65세", "1,200만원"이라고 답하면 그 두 수가 **근거 없는 수치**로
    잡혔다(실측 L10 — 65.0과 12000000.0이 날조로 분류돼 답변이 축퇴됨).
    확인된 조건을 다시 언급하는 것은 상담 답변의 기본 형식이므로 허용한다.

    반환 passed=False 이면 호출 측에서 ① 재생성 1회 시도 후
    ② 그래도 실패하면 fallback 템플릿으로 축퇴시킬 것.
    """
    allowed: set[float] = set()
    for r in calc_results:
        allowed |= _flatten_numbers(r)
    for t in evidence_texts:
        allowed |= extract_numbers(t, include_trivial=True)
    if question:
        allowed |= extract_numbers(question, include_trivial=True)
        # 질의는 원 단위로 쓰이는 일이 많다("1200만원" → 12,000,000원).
        # 만원 단위 계산 체계와 맞물리도록 양방향을 함께 허용한다.
        from app.analysis.units import parse_amount_expressions
        for _s, _e, v in parse_amount_expressions(question):
            allowed |= {v, v * 10_000}

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
