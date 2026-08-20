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

from app.analysis.units import (parse_age, parse_amount_expressions,
                                parse_amount_to_manwon, parse_pension_year,
                                parse_rate, parse_service_years, won_to_manwon)

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

# 두 계좌의 납입액을 합쳐서 말하는 표현
_COMBINED_SIGNALS = ("합쳐", "합산", "합해", "다 합", "총합", "같이 넣", "둘 다 합")


# 키워드 뒤 몇 글자까지를 '그 키워드에 딸린 금액'으로 볼 것인가
_NEAR_WINDOW = 25


def _find_amount_near(question: str, keywords: tuple[str, ...]) -> Optional[float]:
    """키워드에 **가장 가까운 금액 표현 하나**를 고른다.

    ⚠️ 절 단위로 잘라 그 안의 금액을 합치면 안 된다. 실제로
       "총급여 4000만원인데 연금저축에 600만원"이 4,600만원 하나로 합쳐져
       연금저축 납입액이 4,600만원으로 잡히는 버그가 있었다
       ("연금저축에 400만원 IRP에 300만원"은 양쪽 다 700만원이 됐다).

    규칙: 키워드 **뒤쪽**에 있는 가장 가까운 금액을 우선한다
    ("연금저축에 600만원" — 한국어는 수식어가 앞에 온다).
    뒤쪽 창 안에 없으면 앞쪽에서 가장 가까운 것을 본다("600만원을 연금저축에").
    """
    text = question or ""
    exprs = parse_amount_expressions(text)
    if not exprs:
        return None

    best: Optional[tuple[int, float]] = None      # (거리, 금액)
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text):
            kw_end = m.end()
            for start, end, value in exprs:
                if start >= kw_end:
                    distance = start - kw_end
                    if distance > _NEAR_WINDOW:
                        continue
                    rank = distance                     # 뒤쪽 우선
                else:
                    rank = _NEAR_WINDOW + (m.start() - end)  # 앞쪽은 후순위
                    if m.start() - end > _NEAR_WINDOW:
                        continue
                if best is None or rank < best[0]:
                    best = (rank, value)
    return best[1] if best else None


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
    saving = _find_amount_near(q, ("연금저축", "연저축"))
    irp = _find_amount_near(q, ("IRP", "irp", "개인형"))
    if (saving is not None and saving == irp
            and any(k in q for k in _COMBINED_SIGNALS)):
        # "연금저축이랑 IRP 합쳐서 900만원" — 같은 절에서 두 계좌가 같은 금액으로
        # 잡힌 경우는 '각각'이 아니라 '합산'이다. 각각으로 해석하면 납입액이
        # 두 배가 되고, 연금저축 600만원 상한 판정도 틀어진다.
        c["combined_contribution_manwon"] = saving
        c.setdefault("condition_notes", []).append(
            "연금저축과 IRP의 납입액 구분이 확인되지 않아 합산액 기준으로 계산했습니다. "
            "연금저축 단독 납입액이 600만원을 넘으면 결과가 달라질 수 있습니다")
    else:
        if saving is not None:
            c["pension_saving_manwon"] = saving
        if irp is not None:
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

    # ── 수령한도 질의의 무맥락 금액은 계좌 평가액으로 본다 ──
    # "1억이고 연금수령 10년차면 한도가?" 처럼 '계좌에·평가액' 같은 단서 없이
    # 금액만 던지는 질의가 흔하다. 이때 account_value_manwon이 안 잡히면
    # 연금수령한도 계산이 통째로 못 돌아 답변에 숫자가 빠진다(평가 E-05).
    #
    # 무제한으로 넘기지 않는다 — 용도가 이미 밝혀진 금액이 하나라도 있으면
    # 그 금액을 계좌 평가액으로 재해석하는 셈이 되므로 건드리지 않는다.
    # ("연금저축에 600만원 넣었는데 세액공제 한도가?" → 600을 평가액으로
    #  잡으면 안 된다.)
    _PURPOSED = ("pension_saving_manwon", "irp_manwon", "severance_manwon",
                 "total_income_manwon", "combined_contribution_manwon",
                 "private_pension_annual_manwon", "private_pension_monthly_manwon")
    if ("account_value_manwon" not in c
            and c.get("amount_manwon") is not None
            and not any(k in c for k in _PURPOSED)
            and any(k in q for k in ("수령한도", "인출한도", "얼마까지 인출",
                                     "얼마나 인출", "얼마까지 뽑", "한도"))
            and not any(k in q for k in ("세액공제", "공제한도", "납입한도",
                                         "공제 한도"))):
        c["account_value_manwon"] = c["amount_manwon"]
        c.setdefault("condition_notes", []).append(
            "질의에 금액의 용도가 명시되지 않아 연금계좌 평가액으로 보고 계산했습니다. "
            "해당 금액이 평가액이 아니라면 결과가 달라집니다")

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
        "combined_contribution_manwon": "연금저축+IRP 합산 납입액",
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
