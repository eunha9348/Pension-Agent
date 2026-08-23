"""계산함수 15종 회귀 테스트 — 기대값 고정.

제공문서 원문 예시를 재현하는 항목은 주석에 출처를 남긴다.
이 값들이 바뀌면 답변의 숫자가 통째로 바뀌므로, 리팩터링 시 여기가 방어선이다.
"""

from __future__ import annotations

import pytest

from app.core.coverage_pipeline import CALC_REGISTRY
from app.core.pension_calc_functions import (_f_comp, _pension_income_deduction,
                                             calc_np_benefit,
                                             calc_np_birth_credit_months,
                                             calc_np_copay, calc_pension_year,
                                             calc_private_contribution_limit,
                                             calc_private_withholding,
                                             calc_retirement_income_tax,
                                             calc_retirement_tax_reduction,
                                             calc_withdrawal_limit,
                                             check_class_eligibility,
                                             compare_taxation_options,
                                             compare_total_expense,
                                             detect_legacy_tax_content,
                                             normalize_tax_rate)


def test_레지스트리에_15종이_등록돼_있다():
    assert len(CALC_REGISTRY) == 15


# ── 연금수령한도 · 연차 (doc39, doc40) ───────────────────────

@pytest.mark.parametrize("value,year,expected", [
    (10000, 1, 1200.0),      # doc39 원문 예시: 1억 · 1년차 → 1,200만원
    (10000, 10, 12000.0),    # doc39: "10년차가 되면 분모가 1이 되어 평가액 전체의 120%"
])
def test_연금수령한도_원문예시(value, year, expected):
    assert calc_withdrawal_limit(value, year)["limit"] == pytest.approx(expected)


def test_11년차부터는_한도가_없다():
    """doc39: 11년차부터는 한도 자체가 사라진다."""
    out = calc_withdrawal_limit(10000, 11)
    assert out["unlimited"] is True
    assert out["limit"] is None


def test_2013년_이전_가입은_6년차부터_기산():
    """doc39: "만 59세에 이미 10년차 계좌를 보유하게 된다" (4년 경과 기준)"""
    assert calc_pension_year(True, 4)["pension_year"] == 10
    assert calc_pension_year(False, 4)["pension_year"] == 5


@pytest.mark.parametrize("year,reduction,band", [
    (1, 0.30, "1~10년차"),
    (11, 0.40, "11~20년차"),
    (21, 0.50, "21년차 ~"),
])
def test_이연퇴직소득세_감면율(year, reduction, band):
    """doc40 — 연금'실제'수령연차 기준이다. 연금수령연차와 혼동 금지."""
    out = calc_retirement_tax_reduction(year)
    assert out["reduction_rate"] == reduction
    assert out["band"] == band


# ── 퇴직소득세 (doc52) ───────────────────────────────────────

def test_근속연수공제_원문예시():
    """doc52: "25년 → 5,500만 원 공제" """
    out = calc_retirement_income_tax(20000, 25)
    assert out["근속연수공제"] == pytest.approx(5500.0)


def test_퇴직소득세_계산_단계가_모두_산출된다():
    out = calc_retirement_income_tax(20000, 25)
    for key in ("환산급여", "환산급여공제", "퇴직소득_과세표준",
                "환산산출세액", "산출세액"):
        assert key in out
    assert out["산출세액"] > 0
    assert out["기준"] == "국세 (지방소득세 미포함)"


def test_근속연수_0이하는_예외():
    """예외를 삼키지 않는다 — 잘못된 입력은 조용히 0을 주면 안 된다."""
    with pytest.raises(ValueError):
        calc_retirement_income_tax(10000, 0)


# ── 사적연금 납입·수령 ───────────────────────────────────────

@pytest.mark.parametrize("saving,irp,rate,expected", [
    (600, 300, 0.165, 148.5),     # 합산 900만원 한도 × 16.5%
    (600, 300, 0.132, 118.8),
    (900, 0, 0.165, 99.0),        # 연금저축 단독은 600만원까지만
    (0, 900, 0.165, 148.5),       # IRP 단독은 900만원까지
])
def test_세액공제액(saving, irp, rate, expected):
    out = calc_private_contribution_limit(saving, irp, rate)
    assert out["A_tax_credit"] == pytest.approx(expected)


def test_연간_납입한도_1800만원_초과_판정():
    assert calc_private_contribution_limit(1000, 900, 0.165)["IsLimitExceeded"] is True
    assert calc_private_contribution_limit(600, 300, 0.165)["IsLimitExceeded"] is False


# ── 한도 자체를 묻는 질의 (E-01 회귀) ──────────────────────────
# 한도 셋은 제도가 정한 상수라 납입액과 무관하다. 예전에는 수식 안에만
# 있고 반환되지 않아, "얼마까지 세액공제 받나요"에 600·900이 답변에 실릴
# 보장이 없었다. 아래 셋이 이 회귀를 막는다.

def test_한도는_납입액과_무관하게_항상_반환된다():
    for args in [(), (600, 300, 0.165), (0, 0, 0.132), (2000, 500, 0.165)]:
        out = calc_private_contribution_limit(*args)
        assert out["연금저축_단독_한도"] == 600, args
        assert out["연금저축_IRP_합산_한도"] == 900, args
        assert out["연간_총납입한도"] == 1800, args


def test_납입액을_모르면_세액공제액을_내지_않는다():
    """0으로 굴리면 '세액공제액 0만원'이라는 맞지만 오해를 부르는 답이 된다."""
    out = calc_private_contribution_limit()
    assert "A_tax_credit" not in out
    assert "IsLimitExceeded" not in out
    assert "한도만" in out["note"]


def test_납입액이_한쪽만_있어도_세액공제액은_계산된다():
    """한쪽만 모르는 것은 '전부 모름'이 아니다 — 아는 쪽으로 계산한다."""
    only_saving = calc_private_contribution_limit(X_pension_saving=600,
                                                  r_tax_credit=0.165)
    assert only_saving["A_tax_credit"] == pytest.approx(99.0)

    only_irp = calc_private_contribution_limit(Y_irp_personal=900,
                                               r_tax_credit=0.165)
    assert only_irp["A_tax_credit"] == pytest.approx(148.5)


def test_한도_수치가_구법값이_아니다():
    """700/1200/4000은 개정 전 수치다(함정 C5). 섞이면 즉시 오답이다."""
    out = calc_private_contribution_limit()
    assert 700 not in out.values()
    assert 1200 not in out.values()


@pytest.mark.parametrize("age,annuity,rate", [
    (60, False, 0.055),
    (75, False, 0.044),
    (85, False, 0.033),
])
def test_연령별_원천징수세율(age, annuity, rate):
    assert calc_private_withholding(100, age, annuity)["r_withholding"] == rate


def test_종신형_80세_이상_세율_우선순위는_규격서_원문대로_고정():
    """⚠️ 미해결 항목 — 팀 확인 필요.

    규격서 원문 순서상 IsAnnuityType 체크가 Age보다 먼저라
    '종신형 + 80세 이상'은 3.3%가 아니라 4.4%가 된다.
    일반적인 세법 해석과 다를 수 있으나 **임의로 바꾸지 않는다.**
    이 테스트는 현재 동작을 고정할 뿐, 정답을 주장하지 않는다.
    """
    assert calc_private_withholding(100, 85, True)["r_withholding"] == 0.044


def test_1500만원_초과분_산출():
    out = calc_private_withholding(200, 60, False)   # 연 2,400만원
    assert out["P_private_excess"] == pytest.approx(900.0)


# ── 과세방식 비교 ────────────────────────────────────────────

def test_1500만원_이하면_선택_대상이_아니다():
    out = compare_taxation_options(0, 1200)
    assert out["choice_required"] is False


def test_1500만원_초과시_전액이_과세대상이다():
    """함정 C1 — 초과분이 아니라 전액이다.
    연 2,000만원 → 2,000 × 16.5% = 330만원 (초과분 500만원 기준이면 82.5만원)"""
    out = compare_taxation_options(0, 2000)
    assert out["choice_required"] is True
    assert out["separate"]["사적연금_분리과세"] == pytest.approx(330.0)


def test_연금소득공제_한도는_900만원():
    assert _pension_income_deduction(100000) == 900


def test_종합소득세_누진구조():
    """속산식은 구간별 후보 중 최댓값을 취한다 (단위: 만원).
    1,000만원은 6% 구간(60), 2,000만원은 15% 구간(174)이 적용된다."""
    assert _f_comp(1000) == pytest.approx(60.0)
    assert _f_comp(2000) == pytest.approx(0.15 * 2000 - 126)
    assert _f_comp(100) == pytest.approx(6.0)


# ── 세율 기준 정규화 ─────────────────────────────────────────

def test_15퍼센트와_16_5퍼센트는_같은_세율의_다른_표기():
    """함정 — 상충이 아니라 지방소득세 포함 여부 차이 (15 × 1.1 = 16.5)"""
    out = normalize_tax_rate(0.165, includes_local_tax=True)
    assert out["national_rate"] == pytest.approx(0.15)
    out2 = normalize_tax_rate(0.15, includes_local_tax=False)
    assert out2["rate_with_local_tax"] == pytest.approx(0.165)


# ── 상품 적합성 ──────────────────────────────────────────────

def test_계좌유형에_맞지_않는_클래스는_가입_불가():
    assert check_class_eligibility("C-P", "연금저축")["eligible"] is True
    assert check_class_eligibility("C-P", "IRP")["eligible"] is False
    assert check_class_eligibility("C-Re", "IRP")["eligible"] is True


def test_직판_전용_클래스는_일반개인_가입불가():
    """함정 D1 — 총보수 최저 클래스가 가입 불가인 경우가 많다."""
    assert check_class_eligibility("C-RJ", "연금저축")["eligible"] is False


def test_총보수_비교는_가입가능한_클래스끼리만():
    candidates = [
        {"fund_class": "C-RJ", "total_expense": 0.3490},   # 직판 전용 — 제외돼야 함
        {"fund_class": "C-Pe", "total_expense": 0.4390},
        {"fund_class": "C-P", "total_expense": 0.5440},
    ]
    out = compare_total_expense(candidates, "연금저축")
    assert [c["fund_class"] for c in out["comparable"]] == ["C-Pe", "C-P"]
    assert out["excluded"][0]["fund_class"] == "C-RJ"


# ── 국민연금 ─────────────────────────────────────────────────

def test_국민연금_본인부담금은_보험료의_절반():
    assert calc_np_copay(300)["C_np_copay"] == pytest.approx(300 * 0.09 * 0.5)


def test_출산크레딧_인정개월():
    assert calc_np_birth_credit_months(2, 0, 2, 0)["M_np_credit"] == 48
    assert calc_np_birth_credit_months(1, 0, 0, 1)["M_np_credit"] == 12


def test_국민연금_수령액():
    out = calc_np_benefit(300, 0.4)
    assert out["P_np_monthly"] == pytest.approx(120.0)
    assert out["P_np_annual"] == pytest.approx(1440.0)


# ── 구법 탐지 ────────────────────────────────────────────────

def test_구법_수치를_탐지한다():
    """함정 C5 — 700만원/1200만원/4천만원은 개정 전 수치."""
    out = detect_legacy_tax_content("연 700만원 중 적은 금액을 한도로 한다", "OLD")
    assert out["is_legacy_suspect"] is True
    assert out["markers"][0]["marker"] == "700만원"


def test_현행_수치는_구법으로_잡지_않는다():
    out = detect_legacy_tax_content("연 900만원을 한도로 세액공제한다", "NEW")
    assert out["is_legacy_suspect"] is False


# ── 근거 표기 ────────────────────────────────────────────────

def test_문서기반_계산은_source를_함께_반환한다():
    """근거 추적을 위해 반환 dict에 source가 있어야 한다."""
    assert calc_withdrawal_limit(10000, 1)["source"] == "doc39"
    assert calc_pension_year(False, 0)["source"] == "doc39"
    assert calc_retirement_tax_reduction(5)["source"] == "doc40"
    assert calc_retirement_income_tax(10000, 10)["source"] == "doc52"
