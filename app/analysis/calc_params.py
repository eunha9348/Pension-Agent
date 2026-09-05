"""calc_params_builder — 슬롯/조건 → 계산함수 인자 조립.

━━ 이 파일이 가장 시급했던 이유 ━━
`run_calculations()`는 CALC_PENDING 슬롯마다 `calc_params_builder(slot)`을
불러 인자를 받는다. 이게 없으면 **계산함수 15종이 하나도 호출되지 않는다.**

━━ 세 가지 원칙 ━━
1. **모르면 지어내지 않는다.** 필수 인자가 없으면 슬롯을 MISSING으로
   강등하고 ASK_BACK 문구를 남긴다(확인 항목 최대 2건은 상위에서 제한).
2. **모르지만 경우의 수가 적으면 나눠서 계산한다.** 예를 들어 소득 구간을
   모르면 13.2%/16.5% 두 경우를 다 계산해 "A 상황이면 ~, B 상황이면 ~"
   형태의 조건부 답변 재료를 만든다 (`__variants__`).
3. **가정을 쓸 때는 반드시 기록한다.** default를 쓴 인자는 assumptions에
   남고, 답변의 [한계 고지]에 그대로 올라간다.

━━ 단위 ━━
계산함수는 전부 만원 단위. conditions의 `*_manwon` 키를 그대로 넘긴다.
원 단위 변환은 units.py 경계에서 이미 끝나 있어야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.coverage_pipeline import RequirementSlot

# run_calculations()가 이 키를 보고 조건 분기 계산을 수행한다.
VARIANTS_KEY = "__variants__"


class MissingCalcParams(Exception):
    """필수 인자 부족 — 계산을 포기하고 슬롯을 MISSING으로 강등시킨다.

    run_calculations()는 이 예외의 `ask_back` 속성을 읽어 확인 문구를 남긴다.
    """

    def __init__(self, function: str, missing: list[str], ask_back: list[str]):
        self.function = function
        self.missing_params = missing
        self.ask_back = ask_back
        super().__init__(
            f"'{function}' 계산에 필요한 조건 부족: {', '.join(missing)}")


# 함수별 "최소 하나는 있어야 계산이 의미 있는" 조건 키.
#
# ⚠️ 왜 필요한가: 세액공제 계산은 두 납입액이 모두 없으면 기본값 0으로 굴러가
#    "세액공제액 = 0만원"이라는 **맞지만 쓸모없고 오해를 부르는** 답을 낸다.
#    납입액을 아예 안 밝힌 질문은 대개 '한도가 얼마냐'를 묻는 것이므로,
#    계산을 접고 근거 문서의 한도를 설명하거나 되물어야 한다.
# ⚠️ 지금은 비어 있다. 세액공제 계산이 여기 있었으나 걷어냈다 —
#    막는 대신 **계산함수가 스스로 한도만 반환**하도록 고쳤기 때문이다.
#    막아 두면 "얼마까지 받을 수 있나요"(한도 질의)에 600·900이 답변에
#    실릴 보장이 사라진다(평가 E-01). 한도는 납입액과 무관한 상수이므로
#    계산해서 내보내는 것이 맞고, 의미 없는 세액공제액만 빼면 된다.
#
#    새 항목을 추가하기 전에 먼저 물을 것: 인자가 없을 때 **정말 아무것도
#    낼 수 없는가**, 아니면 일부만 내면 되는가. 후자라면 여기가 아니라
#    계산함수에서 부분 반환으로 푸는 것이 옳다.
REQUIRE_ANY: dict[str, tuple[tuple[str, ...], str]] = {}


# ── 인자 값 범위 검증 ──────────────────────────────────────────
#
# ⚠️ 왜 필요한가: 계산함수는 순수함수라 값을 검증하지 않는다. 그래서
#    "연금수령 0년차"(존재하지 않는 연차)나 음수 나이가 그대로 들어가
#    그럴듯한 숫자를 뱉는다. 실제로 0년차 → 한도 1,090만원, 나이 -5세 →
#    기타소득세 16.5%가 계산됐다. **틀린 숫자를 내느니 되묻는 게 낫다.**
#
#    검증은 계산함수(검증 완료 모듈)가 아니라 이 경계층에서 한다.
_VALID: dict[str, tuple] = {
    # 인자명: (하한, 상한, 되물을 문구)
    "pension_year": (1, 60, "연금수령연차 (연금개시 가능한 해가 1년차입니다)"),
    "actual_receipt_year": (1, 60, "연금실제수령연차 (실제로 인출한 해 기준)"),
    "years_elapsed": (0, 60, "연금개시 가능 시점 이후 경과 연수"),
    "Age": (1, 120, "연금 수령 시점의 나이"),
    "service_years": (1, 60, "근속연수"),
    # ── 금액 인자 — 전부 **만원 단위**다 ────────────────────────
    #
    # ⚠️ 상한이 곧 **원 단위 유입 차단선**이다. 예전에는 전부 None(무한대)
    #    이라, 600만원을 원으로 쓴 6,000,000이 그대로 통과해 **60조원**으로
    #    계산됐다. CLAUDE.md가 경고한 "혼동 시 1만배 오차"가 정확히 이 자리다.
    #
    #    상한은 "만원 단위로 읽었을 때 현실적인 최대"로 잡는다. 원 단위로
    #    들어온 값은 이 상한을 반드시 넘으므로 걸러진다:
    #      600만원을 원으로 → 6,000,000 > 상한 1,800  ✓ 차단
    #      1억원을 원으로   → 100,000,000 > 상한 1,000,000  ✓ 차단
    #
    #    걸리면 값이 없는 것과 똑같이 취급해 되묻는다 — 틀린 숫자를 내는
    #    것보다 물어보는 편이 낫다.
    "account_value": (0, 1_000_000, "연금계좌 평가액 (만원 단위로 알려주세요)"),
    "severance_pay": (0, 1_000_000, "퇴직급여 총액 (만원 단위로 알려주세요)"),
    # 납입액은 연간 총 납입한도 1,800만원을 넘을 수 없다
    "X_pension_saving": (0, 1_800, "연금저축 납입액 (연 1,800만원 한도)"),
    "Y_irp_personal": (0, 1_800, "IRP 개인부담금 (연 1,800만원 한도)"),
    "P_private_monthly": (0, 100_000, "월 연금수령액 (만원 단위로 알려주세요)"),
    "P_private_pension_annual": (0, 1_000_000,
                                 "연간 사적연금 수령액 (만원 단위로 알려주세요)"),
    "P_np_annual": (0, 100_000, "국민연금 연 수령액 (만원 단위로 알려주세요)"),
    "I_monthly": (0, 100_000, "월 기준소득월액 (만원 단위로 알려주세요)"),
    "I_final_monthly": (0, 100_000,
                        "가입기간 평균 기준소득월액 (만원 단위로 알려주세요)"),
    "rate": (0, 1, "세율 (0~100% 범위)"),
    "r_tax_credit": (0, 1, "세액공제율"),
    "r_irr": (0, 1, "소득대체율"),
    "N": (0, 20, "전체 자녀 수"),
    "N0": (0, 20, "2007.12.31 이전 출생 자녀 수"),
    "N1": (0, 20, "2008.1.1~2025.12.31 출생 자녀 수"),
    "N2": (0, 20, "2026.1.1 이후 출생 자녀 수"),
}


def validate_param(name: str, value: Any) -> Optional[str]:
    """범위를 벗어나면 되물을 문구를 반환. 정상이면 None."""
    rule = _VALID.get(name)
    if rule is None or value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    low, high, ask = rule
    if low is not None and value < low:
        return ask
    if high is not None and value > high:
        return ask
    return None


@dataclass
class ParamSpec:
    """계산함수 인자 하나의 조달 방법."""
    name: str
    resolve: Callable[[dict], Any]
    required: bool = True
    default: Any = None
    assumption: str = ""                         # default 사용 시 기록할 문구
    ask_back: str = ""                           # 값이 없을 때 되물을 문구
    variants: Optional[list[tuple[str, Any]]] = None   # 미상 시 조건 분기


def _get(key: str) -> Callable[[dict], Any]:
    return lambda c: c.get(key)


def _first(*keys: str) -> Callable[[dict], Any]:
    def _r(c: dict):
        for k in keys:
            if c.get(k) is not None:
                return c[k]
        return None
    return _r


def _tax_credit_rate(c: dict) -> Optional[float]:
    """세액공제율 — 소득 구간으로 결정.

    현행 기준: 총급여 5,500만원(종합소득 4,500만원) 이하 16.5%, 초과 13.2%.
    (pension_calc_functions.CURRENT_TAX_VALUES와 같은 기준)
    소득을 모르면 None → variants로 두 경우를 모두 계산한다.
    """
    income = c.get("total_income_manwon")
    if income is None:
        return None
    threshold = 4500 if c.get("income_type") == "종합소득" else 5500
    return 0.165 if income <= threshold else 0.132


_RATE_VARIANTS = [
    ("총급여 5,500만원 이하(종합소득 4,500만원 이하)", 0.165),
    ("총급여 5,500만원 초과(종합소득 4,500만원 초과)", 0.132),
]

_ANNUITY_VARIANTS = [
    ("확정기간형으로 수령하는 경우", False),
    ("종신형으로 수령하는 경우", True),
]

_JOIN_VARIANTS = [
    ("2013.3.1 이후 가입 계좌인 경우", False),
    ("2013.3.1 이전 가입 계좌인 경우(6년차 기산 특례)", True),
]

_LOCAL_TAX_VARIANTS = [
    ("지방소득세 포함 기준으로 볼 때", True),
    ("국세 기준으로 볼 때", False),
]


# ════════════════════════════════════════════════════════════════
# 함수별 인자 규격
# ════════════════════════════════════════════════════════════════
#
# ⚠️ '과세방식_판정_계산'은 DEPRECATED라 여기 없다 (아래 _REMAP 참고).
#    compare_taxation_options 주석이 3가지 오류를 명시하고 있어
#    답변 생성 경로에서 호출되면 안 된다.

CALC_PARAM_SPECS: dict[str, list[ParamSpec]] = {

    # ── 국민연금 (외부 기본제도 자료 근거) ─────────────────────
    "국민연금_본인부담금_계산": [
        ParamSpec("I_monthly", _first("monthly_income_manwon"),
                  ask_back="월 기준소득월액(월 급여)"),
        ParamSpec("r_np_premium", _get("np_premium_rate"), required=False,
                  default=0.09, assumption="국민연금 보험료율은 현행 9%로 계산"),
    ],
    "출산크레딧_인정개월_계산": [
        ParamSpec("N", _get("children_total"), ask_back="전체 자녀 수"),
        ParamSpec("N0", _get("children_before_2008"),
                  ask_back="2007.12.31 이전 출생 자녀 수"),
        ParamSpec("N1", _get("children_2008_2025"),
                  ask_back="2008.1.1~2025.12.31 출생 자녀 수"),
        ParamSpec("N2", _get("children_after_2026"),
                  ask_back="2026.1.1 이후 출생 자녀 수"),
    ],
    "국민연금_수령액_계산": [
        ParamSpec("I_final_monthly", _first("monthly_income_manwon"),
                  ask_back="가입기간 평균 기준소득월액"),
        # 소득대체율은 가입기간·전체가입자평균소득에 따라 달라져 임의 가정 불가
        ParamSpec("r_irr", _get("income_replacement_rate"),
                  ask_back="적용 소득대체율(가입기간에 따라 달라집니다)"),
    ],

    # ── 사적연금 납입·수령 ─────────────────────────────────────
    "사적연금_납입한도_세액공제_계산": [
        # ⚠️ default를 0.0이 아니라 None으로 둔다. 계산함수가 '0원을 넣었다'와
        #    '납입액을 모른다'를 구분해야 한다 — 후자에 0을 넘기면
        #    "세액공제액 0만원"이라는 오해를 부르는 답이 나간다.
        ParamSpec("X_pension_saving", _get("pension_saving_manwon"),
                  required=False, default=None,
                  assumption="연금저축 납입액이 확인되지 않아 한도만 안내"),
        # 합산액만 확인된 경우("합쳐서 900만원")도 여기로 들어온다.
        # 합산 기준으로 계산했다는 사실은 conditions["condition_notes"]에 기록돼
        # 답변의 [한계 고지]로 올라간다.
        ParamSpec("Y_irp_personal", _first("irp_manwon",
                                           "combined_contribution_manwon"),
                  required=False, default=None,
                  assumption="IRP 개인부담금이 확인되지 않아 한도만 안내"),
        ParamSpec("r_tax_credit", _tax_credit_rate, variants=_RATE_VARIANTS,
                  ask_back="총급여(또는 종합소득) 구간"),
    ],
    "사적연금_원천징수_계산": [
        # ⚠️ 금액은 **세액**에만 필요하다. 세율(r_withholding)은 나이와
        #    수령형태만으로 정해진다. 예전에는 이 인자가 필수라서
        #    "만 80세인데 몇 퍼센트 떼나요"처럼 **요율만 묻는 질의**에도
        #    금액을 요구하며 계산을 통째로 접었고, 답변에서 3.3%가 통째로
        #    빠졌다(평가 E-14). 산출 가능한 것은 내주고, 금액이 필요한
        #    부분만 확인 요청으로 돌린다.
        # ⚠️ default를 0.0이 아니라 None으로 둔다. '0원을 받는다'와 '수령액을
        #    모른다'는 다르다 — 0을 넘기면 "원천징수세액 0만원"이라는
        #    맞지만 오해를 부르는 답이 나간다(300건 감사 지적).
        ParamSpec("P_private_monthly", _get("private_pension_monthly_manwon"),
                  required=False, default=None,
                  assumption="월 연금수령액을 알려주지 않으셔서 세율만 계산했습니다. "
                             "실제 원천징수 세액은 수령액에 따라 달라집니다",
                  ask_back="월 연금수령액"),
        # 연령별 차등과세(5.5/4.4/3.3%)의 기준이라 임의 가정 불가
        ParamSpec("Age", _get("age"), ask_back="연금 수령 시점의 나이"),
        ParamSpec("IsAnnuityType", _get("is_annuity_type"),
                  variants=_ANNUITY_VARIANTS,
                  ask_back="종신형 수령인지 확정기간형 수령인지"),
    ],

    # ── 과세방식 비교 ──────────────────────────────────────────
    "과세방식_비교_계산": [
        ParamSpec("P_np_annual", _get("np_annual_manwon"), required=False,
                  default=0.0,
                  assumption="국민연금 등 다른 연금소득은 없는 것으로 계산"),
        ParamSpec("P_private_pension_annual",
                  _get("private_pension_annual_manwon"),
                  ask_back="연간 사적연금 수령액(연금저축+IRP 합계)"),
        ParamSpec("other_comprehensive_income",
                  _get("other_income_manwon"), required=False, default=0.0,
                  assumption="연금 외 종합소득은 없는 것으로 계산"),
        ParamSpec("include_local_tax", _get("includes_local_tax"),
                  required=False, default=True,
                  assumption="세액은 지방소득세를 포함한 기준으로 표기"),
    ],

    # ── 연금수령한도 · 연차 ────────────────────────────────────
    "연금수령한도_계산": [
        ParamSpec("account_value", _first("account_value_manwon", "amount_manwon"),
                  ask_back="연금계좌 평가액(매년 1월 1일 또는 연금개시일 기준)"),
        ParamSpec("pension_year", _get("pension_year"),
                  ask_back="연금수령연차(연금개시 가능한 해가 1년차)"),
    ],
    "연금수령연차_계산": [
        ParamSpec("join_date_before_2013_03_01", _get("join_before_2013"),
                  variants=_JOIN_VARIANTS,
                  ask_back="계좌를 2013년 3월 1일 이전에 가입했는지"),
        ParamSpec("years_elapsed", _get("years_elapsed"),
                  ask_back="연금개시 가능 시점 이후 경과한 연수"),
    ],
    "퇴직소득세_감면율_계산": [
        # ⚠️ 함정 B1 — 여기에 pension_year(연금수령연차)를 넣으면 안 된다.
        #    감면율을 결정하는 건 '실제로 인출한 해'만 쌓이는
        #    연금실제수령연차다(doc40). 두 값을 절대 대체하지 않는다.
        ParamSpec("actual_receipt_year", _get("actual_receipt_year"),
                  ask_back="연금실제수령연차 — 연금을 개시한 뒤 "
                           "실제로 인출한 해가 몇 번째인지"),
    ],

    # ── 퇴직소득세 ─────────────────────────────────────────────
    # ⚠️ severance_pay는 amount_manwon(문맥 없는 범용 단일 금액)을 폴백으로
    #    쓰지 않는다. "근속 25년차이고 연봉 8천만원인데 DC로 퇴직금 얼마나
    #    받을 수 있나요"에서 연봉 8,000만원이 severance_manwon 가드(income
    #    guard)를 통과해도, amount_manwon 폴백이 그대로 살아 있어 같은 값이
    #    또 새 나갔다(UI-013, 2026-09-06). severance_manwon이 없으면
    #    되묻는 편이 근거 없는 세금 계산보다 낫다.
    "퇴직소득세_계산": [
        ParamSpec("severance_pay", _get("severance_manwon"),
                  ask_back="퇴직급여 총액"),
        ParamSpec("service_years", _get("service_years"), ask_back="근속연수"),
    ],

    # ── 유틸 · 적합성 ──────────────────────────────────────────
    "세율기준_정규화": [
        ParamSpec("rate", _get("rate"), ask_back="정규화할 세율"),
        ParamSpec("includes_local_tax", _get("includes_local_tax"),
                  variants=_LOCAL_TAX_VARIANTS,
                  ask_back="제시된 세율이 지방소득세를 포함한 값인지"),
    ],
    "판매클래스_적합성_판정": [
        ParamSpec("fund_class", _get("fund_class"),
                  ask_back="확인하려는 판매 클래스(예: C-P, C-Re)"),
        ParamSpec("user_account_type", _get("account_type"),
                  ask_back="보유 계좌 유형(연금저축/IRP/퇴직연금/일반)"),
    ],
    "총보수_비교": [
        ParamSpec("candidates", _get("product_candidates"),
                  ask_back="비교 대상 상품(제공 자료에서 총보수 정보를 "
                           "가진 상품을 찾지 못했습니다)"),
        ParamSpec("user_account_type", _get("account_type"),
                  ask_back="보유 계좌 유형(연금저축/IRP/퇴직연금/일반)"),
    ],
    "구법수치_탐지": [
        ParamSpec("doc_text", _get("_evidence_text"),
                  ask_back="검사할 근거 문서"),
        ParamSpec("doc_id", _get("_evidence_doc_id"), required=False, default=""),
    ],
}

# DEPRECATED 함수 → 대체 함수. L1이 잘못 고르면 여기서 결정론적으로 교정한다.
_REMAP = {"과세방식_판정_계산": "과세방식_비교_계산"}


def remap_function(name: str) -> str:
    """DEPRECATED 함수명을 현행 함수로 교정."""
    return _REMAP.get(name, name)


# ════════════════════════════════════════════════════════════════
# 빌더
# ════════════════════════════════════════════════════════════════

@dataclass
class CalcParamsBuilder:
    """조건 dict를 물고 다니는 인자 조립기.

    `coverage_pipeline.run_calculations(slots, calc_params_builder=...)`가
    기대하는 (slot) -> dict 시그니처를 `__call__`로 만족시킨다.
    """
    conditions: dict[str, Any] = field(default_factory=dict)
    ask_back: dict[str, list[str]] = field(default_factory=dict)      # slot_id → 확인 문구
    assumptions: dict[str, list[str]] = field(default_factory=dict)   # slot_id → 가정 문구
    unknown_functions: list[str] = field(default_factory=list)

    def __call__(self, slot: RequirementSlot) -> dict:
        fn_name = remap_function(slot.calc_function or "")
        if fn_name != slot.calc_function:
            slot.calc_function = fn_name        # DEPRECATED 교정 반영

        specs = CALC_PARAM_SPECS.get(fn_name)
        if specs is None:
            self.unknown_functions.append(fn_name)
            raise MissingCalcParams(
                fn_name, ["인자 규격 미정의"],
                [f"'{fn_name}' 계산에 필요한 조건"])

        # 기본값만으로 계산이 굴러가 무의미한 결과를 내는 경우를 먼저 막는다
        if fn_name in REQUIRE_ANY:
            keys, ask = REQUIRE_ANY[fn_name]
            if not any(self.conditions.get(k) is not None for k in keys):
                self.ask_back.setdefault(slot.slot_id, []).append(ask)
                raise MissingCalcParams(fn_name, list(keys), [ask])

        params: dict[str, Any] = {}
        missing: list[str] = []
        asks: list[str] = []
        notes: list[str] = []
        variant_spec: Optional[ParamSpec] = None

        for spec in specs:
            value = spec.resolve(self.conditions)

            if value is not None:
                # 범위를 벗어난 값으로 계산하면 '그럴듯하지만 틀린 숫자'가 나온다.
                # 값이 없는 것과 똑같이 취급해 되묻는다.
                if (bad := validate_param(spec.name, value)) is not None:
                    missing.append(spec.name)
                    asks.append(bad)
                    continue
                params[spec.name] = value
                continue

            # 값이 없다 — 조건 분기 / 기본값 / 부족 중 하나
            if spec.variants and variant_spec is None:
                variant_spec = spec              # 첫 번째 분기 인자만 나눈다
                continue
            if not spec.required:
                params[spec.name] = spec.default
                if spec.assumption:
                    notes.append(spec.assumption)
                continue
            missing.append(spec.name)
            if spec.ask_back:
                asks.append(spec.ask_back)

        if notes:
            self.assumptions.setdefault(slot.slot_id, []).extend(notes)

        if missing:
            self.ask_back.setdefault(slot.slot_id, []).extend(asks)
            raise MissingCalcParams(fn_name, missing, asks)

        if variant_spec is not None:
            # 조건을 모르지만 경우의 수가 적다 → 나눠서 전부 계산
            self.assumptions.setdefault(slot.slot_id, []).append(
                f"{variant_spec.ask_back}을(를) 확인하지 못해 경우를 나눠 계산")
            return {VARIANTS_KEY: [
                (label, {**params, variant_spec.name: value})
                for label, value in variant_spec.variants
            ]}

        return params

    # ── 상위 계층이 읽는 요약 ─────────────────────────────────
    def ask_back_items(self, limit: int = 2) -> list[str]:
        """확인 항목 (중복 제거, 최대 limit건 — 원칙: 되묻기는 2건까지)."""
        seen: list[str] = []
        for items in self.ask_back.values():
            for it in items:
                if it not in seen:
                    seen.append(it)
        return seen[:limit]

    def assumption_items(self) -> list[str]:
        seen: list[str] = []
        for items in self.assumptions.values():
            for it in items:
                if it not in seen:
                    seen.append(it)
        return seen


def make_calc_params_builder(conditions: dict[str, Any]) -> CalcParamsBuilder:
    return CalcParamsBuilder(conditions=dict(conditions or {}))
