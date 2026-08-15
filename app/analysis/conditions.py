"""사용자 조건(user_conditions) 정규화.

질의 원문과 L1 산출물에서 나온 조건을 **하나의 정규 스키마**로 모은다.
계산 인자 조립(calc_params.py)과 질의 분석(query_spec.py)이 이 스키마를 공유한다.

━━ 단위 규칙 ━━
`*_manwon` 으로 끝나는 키는 전부 만원 단위다. 원 단위 값이 이 스키마에
들어오는 일은 없어야 한다 — 변환은 units.py 경계 함수에서만 한다.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.analysis.units import (parse_age, parse_amount_to_manwon,
                                parse_pension_year, parse_rate,
                                parse_service_years, won_to_manwon)

# ── 계좌 유형 인식 ────────────────────────────────────────────
_ACCOUNT_SIGNALS: list[tuple[str, tuple[str, ...]]] = [
    ("IRP", ("irp", "IRP", "개인형퇴직연금", "개인형 퇴직연금", "아이알피")),
    ("연금저축", ("연금저축", "연금 저축", "연저축")),
    ("퇴직연금", ("퇴직연금", "DC형", "DB형", "확정기여", "확정급여")),
]

_ANNUITY_SIGNALS = ("종신", "종신형", "평생 받는", "사망할 때까지")
_PERIOD_SIGNALS = ("확정기간", "기간형", "정해진 기간")

_LEGACY_JOIN_SIGNALS = ("2013년 이전", "2013.3.1 이전", "2013년 3월 이전",
                        "2012년에 가입", "2013년 전에")


def _find_amount_near(question: str, keywords: tuple[str, ...]) -> Optional[float]:
    """특정 키워드 주변(같은 절)에서 금액을 찾는다.

    "연금저축에 400만원, IRP에 300만원" 처럼 금액이 여럿일 때
    어느 금액이 어느 계좌 것인지 구분하기 위한 국소 탐색.
    """
    # ⚠️ 쉼표로 절을 나누되 "4,000만원"의 자릿수 구분 쉼표는 나누면 안 된다.
    for clause in re.split(r'(?<!\d),(?!\d)|[·\n]| 그리고 | 및 ', question or ""):
        if any(k in clause for k in keywords):
            amt = parse_amount_to_manwon(clause)
            if amt is not None:
                return amt
    return None


def derive_conditions(question: str,
                      llm_conditions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """질의 원문 + L1 조건을 정규 스키마로 병합.

    L1(HyperCLOVA X)이 준 값을 우선하되, L1이 비어 있으면(=mock/실패)
    규칙 기반 파싱이 그 자리를 메운다. 즉 **LLM 없이도 조건 추출이 동작한다.**
    """
    q = question or ""
    c: dict[str, Any] = {}

    # ── 계좌 유형 ──
    for label, signals in _ACCOUNT_SIGNALS:
        if any(s in q for s in signals):
            c["account_type"] = label
            break

    # ── 나이·연차 ──
    if (age := parse_age(q)) is not None:
        c["age"] = age
    py = parse_pension_year(q)
    if py is not None:
        # ⚠️ "○년차"는 연금수령연차로만 해석한다.
        #    연금실제수령연차는 '실제 인출한 해'만 누적되는 별개 개념이므로
        #    같은 표현에서 추론하지 않는다 (doc40 함정 B1).
        c["pension_year"] = py
        if any(k in q for k in ("실제수령연차", "실제 수령연차", "실제로 인출")):
            c["actual_receipt_year"] = py
    if (sy := parse_service_years(q)) is not None:
        c["service_years"] = sy
    if any(k in q for k in _LEGACY_JOIN_SIGNALS):
        c["join_before_2013"] = True

    # ── 금액 ──
    if (saving := _find_amount_near(q, ("연금저축", "연저축"))) is not None:
        c["pension_saving_manwon"] = saving
    if (irp := _find_amount_near(q, ("IRP", "irp", "개인형"))) is not None:
        c["irp_manwon"] = irp
    if (sev := _find_amount_near(q, ("퇴직금", "퇴직급여", "명예퇴직", "명퇴"))) is not None:
        c["severance_manwon"] = sev
    if (av := _find_amount_near(q, ("평가액", "적립금", "잔고", "계좌에", "쌓여", "모았"))) is not None:
        c["account_value_manwon"] = av
    if (inc := _find_amount_near(q, ("총급여", "연봉", "종합소득"))) is not None:
        c["total_income_manwon"] = inc
        c["income_type"] = "종합소득" if "종합소득" in q else "총급여"

    # 월/연 단위 연금 수령액
    if (m := re.search(r'(?:매달|매월|월)\s*([^\s,]{1,12})\s*(?:씩|정도)?\s*(?:받|수령|나오)', q)):
        if (v := parse_amount_to_manwon(m.group(1))) is not None:
            c["private_pension_monthly_manwon"] = v
    if (m := re.search(r'(?:연간|1년에|해마다|매년)\s*([^\s,]{1,12})\s*(?:씩|정도)?\s*(?:받|수령|나오)', q)):
        if (v := parse_amount_to_manwon(m.group(1))) is not None:
            c["private_pension_annual_manwon"] = v

    # 문맥 없는 단일 금액은 보조 후보로만 둔다 (용도를 단정하지 않는다)
    if (generic := parse_amount_to_manwon(q)) is not None:
        c.setdefault("amount_manwon", generic)

    # ── 수령 형태 ──
    if any(s in q for s in _ANNUITY_SIGNALS):
        c["is_annuity_type"] = True
    elif any(s in q for s in _PERIOD_SIGNALS):
        c["is_annuity_type"] = False

    # ── 세율 ──
    if (r := parse_rate(q)) is not None:
        c["rate"] = r

    # ── 판매 클래스 ──
    # ⚠️ \b 금지 — "C-P로" 처럼 뒤에 한글이 붙으면 단어 경계로 인식되지 않는다
    if (m := re.search(
            r'(?<![A-Za-z0-9-])([CS]-(?:P2E|P2|Pe|PE|RF|RJ|Re|[PRFW3])'
            r'|Crp-e|Crp|S-I)(?![A-Za-z0-9-])', q)):
        c["fund_class"] = m.group(1)

    # ── 자녀 수 (출산크레딧) ──
    if (m := re.search(r'(?:자녀|아이|애).{0,4}?(\d)\s*(?:명|인)', q)):
        c["children_total"] = int(m.group(1))

    # ── 경과 연수 ──
    if (ye := _elapsed_years(q)) is not None:
        c["years_elapsed"] = ye

    # ── L1 조건이 있으면 덮어쓴다 (원 단위 → 만원 변환 포함) ──
    for k, v in (llm_conditions or {}).items():
        if v in (None, "", []):
            continue
        if k.endswith("_won"):
            try:
                c[k[:-4] + "_manwon"] = round(won_to_manwon(float(v)), 4)
            except (TypeError, ValueError):
                continue
        else:
            c[k] = v

    # 연/월 수령액 상호 보정
    if "private_pension_annual_manwon" not in c and "private_pension_monthly_manwon" in c:
        c["private_pension_annual_manwon"] = round(
            c["private_pension_monthly_manwon"] * 12, 4)
    if "private_pension_monthly_manwon" not in c and "private_pension_annual_manwon" in c:
        c["private_pension_monthly_manwon"] = round(
            c["private_pension_annual_manwon"] / 12, 4)

    return c


def _elapsed_years(q: str) -> Optional[int]:
    m = re.search(r'(?:가입|개설|시작)한?\s*지\s*(\d{1,2})\s*년', q or "")
    if m:
        return int(m.group(1))
    m = re.search(r'(\d{1,2})\s*년\s*(?:이\s*)?(?:지났|경과|됐|되었)', q or "")
    if m:
        return int(m.group(1))
    return None


def describe_conditions(conditions: dict[str, Any]) -> str:
    """[확인된 조건] 블록에 넣을 사람이 읽는 요약."""
    from app.analysis.units import format_manwon

    label = {
        "account_type": "계좌 유형", "age": "나이", "pension_year": "연금수령연차",
        "actual_receipt_year": "연금실제수령연차", "service_years": "근속연수",
        "pension_saving_manwon": "연금저축 납입액", "irp_manwon": "IRP 납입액",
        "severance_manwon": "퇴직급여", "account_value_manwon": "계좌 평가액",
        "total_income_manwon": "소득", "years_elapsed": "가입 후 경과연수",
        "private_pension_annual_manwon": "연간 연금수령액",
        "is_annuity_type": "수령 형태", "fund_class": "판매 클래스",
        "join_before_2013": "2013.3.1 이전 가입",
    }
    parts = []
    for k, v in conditions.items():
        if k not in label or v is None:
            continue
        if k.endswith("_manwon"):
            parts.append(f"{label[k]} {format_manwon(v)}")
        elif k == "is_annuity_type":
            parts.append(f"{label[k]} {'종신형' if v else '확정기간형'}")
        elif k == "age":
            parts.append(f"{label[k]} {v}세")
        elif isinstance(v, bool):
            if v:
                parts.append(label[k])
        else:
            parts.append(f"{label[k]} {v}")
    return ", ".join(parts)
