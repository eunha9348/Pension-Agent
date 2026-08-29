"""L1 경로 분류 — 일반 경로냐 L4-sub냐.

━━ 왜 결정론적 코드인가 ━━
경로 선택을 HCX 재량에 맡기면 같은 질의가 실행마다 다른 계층을 타서
재현이 안 되고 디버깅도 불가능해진다. L1의 HCX는 **조건을 뽑고**,
그 결과를 보고 **경로는 코드가 정한다**. CLAUDE.md의 "판단은 코드,
문장은 LLM" 원칙이 여기에도 그대로 적용된다.

━━ 두 경로 ━━
GENERAL  계산이 특정되는 질의. 결정론적 계산함수가 더 정확하므로
         기존 L2→L3∥L4→L5→L5' 경로로 간다.
ADVISORY 계좌유형·판매클래스가 없고 개인 사정을 서술하며 방향을 묻는 질의.
         L4-sub가 근거를 검토해 HCX로 답한다.

⚠️ ADVISORY는 '답할 수 없는 질의'가 아니다. 오히려 그 반대다 —
   예전 같으면 거절되거나 빈 계산 카드를 받았을 질의를, 지금 있는 근거로
   답하고 부족한 정보를 정리해 주는 경로다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 계산이 특정된다는 신호 — 이게 있으면 결정론 경로가 낫다.
# 사용자가 수치를 주었거나, 특정 값을 콕 집어 물었다는 뜻이다.
_CALC_ANCHORS: tuple[str, ...] = (
    "얼마", "몇 퍼센트", "몇%", "몇 %", "계산", "세율", "한도",
    "며칠", "몇 년", "몇년", "몇 세", "몇살", "몇 살",
)

# 계산 인자로 직접 쓰이는 조건 키. 하나라도 있으면 계산이 가능하다.
#
# ⚠️ amount_manwon은 **일부러 뺐다.** 그것은 "질의에 금액이 하나 있으면
#    일단 담는" 범용 폴백이라, 연금 계산과 무관한 금액까지 들어온다.
#    실측: "현금 3,500만원이 있는데 연금계획을…" → amount_manwon=3500
#          "주택청약이 400만원 있는데 노후 대비를…" → amount_manwon=400
#    이걸 계산 조건으로 세면 범용 신호가 특정 판단을 좌우하게 되는데,
#    그게 바로 이번 개편이 걷어내려는 과최적화의 형태다.
_CALC_CONDITION_KEYS: frozenset[str] = frozenset({
    "account_value_manwon", "pension_year", "actual_receipt_year",
    "service_years", "severance_manwon", "pension_saving_manwon",
    "irp_manwon", "combined_contribution_manwon",
    "private_pension_annual_manwon", "private_pension_monthly_manwon",
    "total_income_manwon", "children_total", "years_elapsed",
})

# 상담·설계를 요청하는 신호. 계산이 아니라 방향을 묻는다.
#
# ⚠️ '어떻게 하'는 넣지 않는다. 너무 넓어서 절차 질의까지 끌어온다 —
#    "연금수령 개시 신청은 어떻게 하나요?", "종합소득세 신고는 어떻게
#    하나요?"는 정답이 문서에 있는 사실 질의라 결정론 경로가 맞다.
#    방향을 구하는 '어떻게 해야'만 남긴다.
_ADVISORY_SIGNALS: tuple[str, ...] = (
    "계획", "설계", "어떻게 해야", "어떡해", "어떻게 준비",
    "노후 대비", "노후대비", "추천", "조언", "상담", "괜찮을까",
    "방법이 있", "어디부터", "뭐부터", "어떤 게 좋",
    "어떤게 좋", "가입할까", "들어야",
)

# 개인 사정을 서술한다는 신호 (조건 키로는 안 잡히는 것들)
# '인데'를 포함해야 "나 몇살인데 연금 계획 좀"이 잡힌다 — 사용자가 자기
# 상황을 흘리듯 말하는 가장 흔한 형태다.
_PERSONAL_NARRATIVE = re.compile(
    r'(저는|제가|나는|내가|저희|우리|나\s)|'
    r'(있는데|있어요|있습니다|없는데|없어요|없습니다|입니다만|인데요|인데|이고)'
)


@dataclass
class RouteDecision:
    """경로 판정 결과. 사유를 반드시 남긴다 — trace로 추적 가능해야 한다."""

    route: str                      # "GENERAL" | "ADVISORY"
    reason: str
    signals: dict = field(default_factory=dict)

    @property
    def is_advisory(self) -> bool:
        return self.route == "ADVISORY"

    def as_trace(self) -> str:
        detail = " · ".join(f"{k}={v}" for k, v in self.signals.items())
        return f"경로 {self.route} — {self.reason}" + (f" ({detail})" if detail else "")


def classify_route(question: str,
                   conditions: dict | None = None,
                   asked_for: list | None = None) -> RouteDecision:
    """질의를 어느 경로로 보낼지 정한다.

    conditions : derive_conditions()가 뽑은 정규 조건 (21종)
    asked_for  : L1이 뽑은 요구사항 슬롯

    ━━ 판정 순서 (앞이 우선) ━━
    1. 계산 조건이 있다            → GENERAL (수치를 받았으면 계산이 정확하다)
    2. 계산함수가 지정됐다          → GENERAL
    3. 계좌유형·클래스가 있다       → GENERAL (기존 자격·비교 로직이 돈다)
    4. 상담 신호 + 개인 서술        → ADVISORY
    5. 계산 앵커가 전혀 없다        → ADVISORY (물을 수치 자체가 없다)
    6. 그 밖                        → GENERAL (기본값은 기존 경로)
    """
    q = question or ""
    cond = conditions or {}
    slots = asked_for or []

    has_calc_cond = bool(_CALC_CONDITION_KEYS & set(cond))
    has_calc_slot = any(
        isinstance(s, dict) and s.get("calc_function") for s in slots)
    has_account = bool(cond.get("account_type") or cond.get("fund_class"))
    advisory_signal = [s for s in _ADVISORY_SIGNALS if s in q]
    personal = bool(_PERSONAL_NARRATIVE.search(q))
    calc_anchor = [a for a in _CALC_ANCHORS if a in q]

    sig = {
        "계산조건": has_calc_cond, "계산슬롯": has_calc_slot,
        "계좌유형": has_account, "상담신호": len(advisory_signal),
        "개인서술": personal, "계산앵커": len(calc_anchor),
    }

    if has_calc_cond:
        return RouteDecision("GENERAL", "계산에 넣을 수치가 확인됨", sig)
    if has_calc_slot:
        return RouteDecision("GENERAL", "계산함수가 지정된 요구사항이 있음", sig)
    if has_account:
        return RouteDecision("GENERAL", "계좌유형·판매클래스가 확인됨", sig)
    if advisory_signal and personal:
        return RouteDecision(
            "ADVISORY",
            f"개인 사정 서술 + 상담 요청('{advisory_signal[0]}') — "
            f"계좌유형·클래스·수치가 없어 계산으로 답할 수 없음", sig)
    if advisory_signal and not calc_anchor:
        return RouteDecision(
            "ADVISORY",
            f"상담 요청('{advisory_signal[0]}')이고 물을 수치가 없음", sig)
    # ⚠️ 개인 서술을 요구한다. 이게 없으면 "1500만원 넘으면 분리과세를
    #    선택해야 하나요?" 같은 **정답이 정해진 제도 질의**까지 ADVISORY로
    #    끌려간다. 그런 질의는 결정론 경로가 더 정확하다.
    if personal and not calc_anchor and not slots:
        return RouteDecision(
            "ADVISORY", "개인 사정 서술뿐 — 물을 수치도 요구사항도 없음", sig)

    return RouteDecision("GENERAL", "기본 경로", sig)
