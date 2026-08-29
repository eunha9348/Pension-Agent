"""compare_taxation_options() 결과 렌더링 — 코드처럼 보이던 것을 문장으로.

━━ 실측 결함 (2026-08-29, 사용자 직접 지적) ━━
"연간 연금수령액 2,000만원" 질의의 답변에 아래처럼 raw dict가 그대로
찍혔다:

    separate:
      사적연금_분리과세 = 330만원
      그외_종합과세 = 0만원
      합계 = 330만원
    comprehensive:
      과세표준 = 1,160만원
      ...

원인: render_calc_result()는 모든 계산함수에 공용인 key=value 나열
렌더러다. "separate"·"comprehensive"는 _LABELS에 없는 raw 영문 키라
번역 없이 그대로 노출됐다. 이 함수만 유일하게 선택지 두 개를 비교하는
중첩 구조를 반환하므로, 이것만 전용 문장 렌더러(_render_tax_choice)로
분리했다.

⚠️ 숫자는 전부 계산 결과 dict에서 그대로 가져온다. 이 테스트가 지키는
불변식은 "숫자가 안 바뀌었는가"이지 "문장이 예쁜가"가 아니다 —
문장이 아무리 매끄러워도 숫자가 달라지면 그게 진짜 결함이다.
"""

from __future__ import annotations

from app.core.pension_calc_functions import compare_taxation_options
from app.generation.render import render_calc_result


def test_raw_영문키가_노출되지_않는다():
    """★ 실측 사고 재현 — separate:/comprehensive:가 번역 없이 그대로 나갔다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000)
    text = render_calc_result(r)

    assert "separate:" not in text
    assert "comprehensive:" not in text
    assert "사적연금_분리과세 =" not in text     # 언더스코어 원본 키
    assert "그외_종합과세 =" not in text


def test_숫자는_계산_결과와_정확히_같다():
    """문장으로 바뀌어도 수치는 dict 값 그대로여야 한다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000)
    text = render_calc_result(r)

    assert "330만원" in text                    # separate.합계
    assert "1,160만원" in text                   # comprehensive.과세표준
    assert "690만원" in text                     # comprehensive.연금소득공제
    assert "77만원" in text                      # comprehensive.합계 (76.56 반올림)
    assert "253만원" in text                     # difference (253.44 반올림)
    assert "종합과세" in text                    # lower_tax_option=COMPREHENSIVE


def test_선택_대상이_아니면_한_줄로_안내한다():
    """1,500만원 이하는 비교할 게 없다 — note 문장 하나로 충분하다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=1200)
    text = render_calc_result(r)

    assert "선택" in text or "종결" in text
    assert "separate" not in text
    assert "choice_required" not in text


def test_한계고지는_그대로_유지된다():
    """⚠️ 필드(제공문서 외 기준이라는 고지)가 문장 전환 과정에서 빠지면 안 된다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000)
    text = render_calc_result(r)

    assert "제공문서 외" in text
    assert "확인 필요" in text


def test_템플릿_답변에서도_동일하게_반영된다():
    """★ 배선 — render_template_answer()를 거쳐도 문장형이 유지되는가."""
    from app.core.coverage_pipeline import RequirementSlot, SlotStatus
    from app.generation.answer_prompt import render_template_answer

    slot = RequirementSlot("t1", "1,500만원 초과 시 과세방식 선택", "calculation",
                           calc_function="과세방식_비교_계산")
    slot.status = SlotStatus.CALC_DONE
    slot.calc_result = compare_taxation_options(
        P_np_annual=0, P_private_pension_annual=2000)

    spec = {"query": "연간 연금수령액 2,000만원 받는데 세금이 어떻게 되나요?",
            "user_conditions": {"private_pension_annual_manwon": 2000}}
    out = render_template_answer(spec, [], [slot])

    assert "separate:" not in out
    assert "77만원" in out


# ════════════════════════════════════════════════════════════════
# 주제 매칭 — 1,500만원을 모르는 사용자도 계산에 도달해야 한다
# ════════════════════════════════════════════════════════════════
#
# 과세방식 규칙의 키워드는 "분리과세·종합과세·1500"뿐이었다. 그런데
# **1,500만원 기준선을 모르는 사용자는 그 말을 쓰지 않는다** — 모르니까
# 묻는 것이다. "연 2,000만원 받는데 세금 어떻게 되나요?"가 주제 미매칭으로
# 떨어져 계산이 통째로 안 돌았다(2026-08-29 실측).
#
# 그래서 조건(연간 수령액 > 1,500만원)으로도 발동하게 했다. 이 문턱은
# 제공문서에 있는 확정 수치이고, private_pension_annual_manwon은
# "연/연간 ○○ 받|수령|나오" 패턴에서만 나오므로 납입액·평가액과 섞이지
# 않는다. 실측: 298건 중 새로 발동한 것은 1건(L26)이고 진짜 양성이었다.

def _fires(question: str) -> bool:
    from app.analysis.query_spec import rule_based_spec

    return "과세방식_비교_계산" in [
        c["function"] for c in rule_based_spec(question)["planned_calls"]]


def test_금액만_말해도_과세방식_계산이_돈다():
    """★ 실측 사고 재현 — 사용자는 '1500'이라는 말을 쓰지 않는다."""
    assert _fires("연간 연금수령액 2,000만원 받는데 세금이 어떻게 되나요?")
    assert _fires("연 2000만원 수령하는데 세금 어떻게 되나요?")


def test_월수령액으로_말해도_연환산으로_판정한다():
    """월 200만원 = 연 2,400만원 — 문턱을 넘는다."""
    assert _fires("매달 200만원씩 받는데 세금은요?")


def test_문턱_이하면_발동하지_않는다():
    """연 1,200만원은 저율 원천징수로 종결 — 선택 대상이 아니다."""
    assert not _fires("연 1200만원 받는데 세금이 어떻게 되나요?")


def test_납입액과_평가액은_수령액이_아니다():
    """★ 오탐 방지 — 같은 2,000만원이라도 용도가 다르면 발동하면 안 된다."""
    assert not _fires("연금저축에 2000만원 납입했는데 세액공제는?")
    assert not _fires("IRP 평가액이 2억인데 세금은?")


def test_키워드_경로는_그대로_동작한다():
    """조건 트리거를 추가해도 기존 키워드 매칭이 죽으면 안 된다."""
    assert _fires("연금소득이 1500만원 넘으면 어떻게 되나요?")
    assert _fires("분리과세가 유리한가요 종합과세가 유리한가요?")
