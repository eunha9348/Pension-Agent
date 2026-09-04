"""
연금 결정론적 계산 함수 모듈
=============================
출처: TalkFile_연금_결정론적함수.html (수리 담당 팀원 규격서)
국민연금 + 사적연금(연금저축/IRP) 납입·수령·과세 계산.

원칙: 이 모듈의 함수는 "계산"만 한다. 설명·문장 생성은 LLM이 담당한다.
금액 단위: 만원 (규격서 원문 기준 — 호출 측에서 원 단위 변환 시 주의)

════════════════════════════════════════════════════════════════
근거 대조 결과 (project_knowledge_search, 2026.08 · 2차 재검증 반영)
════════════════════════════════════════════════════════════════
① [정정] 분리과세 15% vs 16.5% — 상충이 아니라 '지방소득세 포함 여부'
   · 15%  기준: R2_KR5113420013 / R2_KR5113420015 / R2_KR5113450111
     "15% 분리과세 선택가능 [2024년 1월 1일 이후 연금수령하는 분부터 적용]"
   · 16.5% 기준: R2_KR516702010M / R2_KR5111450067 / R2_KR5111420047 / doc39
     "분리과세(16.5%) 선택 가능 (2024년 1월 1일 이후 연금수령하는 분부터 적용)"
   → 시행일이 동일하고 15 × 1.1 = 16.5 이므로 같은 세율의 다른 표기.
     보강 근거: R2_KR5111420047 "15.4% (소득세 14%, 지방소득세 1.4%)" — 지방소득세는
     국세의 10% 부가 구조임이 문서로 확인됨.
   → 설계 원칙: 상충 해소가 아니라 **기준 정규화**. 모든 세율은 내부적으로
     국세 기준으로 보관하고, 출력 시 어느 기준인지 반드시 명시한다.

   ⚠️ 이전 보고서에서 "doc39가 15%를 지지한다"고 기술한 것은 오류였음.
      doc39 원문은 16.5%(지방소득세 포함 기준)임.

② [자료 성격 구분] 국민연금(공적연금) 수치는 제공자료에 없음.
   과제 안내서의 "기본 연금제도 자료에 한해 외부데이터 수집 가능" 예외 조항 적용.
   물가상승률 등 미래 변수는 '명시적 가정 파라미터'로만 사용 (단정 금지).

③ [미해결 — 팀 판단 필요] 이연퇴직소득 연금수령 시 세율 표기 불일치
   · doc39 / doc40: 이연퇴직소득세의 70%(1~10년차) / 60%(11~20년차) / 50%(21년차~)
   · R2_KR516702010M: "이연퇴직소득은 3.3% (지방소득세 포함)"
   두 서술은 구조가 다름(감면율 방식 vs 고정세율 방식). 임의로 하나를 택하지 말고
   질의 대상 상품의 근거문서를 따르되, 답변에 출처를 명시할 것.

⚠️ 미해결 — 추가 확인 필요
   calc_private_withholding의 '종신형 + 80세 이상' 세율 우선순위는 검색된
   문서들이 "연령에 따른 차등과세(5.5~3.3%)"라고만 서술해 종신형의 age 대비
   우선순위까지는 명시하지 않음. 팀원과 재확인 권장 (로직은 원문 그대로 유지).
"""

from __future__ import annotations


# ════════════════════════════════════════════════
# 1. 국민연금 (National Pension)
# SOURCE: 프로젝트 제공문서 없음 — "기본 연금제도 자료" 예외조항 적용,
#         외부 공식자료(국민연금공단 고시) 근거. 사적연금 함수와 근거
#         출처가 다르므로 답변 생성 시 retrieved_context에 구분 표기 필요.
# ════════════════════════════════════════════════

def calc_np_copay(I_monthly: float, r_np_premium: float = 0.09) -> dict:
    """[1.1] 국민연금 월 본인부담금 (노사 절반 부담).
    C_np_copay = I_monthly × r_np_premium × 0.5
    """
    C_np_copay = I_monthly * r_np_premium * 0.5
    return {"C_np_copay": round(C_np_copay, 4)}


def calc_np_birth_credit_months(N: int, N0: int, N1: int, N2: int) -> dict:
    """[1.2] 출산크레딧 인정개월수.
    N  : 전체 자녀 수
    N0 : 2007-12-31 이전 출생 자녀 수
    N1 : 2008-01-01 ~ 2025-12-31 출생 자녀 수
    N2 : 2026-01-01 이후 출생 자녀 수
    """
    if N0 == 0 and N1 == 0:
        M = 12 * N2 + 6 * max(N2 - 2, 0)
    elif (N1 + N2) != 0 and N >= 2:
        M = min(50, 12 + 18 * (N0 + N1)) + 18 * N2
    else:
        M = 0
    return {"M_np_credit": M}


def calc_np_benefit(I_final_monthly: float, r_irr: float) -> dict:
    """[1.3] 국민연금 월/연 수령액 (연 수령액 = 종합과세 대상액)."""
    P_monthly = I_final_monthly * r_irr
    P_annual = 12 * P_monthly
    return {"P_np_monthly": round(P_monthly, 4), "P_np_annual": round(P_annual, 4)}


# ════════════════════════════════════════════════
# 2. 사적연금 (연금저축 / IRP)
# ════════════════════════════════════════════════

# 세액공제 한도 — 현행 기준. 구법(700/1200)과 혼동하면 안 된다(함정 C5).
LIMIT_PENSION_SAVING = 600      # 연금저축 단독
LIMIT_COMBINED = 900            # 연금저축 + IRP 합산
LIMIT_ANNUAL_CONTRIB = 1800     # 연간 총 납입한도


def calc_private_contribution_limit(X_pension_saving=None, Y_irp_personal=None,
                                     r_tax_credit: float = 0.165) -> dict:
    """[2.1] 사적연금 납입한도 검증 및 세액공제액.
    r_tax_credit: 소득 구간에 따라 13.2% 또는 16.5% (호출 측에서 판단 후 전달)
    한도: 연금저축 단독 600만원 / 연금저축+IRP 합산 900만원 / 총 납입한도 1,800만원

    ━━ 한도는 납입액을 몰라도 정해진다 ━━
    한도 셋은 제도가 정한 상수이므로 납입액과 무관하다. 예전에는 이 값들이
    수식 안에만 있고 **출력되지 않아서**, "얼마까지 받을 수 있나요"처럼
    한도를 묻는 질의에 답이 나가도 600·900이 문장에 실릴 보장이 없었다
    (평가 E-01). 이제 항상 함께 반환한다.

    납입액을 모르면 세액공제액은 **내지 않는다.** 0원으로 굴리면
    "세액공제액 = 0만원"이라는 맞지만 오해를 부르는 답이 되기 때문이다.
    한도만 알려주고 납입액은 확인 요청으로 돌리는 것이 옳다.
    """
    out = {
        "연금저축_단독_한도": LIMIT_PENSION_SAVING,
        "연금저축_IRP_합산_한도": LIMIT_COMBINED,
        "연간_총납입한도": LIMIT_ANNUAL_CONTRIB,
        # ━━ 공제율은 납입액을 몰라도 정해진다 (2026-09-01 추가) ━━
        # 공제율은 **소득 구간만으로** 결정된다. 그런데 예전에는 납입액이
        # 없으면 한도만 내고 이 값을 아예 출력하지 않았다. 그 결과
        # "총급여 8,000만원이면 얼마까지?"에 13.2%가 답변에 실릴 보장이
        # 없었고, 실측에서 16.5%로 잘못 안내됐다.
        #
        # 형제 함수 calc_private_withholding이 이미 같은 원칙을 따른다 —
        # 수령액을 몰라도 세율(r_withholding)은 낸다. 산출 가능한 것은
        # 내주고, 값이 더 필요한 부분만 확인 요청으로 돌린다.
        #
        # 소득을 모르면 호출 측(calc_params의 _RATE_VARIANTS)이 두 구간을
        # 각각 계산하므로, 여기서 한쪽을 단정하는 일은 생기지 않는다.
        "세액공제율": r_tax_credit,
    }
    if X_pension_saving is None and Y_irp_personal is None:
        out["note"] = ("납입액이 확인되지 않아 한도와 공제율만 안내합니다. "
                       "실제 세액공제액은 납입액에 따라 달라집니다.")
        return out

    x = X_pension_saving or 0.0
    y = Y_irp_personal or 0.0
    capped_x = min(x, LIMIT_PENSION_SAVING)
    combined_before_cap = capped_x + y
    A_eligible = min(combined_before_cap, LIMIT_COMBINED)
    out["A_tax_credit"] = round(A_eligible * r_tax_credit, 4)
    out["IsLimitExceeded"] = (x + y) > LIMIT_ANNUAL_CONTRIB

    # ━━ 어느 한도가 결과를 갈랐는지 밝힌다 (2026-09-03 추가) ━━
    # 예전에는 IsLimitExceeded 하나뿐이었는데, 이건 총납입한도(1,800만원)
    # 초과만 본다. 그런데 실측 결함은 "연금저축에 900만원 넣으면 다
    # 공제되나요?"였다 — 900은 1,800을 안 넘으니 IsLimitExceeded는
    # False다. 정작 걸린 건 연금저축 **단독** 한도(600만원)인데, 이걸
    # 알리는 신호가 어디에도 없었다. LLM이 "다 공제됩니다"라고 써도
    # 아무 판정도 못 잡는다.
    #
    # 이 두 플래그는 numeric_verifier._presence_targets가 읽어서, 해당
    # 한도가 실제로 결과를 깎았을 때는 계산값이 있어도 한도 상수를
    # 답변에 강제로 싣는다. supervisory_board.audit_anomaly도 이걸 보고
    # "한도를 넘었다는 사실을 답변에 명시했는가"를 REVISE 대상으로 삼는다.
    out["IsPensionSavingLimitExceeded"] = x > LIMIT_PENSION_SAVING
    out["IsCombinedLimitExceeded"] = combined_before_cap > LIMIT_COMBINED
    return out


def calc_private_withholding(P_private_monthly=None, Age=None,
                             IsAnnuityType: bool = False) -> dict:
    """[2.2] 사적연금 원천징수세율 및 1,500만원 초과분 산출.

    ━━ 수령액을 모르면 세액을 내지 않는다 ━━
    세율은 나이·수령형태만으로 정해지지만 **세액은 수령액이 있어야** 나온다.
    예전에는 수령액이 없을 때 0으로 굴려서 "원천징수세액 = 0만원",
    "1,500만원 초과분 = 0만원"을 출력했다. 0원은 사실이 아니라 **미입력**인데,
    사용자에게는 "세금이 0원"으로 읽힌다. 실측 300건 감사에서 지적됐다.
    한도만 아는 경우 한도만 냈던 calc_private_contribution_limit과 같은 원칙이다.

    ⚠️ 규격서 원문 순서상 IsAnnuityType 체크가 Age 체크보다 먼저 적용됩니다.
       즉 '종신형 + 80세 이상' 조합은 3.3%가 아니라 4.4%로 계산됩니다.
       일반적으로 알려진 세법 해석은 80세 이상이면 종신형 여부와 무관하게
       3.3%(더 낮은 쪽)가 적용된다는 것인데, 이 규격서는 종신형을 최우선으로
       고정하고 있어 실제 세법 조문과 다를 가능성이 있습니다.
       프로젝트 내 세제 관련 원본 문서로 재확인 후 필요시 분기 순서만
       바꾸면 됩니다 (로직 구조는 그대로 유지 가능).
    """
    if Age is None:
        # 연령별 차등과세의 기준이라 임의 가정이 불가능하다. 조용히 기본값을
        # 쓰면 55세 미만으로 취급돼 16.5%가 나가므로 예외로 끊는다.
        raise ValueError("Age는 필수입니다 — 연령별 차등과세의 기준입니다")

    if IsAnnuityType:
        r = 0.044
    elif Age >= 80:
        r = 0.033
    elif Age >= 70:
        r = 0.044
    elif Age >= 55:
        r = 0.055
    else:
        r = 0.165  # 55세 미만 해지/수령 시 기타소득세 기준

    out = {"r_withholding": r}
    if P_private_monthly is None:
        out["note"] = ("월 수령액이 확인되지 않아 세율만 안내합니다. "
                       "원천징수세액은 수령액에 따라 달라집니다.")
        return out

    P_annual = 12 * P_private_monthly
    out["T_withholding"] = round(min(P_annual, 1500) * r, 4)
    out["P_private_excess"] = round(max(0.0, P_annual - 1500), 4)
    return out


# ════════════════════════════════════════════════
# 3. 종합 수령 시나리오 및 과세 방식 판정
# ════════════════════════════════════════════════

def _f_comp(Z: float) -> float:
    """종합소득세 누진세율 속산식 (금액 단위: 만원).
    6/15/24/35/38/40/42/45% 8단계 누진공제 방식 — 표준 세율표와 일치 확인됨."""
    candidates = [
        0.06 * Z,
        0.15 * Z - 126,
        0.24 * Z - 576,
        0.35 * Z - 1544,
        0.38 * Z - 1994,
        0.40 * Z - 2594,
        0.42 * Z - 3594,
        0.45 * Z - 6594,
    ]
    return max(candidates)


DEFAULT_SEPARATE_TAX_RATE_LOCAL = 0.165   # 지방소득세 포함 기준
DEFAULT_SEPARATE_TAX_RATE_NATIONAL = 0.15  # 국세 기준
# 두 값은 동일 세율의 다른 표기 (15 × 1.1 = 16.5). 문서별 표기 차이일 뿐 상충 아님.
# 근거: R2_KR5113420013·15·R2_KR5113450111(15%) / doc39·R2_KR516702010M 외(16.5%)
#      시행일 모두 "2024.1.1 이후 연금수령분"으로 동일.


def compare_taxation_options(P_np_annual: float,
                              P_private_pension_annual: float,
                              other_comprehensive_income: float = 0.0,
                              pension_income_deduction: float | None = None,
                              personal_deduction: float = 150.0,
                              include_local_tax: bool = True) -> dict:
    """[전면 재작성] 분리과세 vs 종합과세 세액 비교.

    ⚠️ 팀원 규격서 §3.1의 calc_taxation_mode 대비 3가지를 정정했습니다.
       근거와 검증 과정은 아래 주석 참조. 원 함수는 하위호환용으로 남겨둡니다.

    ── 정정 1. 과세 대상 범위 ────────────────────────────────
    규격서: P_private_excess = max(0, 연액 − 1500) 만 과세
    정정  : 1,500만원 '초과 시' 사적연금소득 **전액**이 선택 대상
    근거  : 제공문서 원문 — "연간 연금수령액이 1,500만원…을 초과하는 경우
            연금소득분리과세(16.5%) 또는 종합과세 중 선택가능"
            (과세 객체가 '초과분'이 아니라 '연금소득'임)
            R2_KR5111450067 / R2_KR5111420047 / R2_KR516702010M / doc39
    영향  : 연 2,000만원 수령 시 82.5만원 → 330만원 (약 4배 과소계상)

    ── 정정 2. 세율 기준 불일치 ──────────────────────────────
    규격서: f_comp(Z) [국세 6~45%]  vs  Z × 0.165 [지방소득세 포함]
            → 손익분기 576/(0.24−0.165) = 7,680만원
    정정  : 양변을 같은 기준으로 통일해야 함
            f(Z)×1.1 vs Z×0.165  ⇔  f(Z) vs Z×0.15
            → 손익분기 576/(0.24−0.15) = **6,400만원**
    영향  : 6,400~7,680만원 구간에서 유불리 판정이 뒤집힘

    ── 정정 3. 과세표준 ≠ 총연금액 ───────────────────────────
    규격서: 누진세율표를 총연금액 Z에 직접 적용
    정정  : 종합과세 과세표준 = 총연금액 − 연금소득공제 − 종합소득공제
            (소득세법 제47조의2 연금소득공제, 한도 900만원)
    주의  : 연금소득공제·인적공제 수치는 **제공문서에 없음**.
            아래 기본식은 소득세법 조문 기준이며, 답변 시 반드시
            "제공자료 외 일반 세법 기준"임을 명시할 것.
            사용자 부양가족 등 개인 사정 미상 시 ASK_BACK 대상.

    반환값의 recommendation은 '유리한 쪽'을 제시할 뿐 단정 추천이 아니며,
    호출 측에서 반드시 조건부 문장으로 변환해야 합니다.
    """
    is_choice_required = P_private_pension_annual > 1500

    if not is_choice_required:
        return {
            "choice_required": False,
            "note": "사적연금 연 1,500만원 이하 — 저율 연금소득세(5.5~3.3%) 원천징수로 종결. "
                    "종합/분리 선택 대상 아님.",
            "source": "R2_KR5111450067 외 · doc39",
        }

    # ── 분리과세 선택 시 ──
    # 사적연금 전액 × 16.5% (분리) + 공적연금 등은 별도 종합과세
    sep_private_tax = P_private_pension_annual * DEFAULT_SEPARATE_TAX_RATE_LOCAL
    base_np = max(0.0, P_np_annual + other_comprehensive_income - personal_deduction)
    sep_other_tax = _f_comp(base_np) * (1.1 if include_local_tax else 1.0)
    separate_total = sep_private_tax + sep_other_tax

    # ── 종합과세 선택 시 ──
    total_pension = P_np_annual + P_private_pension_annual
    if pension_income_deduction is None:
        pension_income_deduction = _pension_income_deduction(total_pension)
    base_comp = max(0.0, total_pension - pension_income_deduction
                    + other_comprehensive_income - personal_deduction)
    comprehensive_total = _f_comp(base_comp) * (1.1 if include_local_tax else 1.0)

    cheaper = "SEPARATE" if separate_total < comprehensive_total else "COMPREHENSIVE"
    return {
        "choice_required": True,
        "separate": {
            "사적연금_분리과세": round(sep_private_tax, 2),
            "그외_종합과세": round(sep_other_tax, 2),
            "합계": round(separate_total, 2),
        },
        "comprehensive": {
            "과세표준": round(base_comp, 2),
            "연금소득공제": round(pension_income_deduction, 2),
            "합계": round(comprehensive_total, 2),
        },
        "lower_tax_option": cheaper,
        "difference": round(abs(separate_total - comprehensive_total), 2),
        "기준": "지방소득세 포함" if include_local_tax else "국세",
        "⚠️": "연금소득공제·인적공제는 제공문서 외 일반 세법 기준. "
              "실제 세액은 부양가족·타소득에 따라 달라지므로 확인 필요.",
    }


def _pension_income_deduction(total_pension: float) -> float:
    """연금소득공제 (소득세법 제47조의2, 한도 900만원). 단위: 만원.

    ⚠️ 이 수치는 **제공문서에 없음** — 일반 세법 기준.
       답변에 사용 시 출처가 제공자료가 아님을 명시할 것.
    """
    if total_pension <= 350:
        d = total_pension
    elif total_pension <= 700:
        d = 350 + (total_pension - 350) * 0.40
    elif total_pension <= 1400:
        d = 490 + (total_pension - 700) * 0.20
    else:
        d = 630 + (total_pension - 1400) * 0.10
    return min(d, 900)


def calc_taxation_mode(P_np_annual: float, P_private_excess: float,
                        separate_tax_rate: float = DEFAULT_SEPARATE_TAX_RATE_LOCAL,
                        source_doc_id: str | None = None) -> dict:
    """[DEPRECATED] 팀원 규격서 §3.1 원문 로직. 하위호환용으로만 보존.

    ⚠️ 위 compare_taxation_options()를 사용하십시오.
       본 함수는 과세범위·세율기준·과세표준 3가지 문제가 있어
       실제 답변 생성에 사용하면 안 됩니다 (상세는 위 함수 주석 참조).
    """
    threshold = max(0, 7680 - P_np_annual)
    if P_private_excess > threshold:
        mode, T_final = "SEPARATE", P_private_excess * separate_tax_rate
    else:
        mode, T_final = "COMPREHENSIVE", _f_comp(P_np_annual + P_private_excess)
    return {
        "TaxationMode": mode,
        "T_final": round(T_final, 4),
        "separate_tax_rate_used": separate_tax_rate,
        "rate_source": source_doc_id if source_doc_id else "default",
        "DEPRECATED": "compare_taxation_options() 사용 권장",
    }


# ════════════════════════════════════════════════
# 4. 연금수령한도 · 연차 체계
# SOURCE: doc39, doc40 (미래에셋 제공자료)
# ════════════════════════════════════════════════

def calc_withdrawal_limit(account_value: float, pension_year: int) -> dict:
    """[doc39] 연금수령한도 = 평가액 ÷ (11 − 연금수령연차) × 120%

    account_value: 매년 1월 1일 또는 연금개시일 기준 평가액
    pension_year : 연금수령연차 (연금개시 가능한 해가 1년차)

    doc39: "11년차부터는 한도 자체가 사라져 전액 일시 인출을 하여도
            연금수령으로 인정받을 수 있다."
    """
    if pension_year >= 11:
        return {
            "limit": None,               # 한도 없음
            "unlimited": True,
            "note": "연금수령연차 11년차 이상 — 한도 없음 (전액 연금수령 인정)",
            "source": "doc39",
        }
    denominator = 11 - pension_year
    limit = account_value / denominator * 1.2
    return {
        "limit": round(limit, 4),
        "unlimited": False,
        "denominator": denominator,
        "source": "doc39",
    }


def calc_pension_year(join_date_before_2013_03_01: bool, years_elapsed: int) -> dict:
    """[doc39] 연금수령연차 기산. 2013.3.1 이전 가입 계좌는 6년차부터 기산.

    ⚠️ 연금수령연차 ≠ 연금실제수령연차. 아래 calc_actual_receipt_year 참조.
       doc40이 명시적으로 혼동 주의를 경고하는 지점 — 오류전제 감지 대상.
    """
    base = 6 if join_date_before_2013_03_01 else 1
    return {
        "pension_year": base + years_elapsed,
        "special_rule_applied": join_date_before_2013_03_01,
        "note": "2013.3.1 이전 가입 계좌 6년차 특례 적용" if join_date_before_2013_03_01
                else "일반 기산 (1년차 시작)",
        "source": "doc39",
    }


def calc_retirement_tax_reduction(actual_receipt_year: int) -> dict:
    """[doc40] 연금실제수령연차에 따른 이연퇴직소득세 감면.

    ⚠️ 핵심 구분 (doc40 원문):
       · 연금수령연차     → '연금수령한도'를 결정. 수령하지 않아도 매년 누적.
       · 연금실제수령연차 → '퇴직소득세 감면율'을 결정. 실제 인출한 연도만 누적.
       "연금 개시를 하고 1만원이라도 인출하여야 해당 연도의 연금실제수령연차가 쌓인다.
        인출을 중단한다면 연금실제수령연차도 누적되지 않는다."

    이 둘을 혼동한 질의(예: "11년째니까 40% 감면되죠?")는 전제 오류로
    분류해 바로잡아야 함 → 평가지표 '정확성' 대응 지점.
    """
    if actual_receipt_year >= 21:
        rate, reduction = 0.50, 0.50
        band = "21년차 ~"
    elif actual_receipt_year >= 11:
        rate, reduction = 0.60, 0.40
        band = "11~20년차"
    else:
        rate, reduction = 0.70, 0.30
        band = "1~10년차"
    return {
        "applied_rate_of_original_tax": rate,   # 이연퇴직소득세율 × 이 값
        "reduction_rate": reduction,            # 감면율
        "band": band,
        "source": "doc40",
    }


# ════════════════════════════════════════════════
# 5. 퇴직소득세 계산 구조
# SOURCE: doc52 (미래에셋 제공자료)
# ════════════════════════════════════════════════

def _service_year_deduction(years: int) -> float:
    """[doc52] 근속연수공제 (단위: 만원)."""
    if years <= 5:
        return 100 * years
    if years <= 10:
        return 500 + 200 * (years - 5)
    if years <= 20:
        return 1500 + 250 * (years - 10)
    return 4000 + 300 * (years - 20)


def _converted_salary_deduction(converted: float) -> float:
    """[doc52] 환산급여공제 (단위: 만원)."""
    if converted <= 800:
        return converted                      # 전액공제
    if converted <= 7000:
        return 800 + (converted - 800) * 0.60
    if converted <= 10000:
        return 4520 + (converted - 7000) * 0.55
    if converted <= 30000:
        return 6170 + (converted - 10000) * 0.45
    return 15170 + (converted - 30000) * 0.35


def calc_retirement_income_tax(severance_pay: float, service_years: int) -> dict:
    """[doc52] 퇴직소득세 산출.

    퇴직급여 − 근속연수공제 → ÷ 근속연수 × 12 = 환산급여
    환산급여 − 환산급여공제 = 퇴직소득 과세표준
    (과세표준 × 기본세율) − 누진공제 = 환산산출세액
    환산산출세액 ÷ 12 × 근속연수 = 산출세액

    기본세율은 _f_comp()와 동일한 누진 구조 사용 (팀원 규격서 §3.2 기준).
    금액 단위: 만원. 지방소득세 미포함(국세 기준).
    """
    if service_years <= 0:
        raise ValueError("근속연수는 1년 이상이어야 합니다")

    sy_deduction = _service_year_deduction(service_years)
    converted = max(0.0, (severance_pay - sy_deduction)) / service_years * 12
    cs_deduction = _converted_salary_deduction(converted)
    tax_base = max(0.0, converted - cs_deduction)
    converted_tax = _f_comp(tax_base)
    final_tax = converted_tax / 12 * service_years

    return {
        "근속연수공제": round(sy_deduction, 4),
        "환산급여": round(converted, 4),
        "환산급여공제": round(cs_deduction, 4),
        "퇴직소득_과세표준": round(tax_base, 4),
        "환산산출세액": round(converted_tax, 4),
        "산출세액": round(final_tax, 4),
        "기준": "국세 (지방소득세 미포함)",
        "source": "doc52",
    }


# ════════════════════════════════════════════════
# 6. 지방소득세 기준 정규화
# SOURCE: R2_KR5111420047 "15.4% (소득세 14%, 지방소득세 1.4%)"
#         → 지방소득세 = 국세의 10% 부가
# ════════════════════════════════════════════════

def normalize_tax_rate(rate: float, includes_local_tax: bool) -> dict:
    """세율을 국세/지방세포함 두 기준으로 동시 제공.

    문서마다 표기 기준이 다르므로(15% vs 16.5%), 계산은 한 기준으로 통일하고
    답변 출력 시 어느 기준인지 명시하기 위한 유틸.
    """
    if includes_local_tax:
        national, with_local = rate / 1.1, rate
    else:
        national, with_local = rate, rate * 1.1
    return {
        "national_rate": round(national, 6),
        "rate_with_local_tax": round(with_local, 6),
        "display_note": f"국세 {national*100:.1f}% / 지방소득세 포함 {with_local*100:.1f}%",
    }


# ════════════════════════════════════════════════
# 7. 상품 적합성 — 가입자격 필터 (Exploitation Agent용)
# SOURCE: 각 투자설명서 "종류별 가입자격에 관한 사항"
# ════════════════════════════════════════════════

# 판매 클래스 접미사 → 요구 계좌유형. 투자설명서 가입자격 서술에서 도출.
CLASS_ACCOUNT_REQUIREMENT = {
    "C-P":   "연금저축",      # 연금저축계좌를 통하여 가입한 자
    "C-Pe":  "연금저축",
    "C-PE":  "연금저축",
    "S-P":   "연금저축",
    "C-R":   "퇴직연금",      # 근로자퇴직급여보장법에 의한 퇴직연금
    "C-Re":  "퇴직연금",
    "C-P2":  "퇴직연금",
    "C-P2E": "퇴직연금",
    "S-P2":  "퇴직연금",
    "S-R":   "퇴직연금",
    "Crp":   "퇴직연금",
    "Crp-e": "퇴직연금",
}

# 별도 자격요건이 있어 일반 개인이 가입 불가한 클래스
RESTRICTED_CLASSES = {
    "C-F":  "기관투자자 또는 고액매입 요건",
    "C-RF": "기관투자자 요건",
    "C-RJ": "집합투자업자 직접판매(직판) 전용",
    "C-W":  "일임형 랩어카운트·특정금전신탁 전용",
    "C-3":  "고액투자자 전용",
    "S-I":  "기관 전용",
}


def check_class_eligibility(fund_class: str, user_account_type: str) -> dict:
    """사용자 계좌유형에 해당 판매 클래스가 적합한지 판정.

    user_account_type: "연금저축" | "퇴직연금" | "IRP" | "일반"

    ⚠️ 이 필터가 없으면 연금저축 고객에게 퇴직연금 전용 클래스를 추천하는
       오답이 발생함. 총보수 비교 시에도 반드시 '가입 가능한 클래스끼리만'
       비교해야 함 (직판 클래스가 총보수는 낮지만 가입 불가).
    """
    normalized = "퇴직연금" if user_account_type in ("퇴직연금", "IRP") else user_account_type

    if fund_class in RESTRICTED_CLASSES:
        return {
            "eligible": False,
            "reason": f"{fund_class}: {RESTRICTED_CLASSES[fund_class]} — 일반 개인 가입 불가",
        }

    required = CLASS_ACCOUNT_REQUIREMENT.get(fund_class)
    if required is None:
        return {"eligible": True, "reason": f"{fund_class}: 계좌유형 제한 없음 (원문 확인 권장)"}
    if required != normalized:
        return {
            "eligible": False,
            "reason": f"{fund_class}는 {required} 계좌 전용 — 보유 계좌({user_account_type})로 가입 불가",
        }
    return {"eligible": True, "reason": f"{fund_class}: {required} 계좌 적합"}


def compare_total_expense(candidates: list[dict], user_account_type: str) -> dict:
    """가입 가능한 클래스끼리만 총보수를 비교.

    candidates: [{"fund_class": "C-P", "total_expense": 0.5440, "name": ...}, ...]
    가입 불가 클래스는 비교 대상에서 제외하고 제외 사유를 함께 반환.
    """
    eligible, excluded = [], []
    for c in candidates:
        verdict = check_class_eligibility(c.get("fund_class", ""), user_account_type)
        (eligible if verdict["eligible"] else excluded).append({**c, "reason": verdict["reason"]})

    eligible.sort(key=lambda x: x.get("total_expense", float("inf")))
    return {
        "comparable": eligible,
        "excluded": excluded,
        "note": "가입 자격이 확인된 클래스끼리만 비교했습니다. "
                "위험등급은 집합투자업자 내부기준이므로 운용사가 다르면 직접 비교에 주의가 필요합니다.",
    }


# ════════════════════════════════════════════════
# 8. 문서 세법 빈티지 판별 (구법 반영 문서 탐지)
# ════════════════════════════════════════════════
#
# ⚠️ 제공자료에 **법 개정 이전 수치를 담은 구버전 투자설명서가 혼재**합니다.
#    확인된 사례 — R2_KR514X450008.pdf:
#      · "연 700만원 중 적은 금액"        → 현행 900만원 (구법)
#      · "연간 1200만원 이하인 경우 분리과세" → 현행 1,500만원 (구법)
#      · "종합소득금액이 4천만원 이하"     → 현행 4,500만원 (구법)
#      · "총급여액 1억2천만원 초과 시 300만원" → 폐지된 구간
#
#    이것은 §지방소득세 표기 차이와 **성격이 다릅니다**.
#    표기 차이는 환산으로 해소되지만, 구법 수치는 환산 불가능한 오답입니다.
#    검색 결과에 구법 수치가 섞이면 답변이 그대로 틀리므로 반드시 탐지해야 합니다.

LEGACY_TAX_MARKERS = {
    "700만원": "세액공제 한도 — 현행 900만원 (2023 개정)",
    "1200만원": "분리과세 기준 — 현행 1,500만원 (2024 개정)",
    "1,200만원": "분리과세 기준 — 현행 1,500만원 (2024 개정)",
    "4천만원": "세액공제율 구간 — 현행 4,500만원",
    "1억2천만원": "폐지된 세액공제 구간 (2023 개정 전)",
}

CURRENT_TAX_VALUES = {
    "세액공제_연금저축_한도": 600,
    "세액공제_합산_한도": 900,
    "연간_납입한도": 1800,
    "분리과세_기준": 1500,
    "세액공제율_구간_종합소득": 4500,
    "세액공제율_구간_총급여": 5500,
}


def detect_legacy_tax_content(doc_text: str, doc_id: str = "") -> dict:
    """근거문서에 구법 수치가 포함돼 있는지 탐지.

    Exploitation 단계에서 세제 관련 근거를 채택하기 전에 호출할 것.
    구법 마커가 발견되면 해당 문서는 세제 근거로 사용하지 않거나,
    최소한 think_trace에 경고를 남기고 현행 기준 문서를 우선한다.
    """
    found = [{"marker": m, "issue": desc}
             for m, desc in LEGACY_TAX_MARKERS.items() if m in doc_text]
    return {
        "doc_id": doc_id,
        "is_legacy_suspect": bool(found),
        "markers": found,
        "action": ("세제 근거로 사용 금지 — 현행 기준 문서 우선" if found
                   else "현행 기준으로 판단됨"),
    }

PENSION_CALC_FUNCTIONS = {
    # 국민연금 (외부 기본제도 자료 근거)
    "국민연금_본인부담금_계산": calc_np_copay,
    "출산크레딧_인정개월_계산": calc_np_birth_credit_months,
    "국민연금_수령액_계산": calc_np_benefit,
    # 사적연금 납입·수령 (제공문서 근거)
    "사적연금_납입한도_세액공제_계산": calc_private_contribution_limit,
    "사적연금_원천징수_계산": calc_private_withholding,
    # 과세방식 비교 (정정판)
    "과세방식_비교_계산": compare_taxation_options,
    "과세방식_판정_계산": calc_taxation_mode,          # DEPRECATED
    # 문서 검증
    "구법수치_탐지": detect_legacy_tax_content,
    # 연금수령한도 · 연차 (doc39, doc40)
    "연금수령한도_계산": calc_withdrawal_limit,
    "연금수령연차_계산": calc_pension_year,
    "퇴직소득세_감면율_계산": calc_retirement_tax_reduction,
    # 퇴직소득세 (doc52)
    "퇴직소득세_계산": calc_retirement_income_tax,
    # 유틸 · 적합성
    "세율기준_정규화": normalize_tax_rate,
    "판매클래스_적합성_판정": check_class_eligibility,
    "총보수_비교": compare_total_expense,
}
