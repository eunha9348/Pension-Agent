"""설계 대조 회귀 테스트.

CLAUDE.md에 적힌 설계 원칙이 **코드에서 실제로 지켜지는지**를 못 박는다.
전부 실제로 어긋나 있던 것을 고친 뒤 추가한 항목이다.
"""

from __future__ import annotations

import pytest

from app.analysis.conditions import derive_conditions
from app.analysis.units import parse_amount_expressions
from app.core.coverage_pipeline import EvidenceChunk, RequirementSlot, SlotStatus
from app.core.pension_calc_functions import calc_retirement_income_tax
from app.core.supervisory_board import (Verdict, _classify_key, audit_anomaly,
                                        audit_fitness)
from app.generation.grounding import make_verify_grounding
from app.llm.clova import llm_call_adapter


# ════════════════════════════════════════════════════════════════
# 원칙 · 결정론적 계층이 1차 방어선 (LLM 감사는 보완)
# ════════════════════════════════════════════════════════════════

def test_LLM이_없어도_결정론적_4대_감사는_돈다():
    """★ 예산 초과·LLM 장애 시 감독이 통째로 사라지던 결함의 회귀 테스트.

    설계상 결정론적 계층이 1차 방어선이고 LLM 감사가 보완인데,
    구현은 정반대로 LLM이 없으면 4대 감사까지 건너뛰었다."""
    verify = make_verify_grounding("질문", [], llm_call=None, citations=[object()])
    v = verify("이 상품이 가장 유리합니다. 추천드립니다.",
               [EvidenceChunk("d", "내용", score=1.0)])

    assert v.supervision is not None, "LLM이 없다고 감독이 사라지면 안 된다"
    assert v.supervision.verdict != Verdict.APPROVE
    assert any(f.code == "ASSERTIVE" for f in v.supervision.findings)


def test_의미감사_미수행_사실이_기록된다():
    """감사자가 응답을 못 준 것과 '문제없음'은 다르다."""
    verify = make_verify_grounding("질문", [], llm_call=None, citations=[object()])
    v = verify("정상적인 답변입니다.", [])
    assert any(f.code == "NOT_RUN" for f in v.supervision.findings)
    assert "의미 감사를 수행하지 않음" in v.as_trace()


def test_권한계층은_그대로_유지된다():
    verify = make_verify_grounding(
        "질문", [], llm_call=lambda s, u: '{"verdict":"APPROVE","findings":[]}',
        citations=[object()])
    v = verify("이 상품이 가장 유리합니다. 추천드립니다.", [])
    assert v.supervision.verdict != Verdict.APPROVE      # LLM이 완화하지 못한다


# ════════════════════════════════════════════════════════════════
# 원칙 · 감사자에게 판정 기준을 준다 (내부 지식에 의존하지 않는다)
# ════════════════════════════════════════════════════════════════

def test_의미감사_페이로드에_도메인_함정이_포함된다():
    """L6가 잡아야 할 건 '수치는 맞는데 설명이 틀린 경우'다.
    무엇을 조심할지 알려주지 않으면 감사자가 내부 지식에 의존하게 되는데,
    이 도메인은 법 개정이 잦아 그걸 신뢰할 수 없다."""
    captured = {}

    class Spy:
        is_mock = False
        def call(self, system, user, **kw):
            captured["user"] = user
            return '{"verdict":"APPROVE","findings":[]}'

    call = llm_call_adapter(Spy(), audit_context="· 연금수령연차와 연금실제수령연차는 다르다")
    call("감사자 프롬프트", "[질문]\n...")
    assert "도메인 유의사항" in captured["user"]
    assert "연금실제수령연차" in captured["user"]


def test_감사_독립성_생성과정은_전달하지_않는다():
    from app.core.supervisory_board import build_llm_audit_payload
    payload = build_llm_audit_payload(
        answer="답변", evidence_texts=["근거"], calc_results=[{"limit": 1200}],
        question="질문")
    assert "당신은 연금 상담 답변을 작성" not in payload      # L5' 프롬프트 미노출
    assert "심사 대상 답변" in payload


# ════════════════════════════════════════════════════════════════
# 이상치 감사 — 정답을 차단하지 않는다 / 이상치는 놓치지 않는다
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key,kind", [
    ("근속연수공제", "금액"),        # ← 5,500만원을 '근속연수 60 초과'로 오판했었다
    ("환산급여공제", "금액"),
    ("퇴직소득_과세표준", "금액"),
    ("산출세액", "금액"),
    ("pension_year", "연차"),        # ← 영문 키라 분류되지 않았다
    ("actual_receipt_year", "연차"),
    ("service_years", "근속연수"),
    ("r_withholding", "세율"),
    ("공제율", "세율"),
    ("tax_year", None),              # 과세연도는 연차가 아니다
])
def test_계산결과_키_분류(key, kind):
    assert _classify_key(key) == kind


def test_doc52_정답이_감독에서_차단되지_않는다():
    """근속 25년 → 근속연수공제 5,500만원 (doc52 원문 예시).
    이 정답이 BLOCK되어 템플릿으로 축퇴되던 오탐의 회귀 테스트."""
    result = calc_retirement_income_tax(20000, 25)
    assert audit_anomaly([result]) == []


@pytest.mark.parametrize("result,code", [
    ({"A_tax_credit": -50}, "NEGATIVE"),
    ({"r_withholding": 3.0}, "OUT_OF_RANGE"),
    ({"pension_year": 200}, "OUT_OF_RANGE"),
    ({"limit": 1_200_000_000}, "UNIT_SUSPECT"),
])
def test_진짜_이상치는_여전히_잡는다(result, code):
    assert any(f.code == code for f in audit_anomaly([result]))


# ════════════════════════════════════════════════════════════════
# 적합성 감사 — 함정 D1이 실제로 동작하는가
# ════════════════════════════════════════════════════════════════

def test_가입불가_상품_언급을_잡는다():
    """검색 후보를 그대로 넘기던 탓에 eligible 키가 없어
    이 감사가 한 번도 발동하지 않았다."""
    products = [{"fund_class": "C-RJ", "name": "C-RJ", "eligible": False,
                 "reason": "직판 전용"}]
    findings = audit_fitness("총보수가 낮은 C-RJ가 있습니다",
                             {"account_type": "연금저축"}, products, [])
    assert any(f.code == "INELIGIBLE_PRODUCT" for f in findings)


def test_상품을_언급하지_않은_답변은_강등되지_않는다():
    """검색 결과에 상품표가 있다는 이유만으로 등급이 강등되던 오탐."""
    findings = audit_fitness("연금저축 세액공제 한도는 900만원입니다", {}, [], [])
    assert not any(f.code == "UNVERIFIED_RECOMMENDATION" for f in findings)


def test_회피성_되묻기_감사가_활성화되어_있다():
    """partial_answer_possible이 전달되지 않아 영구 비활성이던 항목."""
    from app.core.supervisory_board import supervise
    r = supervise("확인이 필요합니다", ask_back_items=["a"], answerability="ASK_BACK",
                  partial_answer_possible=True, citations=[object()])
    assert any(f.code == "AVOIDABLE_ASKBACK" for f in r.findings)


# ════════════════════════════════════════════════════════════════
# 금액 파싱 — 자신 있게 틀린 숫자를 만들지 않는다
# ════════════════════════════════════════════════════════════════

def test_서로_다른_항목의_금액을_합치지_않는다():
    """★ 가장 위험했던 결함.

    "총급여 4000만원인데 연금저축에 600만원"이 4,600만원 하나로 합쳐져
    연금저축 납입액이 4,600만원으로 잡혔다. 한도(600만원)에 걸려 결과가
    우연히 맞는 경우가 있어 기존 테스트를 통과했다."""
    c = derive_conditions("총급여 4000만원인데 연금저축에 600만원 넣으면 세액공제 얼마인가요?")
    assert c["pension_saving_manwon"] == 600
    assert c["total_income_manwon"] == 4000


def test_세액공제율_구간이_뒤집히지_않는다():
    """소득 5,000만원 + 납입 600만원을 합쳐 5,600만원으로 읽으면
    총급여 5,500만원 기준을 넘겨 16.5%가 13.2%로 뒤집힌다."""
    c = derive_conditions("총급여 5000만원이고 연금저축에 600만원 납입했습니다")
    assert c["total_income_manwon"] == 5000

    from app.analysis.calc_params import _tax_credit_rate
    assert _tax_credit_rate(c) == 0.165


def test_계좌별_납입액을_각각_인식한다():
    c = derive_conditions("연금저축에 400만원 IRP에 300만원 넣었어요")
    assert c["pension_saving_manwon"] == 400
    assert c["irp_manwon"] == 300


def test_하나의_금액_표현은_합친다():
    """'1억 2천만원'은 두 토큰이지만 하나의 금액이다."""
    assert parse_amount_expressions("1억 2천만원")[0][2] == 12000
    assert len(parse_amount_expressions("퇴직금 2억원과 연금 500만원")) == 2


def test_합산_표현은_합산으로_인식한다():
    c = derive_conditions("연금저축이랑 IRP 합쳐서 900만원 넣으면?")
    assert c.get("combined_contribution_manwon") == 900
    assert "pension_saving_manwon" not in c


# ════════════════════════════════════════════════════════════════
# LLM 호출 예산 — 설계 가정(3개소)을 벗어나지 않는가
# ════════════════════════════════════════════════════════════════

def test_정상_경로에서_LLM_호출은_3회_이하():
    """L1 · L5' · L6. 재생성이 매번 돌면 문항당 5회가 되어
    크레딧 소모가 설계 가정의 1.7배가 된다."""
    from app.pipeline import answer_question

    class Compliant:
        is_mock = False
        def __init__(self): self.calls = []
        def call(self, system, user, purpose="?", **kw):
            self.calls.append(purpose)
            if "감사자" in system:
                return '{"verdict":"APPROVE","findings":[]}'
            body = "[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
            if "주의할 혼동" in user:
                body += "두 개념은 서로 다릅니다. 주의하실 점입니다.\n"
            body += "제공 자료 근거로 안내드립니다.\n\n[한계 고지]\n확인이 필요합니다."
            return body
        def call_with_functions(self, s, u, t, purpose="?", **kw):
            self.calls.append(purpose)
            return {"name": None, "arguments": None, "raw": ""}

    client = Compliant()
    answer_question("Q", "연금저축 세액공제 한도가 얼마인가요?", client=client)
    assert len(client.calls) <= 3, f"호출 {client.calls}"
