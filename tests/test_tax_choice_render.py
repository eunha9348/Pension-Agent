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
    assert "76.6만원" in text                    # comprehensive.합계 (76.56, 소수 첫째자리)
    assert "253.4만원" in text                   # difference (253.44, 소수 첫째자리)
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
    assert "76.6만원" in out


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


# ════════════════════════════════════════════════════════════════
# 표시 반올림 ↔ 수치 검증 — 시스템이 표시한 값을 날조로 몰면 안 된다
# ════════════════════════════════════════════════════════════════
#
# ⚠️ 2026-09-06 변경 — format_manwon이 정수로 반올림하던 것을 소수점
#    첫째 자리까지만 보이도록 바꿨다(76.56 → "77만원"이 아니라 "76.6만원").
#    정수 반올림 폭이 컸을 때는 그 차이(76.56 vs 77 = 0.575%)가 수치
#    검증의 상대오차 허용(0.5%)을 간발의 차로 넘어, **시스템이 스스로
#    표시한 값**이 '근거 없는 수치'로 잡혀 답변이 통째로 축퇴한 사고가
#    있었다(2026-08-29 실측). 반올림 폭을 소수 첫째 자리로 줄이면 그
#    오차 자체가 훨씬 작아진다(76.56 vs 76.6 = 0.05%).
#
#    다만 안전망은 그대로 둔다 — numeric_verifier._flatten_numbers가
#    format_manwon의 실제 출력을 허용 집합에 그대로 넣으므로, 표시
#    정밀도를 다시 바꾸더라도 같은 사고가 재발하지 않는다.
#
# ⚠️ 이건 허용 오차를 늘리는 것과 **다르다.** render_calc_result가 표시한
#    값은 계산함수 출력에서 결정론적으로 파생된 것이므로 정의상 근거가 있다.
#    오차를 키우면 진짜 날조까지 통과하므로 그 길로 가면 안 된다.

_CALC = [{"합계": 76.56, "과세표준": 1160.0, "source": "doc39"}]


def _passes(answer: str) -> bool:
    from app.core.numeric_verifier import verify_numeric_grounding

    return verify_numeric_grounding(answer, calc_results=_CALC).passed


def test_표시_반올림값은_근거있는_수치다():
    """★ 실측 사고 재현 — 76.56을 '76.6만원'으로 표시한 것이 날조로 잡히면 안 된다."""
    assert _passes("종합과세 세액은 76.6만원입니다")


def test_원본값과_정수값도_그대로_통과한다():
    assert _passes("76.56만원입니다")
    assert _passes("과세표준은 1,160만원입니다")


def test_진짜_날조는_여전히_잡힌다():
    """★ 완화가 검증을 무력화하면 안 된다 — 이쪽이 더 중요하다."""
    assert not _passes("세액은 9,999만원입니다")
    assert not _passes("세액은 100만원입니다")
    assert not _passes("세액은 770만원입니다")      # 자릿수만 다른 값
    assert not _passes("세액은 80만원입니다")       # 가깝지만 다른 값


def test_과세방식_답변이_축퇴되지_않는다():
    """★ 배선 — 파이프라인 끝까지 가서 축퇴가 사라졌는지 본다.

    축퇴되면 L5'가 쓴 문장이 버려지고 템플릿 불릿 나열이 나간다.
    """
    from app.pipeline import answer_question

    r = answer_question(
        "ROUND-1", "연금 1년차인데 연간 연금수령액 2,000만원 받으면 세금이 어떻게 되나요?")
    assert "수치검증_실패" not in r["think_trace"], (
        "표시 반올림 때문에 답변이 축퇴됐다")
    assert "76.6만원" in r["answer"]


# ════════════════════════════════════════════════════════════════
# F37 · 종합과세 과세표준 산출 근거에 인적공제가 빠져 있던 결함 (UI-037)
# ════════════════════════════════════════════════════════════════
#
# 실측 (2026-09-06) — "총 9000만원에서 연금소득공제 690만원을 빼면
# 8310만원인데 왜 8160만원이냐, 150만원은 어디서 빠졌냐"는 질문에 답변이
# "그렇게 되는 것입니다"만 반복하고 정확한 근거를 대지 못했다("추가 정보
# 필요"로 얼버무림).
#
# 원인: compare_taxation_options()가 실제로는
# 총연금및기타소득(9,000) − 연금소득공제(690) − 인적공제(150) = 8,160
# 을 계산하는데, personal_deduction(인적공제 150만원)이 함수 내부
# 지역변수로만 쓰이고 반환값 어디에도 없었다. 그래서 답변을 만드는 LLM도
# 이를 검증하는 numeric_verifier도 150만원의 존재 자체를 알 수 없었다 —
# 계산은 처음부터 맞았지만, **그 계산을 설명하는 데 필요한 중간값이
# 출력에서 빠져** 사용자 질문에 답할 수 없었던 경우다.

def test_종합과세_결과에_총소득과_인적공제가_노출된다():
    """★ 실측 재현 — 인적공제 150만원이 반환값 어디에도 없었다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    comp = r["comprehensive"]
    assert comp["총연금및기타소득"] == 9000
    assert comp["인적공제"] == 150.0
    # 산식이 실제로 성립해야 한다 — 셋을 더하면 원래 값으로 돌아온다
    assert (comp["총연금및기타소득"] - comp["연금소득공제"]
            - comp["인적공제"]) == comp["과세표준"] == 8160.0


def test_렌더링_문장이_150만원_공제를_명시한다():
    """★ 배선 — L5' 생성 프롬프트에 실리는 문장 자체에 인적공제가 보여야 한다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    text = render_calc_result(r)
    assert "9,000만원" in text
    assert "인적공제 150만원" in text
    assert "8,160만원" in text


def test_분리과세_쪽_인적공제도_함께_노출된다():
    """대조군 — separate 분기도 같은 personal_deduction을 쓰므로 함께 실어야 한다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    assert r["separate"]["인적공제"] == 150.0


# ════════════════════════════════════════════════════════════════
# F38 · 과세방식 비교 각 단계에 세율(%) 안내 추가 (사용자 요청)
# ════════════════════════════════════════════════════════════════
#
# 사용자 요청 — "각 단계에서 세율에 대한 퍼센트에이지 안내도 같이 해주면
# 좋겠다. 단, 로직을 해치거나 안전성을 해칠 위험이 조금이라도 있다면
# 하지 말아달라."
#
# 위험 검토:
# ① 사적연금 분리과세 몫은 DEFAULT_SEPARATE_TAX_RATE_LOCAL(16.5%)이라는
#    이미 쓰이던 **상수**를 그대로 노출할 뿐이다 — 새 판단이 없다.
# ② 그 외 소득 종합과세·종합과세 전체는 누진세율(6~45% 8단계)이라 단일
#    세율이 없다. 새 세율을 추정하지 않고, 이미 계산된 세액÷과세표준의
#    **실효세율**(나누기 한 번)만 파생한다 — "적용세율"이 아니라
#    "실효세율"이라 불러 이 값이 누진 구간과 다르다는 것을 그대로 드러낸다.
# ③ verify_calc_presence(_presence_targets)는 separate/comprehensive처럼
#    중첩된 dict를 재귀하지 않으므로(변형 없는 일반 dict의 값이 dict면
#    isinstance(value, (int, float)) 검사에서 걸러진다) 새 필드가 답변에
#    강제로 요구되지 않는다 — 기존에 과세표준·합계 등도 강제되지 않았던
#    것과 같은 동작이라 새로운 강제표기 위험이 없다.
# ④ verify_numeric_grounding(_flatten_numbers)은 모든 중첩값을 재귀하므로
#    새 퍼센트 수치도 허용 집합에 자동으로 들어간다 — 답변이 이 값을
#    인용해도 '근거 없는 수치'로 잡히지 않는다(아래 테스트로 실측 확인).

def test_사적연금_분리과세_세율이_노출된다():
    """상수를 그대로 노출하므로 항상 16.5%다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    assert r["separate"]["사적연금_적용세율"] == 0.165


def test_누진구간_실효세율은_세액을_과세표준으로_나눈_값이다():
    """★ 새 판단이 아니라 이미 나온 두 숫자의 비율이어야 한다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    comp = r["comprehensive"]
    assert comp["실효세율"] == round(comp["합계"] / comp["과세표준"], 6)

    sep = r["separate"]
    assert sep["그외소득_실효세율"] == round(sep["그외_종합과세"] / sep["그외소득_과세표준"], 6)


def test_그외소득이_0이면_실효세율도_0이고_0으로_나누지_않는다():
    """★ 회귀 방지 — division by zero를 내면 안 된다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000)
    assert r["separate"]["그외소득_실효세율"] == 0.0
    assert r["separate"]["그외소득_과세표준"] == 0.0


def test_렌더링된_문장에_세율이_퍼센트로_보인다():
    """★ 배선 — 답변 문장 자체에 %가 찍혀야 사용자에게 도달한 것이다."""
    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    text = render_calc_result(r)
    assert "세율 16.5%" in text
    assert "실효세율 17.15%" in text
    assert "실효세율 18.64%" in text


def test_새_세율_수치가_근거없는_수치로_잡히지_않는다():
    """★ 안전성 확인 — 답변에 쓰인 새 %가 numeric_verifier를 통과해야 한다."""
    from app.core.numeric_verifier import verify_numeric_grounding

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    text = render_calc_result(r)
    v = verify_numeric_grounding(text, calc_results=[r],
                                 question="사적연금 2000만원, 그외소득 7000만원 분리과세 종합과세")
    assert v.passed, v.ungrounded


def test_새_필드가_계산결과_강제표기_대상에_새로_추가되지_않는다():
    """★ 안전성 확인 — verify_calc_presence가 nested dict를 재귀하지 않는지
    재확인한다. 재귀하면 이 세율들이 답변에 없을 때마다 강제로 요구돼
    불필요한 축퇴 위험이 새로 생긴다.
    """
    from app.core.numeric_verifier import verify_calc_presence

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    # 세율 얘기를 전혀 안 한 답변 — 그래도 강제 누락으로 잡히면 안 된다
    p = verify_calc_presence("세액 차이는 15.8만원입니다.", [r])
    assert p.passed, [m[0] for m in p.missing]
