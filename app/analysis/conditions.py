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


# 금액 뒤에 이 말이 오면 잔고가 아니라 **납입액**이다.
_CONTRIB_VERB = re.compile(r'(납입|넣|불입|납부|적립하|저축하)')
_CONTRIB_WINDOW = 14        # "900만원을 한꺼번에 납입" 정도까지 본다


def _followed_by_contribution(question: str, value: float) -> bool:
    """그 금액 표현 바로 뒤에 납입 동사가 오는가.

    "연금계좌에 900만원 **납입**하면" → True (납입액)
    "계좌에 1억원 **있고**"            → False (평가액)
    """
    for start, end, v in parse_amount_expressions(question or ""):
        if v == value:
            if _CONTRIB_VERB.search(question[end:end + _CONTRIB_WINDOW]):
                return True
    return False


# 금액 앞뒤에 이 말이 붙으면 납입액이 아니라 **잔고**다.
_BALANCE_NOUN = ("평가액", "평가금액", "적립금", "잔고", "잔액", "모아둔", "쌓인")
_BALANCE_VERB = re.compile(r'(있|쌓여|모았|모아|보유)')


def _is_balance_amount(question: str, value: float) -> bool:
    """그 금액이 납입액이 아니라 계좌 잔고를 가리키는가.

    "IRP **평가액**이 3억원"        → True  (잔고)
    "IRP에 **적립금** 5000만원 있는데" → True  (잔고)
    "IRP에 900만원 **넣으면**"       → False (납입액)

    ━━ 왜 필요한가 ━━
    잔고를 납입액으로 읽으면 세액공제를 3억원 납입 기준으로 계산한다.
    연 납입한도가 1,800만원이므로 애초에 불가능한 전제다.
    """
    q = question or ""
    for start, end, v in parse_amount_expressions(q):
        if v != value:
            continue
        before = q[max(0, start - 12):start]
        if any(n in before for n in _BALANCE_NOUN):
            return True
        if _BALANCE_VERB.search(q[end:end + 8]):
            return True
    return False


# 이 명사가 금액 앞뒤에 붙으면 연금 관련 계좌·납입·퇴직급여가 **아니라**
# 그냥 일반 자산(현금·예적금)이다. 연금 계좌에 들어있지 않다는 뜻이므로
# account_value_manwon 등 연금 관련 슬롯으로 넘기면 안 된다.
# ⚠️ "3000만원 현금있고"처럼 한국어는 이 명사가 금액 **뒤**에 붙는 경우가
#    흔하다 — _is_income_amount(명사가 항상 금액 앞)와 달리 양방향으로 봐야 한다.
_NON_PENSION_ASSET_NOUN = ("현금", "예금", "적금", "저축", "주택청약", "청약")


def _is_non_pension_asset_amount(question: str, value: float) -> bool:
    """그 금액이 연금 계좌가 아니라 일반 현금·예적금을 가리키는가.

    ━━ 왜 필요한가 (UI-017, 2026-09-06) ━━
    "24살에 3000만원 현금있고 500만원 주택청약있는데 노후대비 어떻게
    해야할까"에서 실서버의 HyperCLOVA X(L1)가 "3000만원 현금"을
    account_value_manwon(연금계좌 평가액)으로 잘못 라벨링해 규칙 기반
    추출(derive_conditions의 rule 단계)이 걸러내는 값을 **LLM 조건 병합
    루프가 그대로 받아들였다.** 그 결과 계산 조건이 있는 것으로 오판돼
    ADVISORY(불특정 개인 서술)로 가야 할 질의가 GENERAL로 잘못 라우팅됐고,
    대응하는 계산이 없어 "제공 자료로 확정하기 어렵습니다"로 무너졌다.

    규칙 기반 경로는 애초에 '계좌에'·'평가액' 같은 키워드 없이는
    account_value_manwon을 만들지 않으므로 안전하다(이 함수가 없어도
    로컬 재현에서 확인됨). 이 함수는 **LLM이 준 값에도 같은 수준의
    의심을 적용**하기 위한 것이다 — 규칙 경로의 _is_income_amount와
    같은 계열.
    """
    q = question or ""
    for start, end, v in parse_amount_expressions(q):
        if v != value:
            continue
        before = q[max(0, start - 12):start]
        after = q[end:end + 8]
        if any(n in before for n in _NON_PENSION_ASSET_NOUN):
            return True
        if any(n in after for n in _NON_PENSION_ASSET_NOUN):
            return True
    return False


# LLM이 이 키들에 값을 줬다면, 그 금액이 실제로는 연금 계좌 밖 일반 자산
# (현금·예적금)일 가능성을 의심해야 한다 — 규칙 경로는 '계좌에'·'평가액' 같은
# 전용 키워드 없이는 이 키들을 만들지 않으므로 안전하지만, LLM은 그런 검증
# 없이 자유롭게 라벨링할 수 있기 때문이다.
#
# ⚠️ routing._CALC_CONDITION_KEYS의 "_manwon" 접미사 키 **전부**를 담는다.
#    처음엔 account_value_manwon 등 5개만 막았는데(F28), "주택청약 500만원"이
#    private_pension_annual_manwon(연간 연금수령액)으로 오분류되는 사고가
#    재현됐다(UI-014, 2026-09-06) — 같은 결함이 안 막은 키에서 그대로
#    반복됐다. 이 세트는 GENERAL 라우팅을 유발하는 "계산 조건" 키와
#    정확히 같아야 한다 — 하나라도 빠지면 그 키가 다음 사고 지점이 된다.
_GUARDED_MONEY_KEYS = frozenset({
    "account_value_manwon", "severance_manwon", "pension_saving_manwon",
    "irp_manwon", "combined_contribution_manwon",
    "private_pension_annual_manwon", "private_pension_monthly_manwon",
    "total_income_manwon",
})


# 이 명사 바로 뒤에 붙은 금액은 **소득**이지 납입액이 아니다.
_INCOME_NOUN = ("총급여", "연봉", "종합소득", "근로소득", "소득금액", "급여가", "연소득")


def _is_income_amount(question: str, value: float) -> bool:
    """그 금액이 납입액이 아니라 소득을 가리키는가.

    ━━ 왜 필요한가 (2026-09-01 실측) ━━
    "연금저축+IRP 세액공제, 총급여 8000만원이면 얼마까지?" 처럼 **납입액을
    말하지 않고 소득만 말한 질의**에서, `_find_amount_near`가 '연금저축'과
    'IRP' 양쪽에 대해 질의의 유일한 금액인 8,000만원을 집어 왔다. 그 결과

        pension_saving_manwon=8000 · irp_manwon=8000 · total_income_manwon=8000

    이 되어, 납입액이 연 한도(1,800만원)를 넘는 불가능한 전제가 만들어졌다.
    계산은 "조건 부족"으로 실패했고, **소득 구간에 따른 공제율(13.2%)이
    산출되지 못했다.** 사용자에게는 근거 문서가 잘린 채 인용돼 "16.5%"로
    보였다 — 확정 법령 수치를 틀리는 것으로 드러난 결함의 실제 원인이다.

    잔고 가드(`_is_balance_amount`)와 정확히 같은 계열의 문제다. 잔고는
    막아 뒀는데 소득은 막혀 있지 않았다.

    "총급여 8000만원인데 IRP에 900만원 넣으면"처럼 납입액이 따로 있으면
    `_find_amount_near`가 더 가까운 900을 고르므로 이 가드는 발동하지 않는다.
    """
    q = question or ""
    for start, _end, v in parse_amount_expressions(q):
        if v != value:
            continue
        before = q[max(0, start - 12):start]
        if any(n in before for n in _INCOME_NOUN):
            return True
    return False


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


_RANGE_YEARS = re.compile(r'(\d{1,2})\s*[~∼\-–]\s*(\d{1,2})\s*년\s*차?')
_FIRST_SALARY = ("초봉", "첫 연봉", "첫연봉", "입사 시", "입사시", "시작")
_NOW_SALARY = ("지금은", "지금", "현재는", "현재", "올해")


def _salary_schedule(q: str, service_years: Optional[float]) -> Optional[list]:
    """DC형 적립액 산식의 입력 — [(근속연차, 연봉만원), ...] 를 만든다.

    ━━ 두 형태만 결정론적으로 다룬다 ━━
    (a) 구간 명시 — "1~5년차는 5천만원, 6~10년차는 8천만원"
        'N~M년차'는 구간 [N-1, M]을 뜻한다(1년차 = 입사 후 첫 해 = [0,1]).
        구간 안에서는 연봉이 일정하므로 양 끝점을 모두 넣어 계단을 만든다.
    (b) 초봉·현재 연봉 — "초봉 5000만원으로 시작해서 지금은 1억 1000만원"
        + 근속연수. 두 점만 알므로 **그 사이는 선형 변화로 가정**하고,
        그 사실을 condition_notes로 고지한다(계단식 인상이면 오차가 난다).

    ⚠️ 이 밖의 서술은 만들지 않는다. 추측으로 구간을 지어내면 그 순간
       "계산은 함수" 원칙이 무너진다 — 모르면 되묻는 편이 맞다.
    """
    text = q or ""
    exprs = parse_amount_expressions(text)
    if not exprs:
        return None

    # ── (a) 구간 명시 ──
    segments: list[tuple[float, float, float]] = []      # (시작, 끝, 연봉)
    for m in _RANGE_YEARS.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < 1 or hi < lo:
            continue
        after = [(s, v) for s, _e, v in exprs if s >= m.end()]
        if not after:
            continue
        start, value = min(after, key=lambda p: p[0])
        if start - m.end() > _NEAR_WINDOW:
            continue
        segments.append((lo - 1, float(hi), value))
    if len(segments) >= 2:
        segments.sort(key=lambda s: s[0])
        pts: list[tuple[float, float]] = []
        for lo, hi, value in segments:
            pts.append((lo, value))
            pts.append((hi, value))
        return pts

    # ── (b) 초봉 + 현재 연봉 + 근속연수 ──
    if not service_years or service_years <= 0:
        return None
    first = _find_amount_near(text, _FIRST_SALARY)
    now = _find_amount_near(text, _NOW_SALARY)
    if first is None or now is None or first == now:
        return None
    return [(0.0, first), (float(service_years), now)]


# _manwon 접미사가 없지만 숫자여야 하는 필드. LLM 출력에서 여기 해당하는
# 키의 값이 숫자로 안 바뀌면 조용히 버린다(위조할 수 없으므로).
_NUMERIC_CONDITION_KEYS = {"age", "pension_year", "actual_receipt_year",
                          "service_years", "years_elapsed"}

# 이 키들이 벗어나면 안 되는 범위. calc_params.py의 _VALID와 같은 수치를
# 쓴다 — 계산 인자에 쓰일 때만 걸러지고, 이 조건이 그대로 사람에게
# 보여주는 문장("조건으로 이해했습니다")이나 L4-sub 페이로드에 실릴 때는
# 아무 검증도 없었다. 실제로 "연간 연금수령액 2,000만원"을 HCX가
# pension_year=2000으로 잘못 채운 값이 계산에는 안 들어갔지만(이 답변은
# pension_year를 쓰지 않는 계산이었다) 사용자에게 그대로 노출됐다
# (2026-08-29 실측 — "연금수령연차 2000.0"). _unit_confusion은 `_manwon`
# 접미사가 있는 금액 필드만 보므로 이 키들에는 전혀 적용되지 않는다.
_NUMERIC_CONDITION_BOUNDS: dict[str, tuple[float, float]] = {
    "age": (1, 120),
    "pension_year": (1, 60),
    "actual_receipt_year": (1, 60),
    "service_years": (1, 60),
    "years_elapsed": (0, 60),
}


# 단위 혼동으로 볼 배수. 만원↔억, 만원↔원은 전부 10,000배 차이다.
# 100배를 문턱으로 두면 그 사고는 잡히고, 같은 자릿수 안의 정당한 이견
# (규칙이 600을 읽고 L1이 900을 읽는 등)은 건드리지 않는다.
_UNIT_CONFUSION_RATIO = 100.0


def _unit_confusion(key: str, rule_value, llm_value,
                    text_ceiling: Optional[float] = None) -> bool:
    """L1이 준 금액이 단위를 잘못 잡은 것으로 보이는가.

    ━━ 왜 필요한가 ━━
    계산함수는 전부 **만원 단위**다(CLAUDE.md). 그런데 L1은 "1억 원"을
    만원으로 환산해 10000을 줘야 할 자리에 1(억 단위)이나 100000000(원 단위)을
    주기도 한다. 예전에는 그 값이 규칙 파싱 결과를 **무조건 덮어썼고**,
    1만배 오차가 그대로 계산에 들어가 "연금수령한도 0만원" 같은
    **확신에 찬 오답**이 나갔다.

    규칙 파싱은 사용자가 쓴 글자("1억 원")를 명시적 단위 변환으로 읽은
    것이므로, 자릿수가 크게 어긋나면 규칙 쪽을 믿는다. 같은 자릿수 안의
    차이는 L1이 문맥을 더 잘 봤을 수 있으므로 그대로 둔다.

    ━━ text_ceiling — 같은 키에 규칙값이 없을 때 ━━
    규칙이 이 키를 아예 못 채운 경우(예: "연금계좌에 900만원"처럼 연금저축/
    IRP 구분이 없어 combined_contribution_manwon만 채워지고
    pension_saving_manwon은 비는 경우), 비교 대상이 없어 가드가 통째로
    무력화됐다. 실제로 L1이 900을 9,000,000으로 잘못 준 값이 그대로 통과해,
    900만원 납입인데 "연간 납입한도(1,800만원) 초과"로 잘못 표시됐다.

    질의 원문에 실제로 등장한 금액 중 **가장 큰 값**을 대신 천장으로 쓴다.
    L1은 추출기이지 계산기가 아니므로, 답이 어떤 _manwon이든 원문에 쓰인
    숫자 중 하나에서 나왔어야 한다. 원문 최대값보다 100배 이상 크면
    거의 확실히 원·만원 단위 혼동이다.
    """
    if not key.endswith("_manwon"):
        return False
    baseline = rule_value
    if not isinstance(baseline, (int, float)) or baseline <= 0:
        baseline = text_ceiling
    if not isinstance(baseline, (int, float)) or baseline <= 0:
        return False        # 비교할 게 아무것도 없으면 L1 값이 유일한 정보다
    if llm_value <= 0:
        return True         # 금액이 0 이하일 수는 없다
    ratio = max(baseline, llm_value) / min(baseline, llm_value)
    return ratio >= _UNIT_CONFUSION_RATIO


def _fmt(v) -> str:
    """조건 노트에 쓸 짧은 수치 표기."""
    f = float(v)
    return f"{int(f):,}" if f.is_integer() else f"{f:,.4g}"


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
    # ⚠️ 잔고를 납입액으로 읽으면 안 된다. "IRP 평가액이 3억원인데 세액공제는?"
    #    에서 3억을 납입액으로 잡으면 연 납입한도(1,800만원)로는 불가능한
    #    전제로 세액공제를 계산한다.
    saving = _find_amount_near(q, ("연금저축", "연저축"))
    if saving is not None and (_is_balance_amount(q, saving)
                               or _is_income_amount(q, saving)):
        saving = None
    irp = _find_amount_near(q, ("IRP", "irp", "개인형"))
    if irp is not None and (_is_balance_amount(q, irp)
                            or _is_income_amount(q, irp)):
        irp = None
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
    sev = _find_amount_near(q, ("퇴직금", "퇴직급여", "명예퇴직", "명퇴"))
    if sev is not None and (_is_balance_amount(q, sev)
                            or _is_income_amount(q, sev)):
        sev = None
    if sev is not None:
        c["severance_manwon"] = sev
    # DB형 퇴직급여 산식의 입력 — 퇴직 직전 3개월 평균월급.
    # ⚠️ '연봉'과 섞이면 12배 오차가 나므로 소득 가드를 그대로 건다.
    wage = _find_amount_near(q, ("평균월급", "평균 월급", "평균임금", "평균 임금",
                                 "월급", "월 급여", "월급여"))
    if wage is not None and not _is_income_amount(q, wage):
        c["avg_monthly_wage_manwon"] = wage
    # DC형 적립액 산식의 입력 — 연차별 연봉 구간
    if (sched := _salary_schedule(q, c.get("service_years"))) is not None:
        c["salary_schedule"] = sched
        if len(sched) == 2:
            c.setdefault("condition_notes", []).append(
                "연차별 연봉 변화를 구간별로 확인하지 못해 입사 시점과 현재 "
                "연봉 사이를 선형 증가로 가정해 계산했습니다. 실제 인상이 "
                "계단식이면 결과가 달라질 수 있습니다")
    # ⚠️ '계좌에'는 잔고 표지가 아니라 **위치 표지**다. "연금계좌에 900만원
    #    납입하면"의 900은 평가액이 아니라 납입액인데, 예전에는 평가액으로
    #    읽어 세액공제 계산이 통째로 빗나갔다(300건 감사 A03).
    #    금액 바로 뒤에 납입 동사가 오면 잔고로 보지 않는다.
    if (av := _find_amount_near(q, ("평가액", "적립금", "잔고", "계좌에",
                                    "쌓여", "모았"))) is not None:
        if not _followed_by_contribution(q, av):
            c["account_value_manwon"] = av

    # "연금계좌에 900만원 납입" — 연금계좌는 연금저축과 퇴직연금을 아우르는
    # 상위 개념이라 어느 쪽에도 안 걸린다. 구분이 없으므로 합산으로 본다
    # (연금저축·IRP 합산이 확인되지 않을 때와 같은 처리 · 가정은 고지한다).
    if not any(k in c for k in ("pension_saving_manwon", "irp_manwon",
                                "combined_contribution_manwon")):
        pc = _find_amount_near(q, ("연금계좌", "연금 계좌"))
        if pc is not None and _followed_by_contribution(q, pc):
            c["combined_contribution_manwon"] = pc
            c.setdefault("condition_notes", []).append(
                "연금저축과 IRP의 납입액 구분이 확인되지 않아 합산액 기준으로 계산했습니다. "
                "연금저축 단독 납입액이 600만원을 넘으면 결과가 달라질 수 있습니다")
    if (inc := _find_amount_near(q, ("총급여", "연봉", "종합소득"))) is not None:
        c["total_income_manwon"] = inc
        c["income_type"] = "종합소득" if "종합소득" in q else "총급여"

    # 월/연 단위 연금 수령액
    if (m := re.search(r'(?:매달|매월|월)\s*([^\s,]{1,12})\s*(?:씩|정도)?\s*(?:받|수령|나오)', q)):
        if (v := parse_amount_to_manwon(m.group(1))) is not None:
            c["private_pension_monthly_manwon"] = v
    # ⚠️ '연간'만 받으면 안 된다. "연 1200만원 받으면"처럼 '연간'의 축약형인
    #    맨 '연'이 훨씬 흔한데, 그동안 이 규칙에 없어서 원천징수 계산에
    #    쓸 금액이 통째로 안 잡혔다(실측 감사 L10·L11·L12·L13·L19 5건 전부
    #    "월 수령액이 확인되지 않아 세율만 안내합니다"로 계산이 비었다).
    #    '연'을 목록 맨 뒤에 둔다 — '연간'이 먼저 매칭되게 순서를 지킨다.
    #    '국민연금'·'연령' 같은 복합어는 연 뒤에 공백이 없어 오매칭되지 않는다.
    if (m := re.search(r'(?:연간|1년에|해마다|매년|연)\s*([^\s,]{1,12})\s*(?:씩|정도)?\s*(?:받|수령|나오)', q)):
        if (v := parse_amount_to_manwon(m.group(1))) is not None:
            c["private_pension_annual_manwon"] = v
    # ⚠️ 위 정규식은 시간 표지 **바로 다음 토큰**만 금액으로 본다. 그래서
    #    "연간 연금수령액 2,000만원 받는데"처럼 사이에 명사가 끼면 '연금'을
    #    캡처하고 실패한다(2026-08-29 실측 — 계산이 통째로 안 돌았다).
    #    _find_amount_near는 키워드와 금액이 떨어져 있는 경우를 다루라고
    #    이미 있는 도구이므로 그대로 재사용한다. 위에서 못 잡았을 때만
    #    보조로 돌려, 기존에 맞던 케이스의 해석은 건드리지 않는다.
    if "private_pension_annual_manwon" not in c:
        if (v := _find_amount_near(q, ("연간 연금수령액", "연간 수령액",
                                       "연 연금수령액"))) is not None:
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
    #
    # ⚠️ 숫자여야 할 필드는 반드시 검증한다. 실사고: 프롬프트 탈취 질의에서
    #    L1이 내놓은 JSON에 만원 필드 값으로 "**" 같은 비정상 문자열이
    #    섞여 들어왔다. 검증 없이 그대로 저장했더니 훨씬 나중에
    #    format_manwon()이 float()으로 변환하다 죽었고, 그 예외가 위로
    #    뚫고 나가 요청 전체가 실패했다. LLM 출력은 신뢰할 수 없는 입력이므로
    #    경계에서 막아야 한다 — 원거리에서 죽으면 원인을 찾기도 어렵다.
    # 원문에 실제로 쓰인 금액 중 최댓값. 같은 키에 규칙값이 없을 때
    # _unit_confusion의 비교 천장으로 쓴다.
    _text_amounts = [v for _, _, v in parse_amount_expressions(q)]
    _text_ceiling = max(_text_amounts) if _text_amounts else None

    for k, v in (llm_conditions or {}).items():
        if v in (None, "", []):
            continue
        if k.endswith("_won"):
            try:
                c[k[:-4] + "_manwon"] = round(won_to_manwon(float(v)), 4)
            except (TypeError, ValueError):
                continue
        elif k.endswith("_manwon") or k in _NUMERIC_CONDITION_KEYS:
            if isinstance(v, bool):
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue    # 숫자가 아니면 조용히 버린다 — 지어낼 수 없다
            bounds = _NUMERIC_CONDITION_BOUNDS.get(k)
            if bounds is not None and not (bounds[0] <= val <= bounds[1]):
                # 금액과 달리 "그럴듯한 원인"을 추정할 수 없다(원↔만원
                # 환산 같은 정정 규칙이 없다) — 있을 수 없는 값이므로
                # 그대로 버리고, 규칙 파싱 값이 있으면 그걸 지킨다.
                if k not in c:
                    c.setdefault("diagnostic_notes", []).append(
                        f"{k}={_fmt(val)}로 분석됐으나 있을 수 없는 값이라 "
                        f"반영하지 않았습니다")
                continue
            if k in _GUARDED_MONEY_KEYS and _is_non_pension_asset_amount(q, val):
                # LLM이 "3000만원 현금"·"주택청약 500만원"을 account_value_manwon
                # 등으로 잘못 라벨링한 경우 — 근거 없는 라벨을 받아들이지 않는다.
                c.setdefault("diagnostic_notes", []).append(
                    f"{_fmt(val)}만원이 현금·예적금·주택청약 등 연금과 무관한 "
                    f"자산으로 언급되어 조건({k})으로 반영하지 않았습니다")
                continue
            if _unit_confusion(k, c.get(k), val, _text_ceiling):
                # 규칙이 읽은 값이 있으면 그 값을 지키고, 없으면(천장값만으로
                # 걸린 경우) 아예 버린다 — 지어낼 근거가 없다.
                if k in c:
                    c.setdefault("diagnostic_notes", []).append(
                        f"질의에서 읽은 금액({_fmt(c[k])}만원)과 분석 결과"
                        f"({_fmt(val)}만원)의 자릿수가 크게 달라, 질의 원문에서 읽은 "
                        f"값으로 계산했습니다")
                else:
                    c.setdefault("diagnostic_notes", []).append(
                        f"분석 결과({_fmt(val)}만원)가 질의 원문의 금액과 자릿수가 "
                        f"크게 달라 반영하지 않았습니다")
                continue
            c[k] = val
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
            # LLM 조건 경로는 age를 검증 과정에서 float으로 통일한다
            # (_NUMERIC_CONDITION_KEYS). 정수로 떨어지면 정수로 보여준다 —
            # "80.0세"는 사람이 쓰는 표현이 아니다.
            age_v = int(v) if float(v).is_integer() else v
            parts.append(f"{label[k]} {age_v}세")
        elif k in _NUMERIC_CONDITION_KEYS:
            # age와 같은 이유 — pension_year 등도 LLM 경로에서 float으로
            # 통일되므로, 정수로 떨어지면 정수로 보여준다. 실제로 "연금수령연차
            # 5.0"처럼 소수점이 그대로 노출된 적이 있다(2026-08-29 실측).
            num_v = int(v) if float(v).is_integer() else v
            parts.append(f"{label[k]} {num_v}")
        elif isinstance(v, bool):
            if v:
                parts.append(label[k])
        else:
            parts.append(f"{label[k]} {v}")
    return ", ".join(parts)
