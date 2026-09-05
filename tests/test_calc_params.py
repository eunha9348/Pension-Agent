"""calc_params_builder 회귀 테스트.

이 파일의 존재 이유: 이 빌더가 없으면 계산함수 15종이 **하나도** 호출되지
않는다. 따라서 "15종 전부가 실제로 호출 가능한가"를 테스트로 못 박는다.
"""

from __future__ import annotations

import pytest

from app.analysis.calc_params import (CALC_PARAM_SPECS, MissingCalcParams,
                                      make_calc_params_builder, remap_function)
from app.analysis.conditions import derive_conditions
from app.analysis.units import parse_amount_to_manwon
from app.core.coverage_pipeline import (CALC_REGISTRY, RequirementSlot,
                                        SlotStatus, run_calculations)


def _slot(fn: str, desc: str = "테스트") -> RequirementSlot:
    s = RequirementSlot(slot_id=f"s_{fn}", description=desc,
                        slot_type="calculation", calc_function=fn)
    s.status = SlotStatus.CALC_PENDING
    return s


# ════════════════════════════════════════════════════════════════
# 규격 커버리지
# ════════════════════════════════════════════════════════════════

def test_registry_15종이_전부_인자규격을_갖는다():
    """DEPRECATED 1종(과세방식_판정_계산)은 remap으로 대체되므로 제외."""
    covered = set(CALC_PARAM_SPECS) | {"과세방식_판정_계산"}
    missing = set(CALC_REGISTRY) - covered
    assert not missing, f"인자 규격이 없는 계산함수: {missing}"
    assert len(CALC_REGISTRY) == 15


def test_deprecated_함수는_현행함수로_교정된다():
    assert remap_function("과세방식_판정_계산") == "과세방식_비교_계산"


def test_인자규격의_이름이_실제_함수_시그니처와_일치한다():
    import inspect
    for fn_name, specs in CALC_PARAM_SPECS.items():
        sig = inspect.signature(CALC_REGISTRY[fn_name])
        actual = set(sig.parameters)
        declared = {s.name for s in specs}
        assert declared <= actual, (
            f"{fn_name}: 규격에 없는 인자 {declared - actual}")
        required = {n for n, p in sig.parameters.items()
                    if p.default is inspect.Parameter.empty}
        assert required <= declared, (
            f"{fn_name}: 필수 인자 누락 {required - declared}")


# ════════════════════════════════════════════════════════════════
# 실제 계산 실행
# ════════════════════════════════════════════════════════════════

def test_연금수령한도_doc39_원문예시_재현():
    """doc39: 1억·1년차 → 1,200만원 (평가액 ÷ (11-연차) × 120%)"""
    cond = derive_conditions("계좌에 1억원 있고 연금수령 1년차인데 얼마까지 뽑을 수 있나요?")
    assert cond["account_value_manwon"] == 10000
    assert cond["pension_year"] == 1

    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("연금수령한도_계산")], builder)
    assert slots[0].status == SlotStatus.CALC_DONE
    assert slots[0].calc_result["limit"] == pytest.approx(1200.0)


def test_퇴직소득세_doc52_원문예시_재현():
    """doc52: 근속 25년 → 근속연수공제 5,500만원"""
    cond = derive_conditions("퇴직금 2억원 받았고 근속 25년입니다. 퇴직소득세 얼마인가요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("퇴직소득세_계산")], builder)
    assert slots[0].status == SlotStatus.CALC_DONE
    assert slots[0].calc_result["근속연수공제"] == pytest.approx(5500.0)


def test_연봉만_있고_퇴직급여_총액이_없으면_되묻는다():
    """UI-013 실사용 재현 (2026-09-06).

    "근속 25년차이고 연봉 8천만원인데 DC로 퇴직금 얼마나 받을 수 있나요"에서
    연봉 8,000만원이 severance_pay로 새 나가 calc_retirement_income_tax가
    근거 없는 세금을 계산했다. severance_pay는 amount_manwon(문맥 없는
    범용 단일 금액)을 폴백으로 쓰지 않으므로, severance_manwon이 없으면
    계산 대신 되묻기로 강등돼야 한다.
    """
    cond = derive_conditions("근속 25년차이고 연봉 8천만원인데 DC로 퇴직금 얼마나 받을 수 있나요")
    assert "severance_manwon" not in cond
    assert cond["amount_manwon"] == 8000        # 캐치올 자체는 여전히 채워짐(다른 슬롯용)

    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("퇴직소득세_계산")], builder)
    assert slots[0].status == SlotStatus.MISSING
    assert "severance_pay" not in (slots[0].calc_result or {})


def test_소득_모르면_세액공제율_두_경우를_모두_계산한다():
    """조건을 모르면 단정하지 않고 나눠서 계산 — '조건별 결론'의 재료."""
    cond = derive_conditions("연금저축에 600만원, IRP에 300만원 넣으면 세액공제 얼마인가요?")
    assert cond["pension_saving_manwon"] == 600
    assert cond["irp_manwon"] == 300

    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("사적연금_납입한도_세액공제_계산")], builder)
    result = slots[0].calc_result
    assert slots[0].status == SlotStatus.CALC_DONE
    assert len(result["variants"]) == 2
    # 합산한도 900만원 × 16.5% = 148.5만원 / × 13.2% = 118.8만원
    by_label = {v["label"]: v["result"]["A_tax_credit"] for v in result["variants"]}
    assert pytest.approx(148.5) in by_label.values()
    assert pytest.approx(118.8) in by_label.values()


def test_소득을_알면_단일_세액공제율로_계산한다():
    cond = derive_conditions(
        "총급여 4,000만원이고 연금저축에 600만원 넣었습니다. 세액공제 얼마인가요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("사적연금_납입한도_세액공제_계산")], builder)
    result = slots[0].calc_result
    assert "variants" not in result
    assert result["A_tax_credit"] == pytest.approx(600 * 0.165)


def test_필수조건_없으면_MISSING으로_강등되고_확인문구가_남는다():
    cond = derive_conditions("연금수령한도가 어떻게 되나요?")   # 평가액·연차 없음
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("연금수령한도_계산")], builder)

    assert slots[0].status == SlotStatus.MISSING       # ASK_BACK 유도
    asks = builder.ask_back_items()
    assert asks and len(asks) <= 2                     # 확인 항목 최대 2건
    assert any("평가액" in a for a in asks)


def test_감면율_계산은_연금수령연차를_실제수령연차로_대체하지_않는다():
    """함정 B1 — 두 연차는 다른 개념이므로 대체 금지.

    "11년차"라고만 한 질의에서 퇴직소득세 감면율을 계산하면 안 되고,
    연금실제수령연차를 되물어야 한다."""
    cond = derive_conditions("연금 11년차인데 퇴직소득세 40% 감면되나요?")
    assert cond.get("pension_year") == 11
    assert "actual_receipt_year" not in cond          # 대체 추론 금지

    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("퇴직소득세_감면율_계산")], builder)
    assert slots[0].status == SlotStatus.MISSING
    assert any("실제로 인출한" in a for a in builder.ask_back_items())


def test_실제수령연차를_명시하면_계산된다():
    cond = derive_conditions("연금 개시하고 실제로 인출한 게 11년차입니다. 감면율이 얼마인가요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("퇴직소득세_감면율_계산")], builder)
    assert slots[0].status == SlotStatus.CALC_DONE
    assert slots[0].calc_result["reduction_rate"] == 0.40


def test_기본값_사용은_가정으로_기록된다():
    cond = derive_conditions("연금저축에서 매달 200만원씩 받으면 세금 어떻게 되나요?")
    builder = make_calc_params_builder(cond)
    run_calculations([_slot("과세방식_비교_계산")], builder)
    notes = builder.assumption_items()
    assert any("국민연금" in n for n in notes)      # 0으로 계산했다는 사실이 남는다


# ════════════════════════════════════════════════════════════════
# '연간'의 축약형 '연'도 연간수령액으로 잡혀야 한다 (300건 감사 회귀)
# ════════════════════════════════════════════════════════════════
# "연 1200만원 받으면"처럼 '연간'을 줄인 맨 '연'이 훨씬 흔한데, 이 표현이
# 없어서 원천징수 계산에 쓸 금액이 통째로 안 잡혔다(실측 감사 L10·L11·L12
# 3건 전부 "월 수령액이 확인되지 않아 세율만 안내합니다"로 계산이 비었다).
# '국민연금'·'연령'처럼 '연' 뒤에 공백 없는 복합어는 오매칭되지 않아야 한다.

def test_연간의_축약형_연도_연간수령액으로_잡힌다():
    cond = derive_conditions("만 65세가 연금으로 연 1200만원 받으면 세금은 얼마인가요?")
    assert cond.get("private_pension_annual_manwon") == 1200


def test_연간_전체형도_그대로_잡힌다():
    cond = derive_conditions("사적연금으로 연간 1200만원 받으면 세금은 얼마인가요?")
    assert cond.get("private_pension_annual_manwon") == 1200


@pytest.mark.parametrize("question", [
    "국민연금은 몇 살부터 받을 수 있나요?",
    "1969년생은 국민연금 수급개시 연령이 몇 세인가요?",
])
def test_연_뒤에_공백없는_복합어는_연간수령액으로_오매칭되지_않는다(question):
    cond = derive_conditions(question)
    assert "private_pension_annual_manwon" not in cond


# ════════════════════════════════════════════════════════════════
# 한도 질의는 막지 말고 계산한다 (E-01 회귀)
# ════════════════════════════════════════════════════════════════
# 예전에는 REQUIRE_ANY가 납입액 없는 질의의 계산을 통째로 막았다. 그러면
# "얼마까지 받을 수 있나요"에 600·900이 답변에 실릴 보장이 사라진다.
# 한도는 상수이므로 계산해서 내보내고, 의미 없는 세액공제액만 빼는 게 맞다.

def test_납입액_없는_한도질의도_계산까지_간다():
    cond = derive_conditions("연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("사적연금_납입한도_세액공제_계산")], builder)
    assert slots[0].status == SlotStatus.CALC_DONE


def test_한도질의_렌더링에_600과_900이_실린다():
    """LLM이 숫자를 빠뜨려도 결정론적 계산 결과에는 남아 있어야 한다."""
    from app.generation.render import render_calc_result

    cond = derive_conditions("연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("사적연금_납입한도_세액공제_계산")], builder)
    text = render_calc_result(slots[0].calc_result)

    assert "600" in text
    assert "900" in text
    assert "700만원" not in text        # 구법 수치가 섞이면 안 된다
    assert "1,200만원" not in text


def test_한도만_안내할_때_세액공제액_0원이_찍히지_않는다():
    from app.generation.render import render_calc_result

    cond = derive_conditions("연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?")
    builder = make_calc_params_builder(cond)
    slots = run_calculations([_slot("사적연금_납입한도_세액공제_계산")], builder)
    text = render_calc_result(slots[0].calc_result)

    assert "세액공제액 = 0만원" not in text


def test_미등록_함수는_예외로_강등된다():
    builder = make_calc_params_builder({})
    with pytest.raises(MissingCalcParams):
        builder(_slot("존재하지_않는_함수"))


# ════════════════════════════════════════════════════════════════
# 단위 변환 — 1만배 오차 방지
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("1억", 10000),
    ("1억 2천만원", 12000),
    ("5,000만원", 5000),
    ("50,000,000원", 5000),
    ("300만원", 300),
    ("1,500만원", 1500),
])
def test_금액_파싱_만원단위(text, expected):
    assert parse_amount_to_manwon(text) == pytest.approx(expected)


def test_단위없는_맨숫자는_금액으로_해석하지_않는다():
    """'1500'을 1500원으로 읽으면 1만배 오차가 난다. 해석을 거부하는 게 맞다."""
    assert parse_amount_to_manwon("1500") is None
    assert parse_amount_to_manwon("연금 10년차") is None


# ════════════════════════════════════════════════════════════════
# 부분 계산 — 산출 가능한 것은 내준다
# ════════════════════════════════════════════════════════════════
#
# 실사고(평가 E-14): "만 80세인데 세금 몇 퍼센트 떼나요"는 **요율**을 묻는데,
# 빌더가 월 수령액을 필수로 요구해 계산을 통째로 접었다. 그 결과 답변에서
# 3.3%가 빠졌다. 세율은 나이·수령형태만으로 정해지고 금액은 세액에만 쓰인다.

def test_금액이_없어도_원천징수_세율은_나온다():
    from app.analysis.calc_params import make_calc_params_builder
    from app.core.coverage_pipeline import CALC_REGISTRY, RequirementSlot

    slot = RequirementSlot("w", "원천징수율", "calculation",
                           calc_function="사적연금_원천징수_계산")
    b = make_calc_params_builder({"age": 80, "is_annuity_type": False})
    result = CALC_REGISTRY["사적연금_원천징수_계산"](**b(slot))
    assert result["r_withholding"] == 0.033


def test_금액이_없으면_세액을_단정하지_않는다():
    """0원을 답으로 내놓으면 맞지만 오해를 부른다 — 한계를 밝혀야 한다."""
    from app.analysis.calc_params import make_calc_params_builder
    from app.core.coverage_pipeline import RequirementSlot

    slot = RequirementSlot("w", "원천징수율", "calculation",
                           calc_function="사적연금_원천징수_계산")
    b = make_calc_params_builder({"age": 80, "is_annuity_type": False})
    b(slot)
    assert any("세율만" in a for a in b.assumption_items())


def test_금액이_있으면_세액까지_계산한다():
    from app.analysis.calc_params import make_calc_params_builder
    from app.core.coverage_pipeline import CALC_REGISTRY, RequirementSlot

    slot = RequirementSlot("w", "원천징수", "calculation",
                           calc_function="사적연금_원천징수_계산")
    b = make_calc_params_builder({"age": 80, "is_annuity_type": False,
                                  "private_pension_monthly_manwon": 100})
    result = CALC_REGISTRY["사적연금_원천징수_계산"](**b(slot))
    assert result["T_withholding"] > 0
    assert b.assumption_items() == []


# ════════════════════════════════════════════════════════════════
# LLM 출력의 비정상 값이 파이프라인을 죽이지 않는다
# ════════════════════════════════════════════════════════════════
#
# 실사고: 프롬프트 탈취 질의(E-18)에서 L1이 내놓은 JSON의 만원 필드에
# 마크다운 조각("**") 같은 비정상 문자열이 섞여 들어왔다. 검증 없이
# conditions에 저장했더니 한참 뒤 format_manwon()이 float()으로 바꾸다
# 죽었고, 그 예외가 위로 뚫고 나가 평가 전체가 중단됐다.

def test_LLM이_만원_필드에_문자열을_줘도_죽지_않는다():
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("질문", llm_conditions={"account_value_manwon": "**"})
    assert "account_value_manwon" not in c


def test_LLM이_나이에_문자열을_줘도_죽지_않는다():
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("질문", llm_conditions={"age": "여든"})
    assert "age" not in c


def test_정상_숫자는_그대로_들어간다():
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("질문", llm_conditions={"age": 80,
                                                "account_value_manwon": 10000})
    assert c["age"] == 80.0
    assert c["account_value_manwon"] == 10000.0


def test_bool은_숫자_필드로_들어가지_않는다():
    """isinstance(True, int)는 True다 — bool이 age로 새면 조용히 틀린다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("질문", llm_conditions={"age": True})
    assert "age" not in c


def test_format_manwon은_문자열_입력에도_죽지_않는다():
    """검증을 어디서든 뚫고 들어올 경우의 최후 방어선."""
    from app.analysis.units import format_manwon

    assert format_manwon("**") == "—"
    assert format_manwon(None) == "—"


def test_LLM_age가_정수로_표시된다():
    """80.0세가 아니라 80세여야 한다."""
    from app.analysis.conditions import describe_conditions, derive_conditions

    c = derive_conditions("질문", llm_conditions={"age": 80})
    desc = describe_conditions(c)
    assert "80세" in desc
    assert "80.0세" not in desc


# ════════════════════════════════════════════════════════════════
# 단위 혼동 — L1 금액이 규칙 파싱을 덮어써 1만배 오차를 내면 안 된다
# ════════════════════════════════════════════════════════════════
#
# 계산함수는 전부 만원 단위다(CLAUDE.md). L1은 "1억 원"을 10000으로 줘야 할
# 자리에 1(억)이나 100000000(원)을 주기도 하는데, 예전에는 그 값이 규칙
# 파싱 결과를 무조건 덮어써 "연금수령한도 0만원" 같은 **확신에 찬 오답**이
# 나갔다. 규칙 파싱은 사용자가 쓴 글자를 명시적 단위 변환으로 읽은 것이므로,
# 자릿수가 크게 어긋나면 규칙 쪽을 믿는다.

_Q_1억 = "계좌에 1억 원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?"


def test_L1이_억_단위로_착각해도_규칙값을_지킨다():
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(_Q_1억, llm_conditions={"account_value_manwon": 1})
    assert c["account_value_manwon"] == 10000.0


def test_L1이_원_단위로_착각해도_규칙값을_지킨다():
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(_Q_1억,
                          llm_conditions={"account_value_manwon": 100000000})
    assert c["account_value_manwon"] == 10000.0


def test_버린_사실을_한계고지에_남긴다():
    """조용히 버리면 왜 그 값이 안 쓰였는지 추적할 수 없다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(_Q_1억, llm_conditions={"account_value_manwon": 1})
    assert any("자릿수" in n for n in c.get("condition_notes", []))


def test_같은_자릿수_이견은_L1을_존중한다():
    """단위 사고만 막는 것이지 L1 판단을 통째로 무시하는 것이 아니다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(_Q_1억, llm_conditions={"account_value_manwon": 12000})
    assert c["account_value_manwon"] == 12000.0


def test_규칙이_못_읽은_금액은_L1_값을_쓴다():
    """비교 대상이 없으면 L1이 유일한 정보원이다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("연금 세금이 궁금해요",
                          llm_conditions={"severance_manwon": 20000})
    assert c["severance_manwon"] == 20000.0


# ════════════════════════════════════════════════════════════════
# 단위 통일 — 만원 단일 (2026-08-29 R6)
# ════════════════════════════════════════════════════════════════
# 계산함수 15종은 전부 만원 단위다. 원 단위 값이 인자로 새어 들어가면
# **1만배 오차**가 난다 — 600만원을 원으로 쓴 6,000,000이 통과하면
# 60조원으로 계산된다. 상한이 그 차단선이다.

@pytest.mark.parametrize("name,value", [
    ("account_value", 100_000_000),       # 1억원을 원으로
    ("severance_pay", 200_000_000),       # 2억원을 원으로
    ("X_pension_saving", 6_000_000),      # 600만원을 원으로
    ("Y_irp_personal", 9_000_000),        # 900만원을 원으로
    ("P_private_monthly", 1_000_000),     # 100만원을 원으로
    ("P_np_annual", 12_000_000),          # 1200만원을 원으로
])
def test_원_단위_유입은_경계에서_차단된다(name, value):
    from app.analysis.calc_params import validate_param

    assert validate_param(name, value) is not None, \
        f"{name}={value:,} 가 통과했다 — 1만배 오차가 난다"


@pytest.mark.parametrize("name,value", [
    ("account_value", 10_000),        # 1억
    ("account_value", 1_000_000),     # 100억 — 상한 경계
    ("severance_pay", 20_000),        # 2억
    ("X_pension_saving", 600),
    ("X_pension_saving", 1_800),      # 연간 총 납입한도 = 상한 경계
    ("Y_irp_personal", 900),
    ("P_private_monthly", 100),
    ("P_private_pension_annual", 1_200),
])
def test_정상_만원_값은_통과한다(name, value):
    from app.analysis.calc_params import validate_param

    assert validate_param(name, value) is None, \
        f"{name}={value:,} 가 차단됐다 — 정상 범위인데 되묻게 된다"


def test_모든_금액_인자에_상한이_있다():
    """상한이 None이면 원 단위 유입을 못 막는다."""
    from app.analysis.calc_params import _VALID

    amount_params = ("account_value", "severance_pay", "X_pension_saving",
                     "Y_irp_personal", "P_private_monthly",
                     "P_private_pension_annual", "P_np_annual",
                     "I_monthly", "I_final_monthly")
    for name in amount_params:
        low, high, _ask = _VALID[name]
        assert high is not None, f"{name}에 상한이 없다 — 원 단위가 새어 든다"


def test_차단되면_되묻는다_계산을_강행하지_않는다():
    """틀린 숫자를 내는 것보다 물어보는 편이 낫다."""
    from app.analysis.calc_params import make_calc_params_builder

    builder = make_calc_params_builder({"account_value_manwon": 100_000_000,
                                        "pension_year": 1})
    with pytest.raises(MissingCalcParams):
        builder(_slot("연금수령한도_계산"))


def test_입력_경계가_항상_만원으로_변환한다():
    """사용자는 원·만원·억을 섞어 쓴다. 함수에 닿을 때는 만원이어야 한다."""
    cases = [
        ("연금저축에 600만원 넣으면", "pension_saving_manwon", 600),
        ("연금저축에 6000000원 넣으면", "pension_saving_manwon", 600),
        ("계좌에 1억원 있고", "account_value_manwon", 10000),
        ("계좌에 1억 2천만원 있고", "account_value_manwon", 12000),
        ("계좌에 100000000원 있고", "account_value_manwon", 10000),
        ("퇴직금 2억원 받았고", "severance_manwon", 20000),
        ("총급여 5500만원", "total_income_manwon", 5500),
        ("총급여 55000000원", "total_income_manwon", 5500),
    ]
    for q, key, expected in cases:
        assert derive_conditions(q).get(key) == expected, q
