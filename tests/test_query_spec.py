"""L1 질의 분석 · 답변 생성 · 근거 검증 래퍼 테스트."""

from __future__ import annotations

from app.analysis.query_spec import (make_extract_query_spec, rule_based_spec,
                                     sanitize_spec)
from app.config import Settings
from app.core.coverage_pipeline import (CALC_REGISTRY, EvidenceChunk,
                                        RequirementSlot, SlotStatus)
from app.generation.answer_prompt import (FORBIDDEN_EXPRESSIONS,
                                          build_supervisor_payload,
                                          render_template_answer,
                                          strip_forbidden)
from app.generation.grounding import make_verify_grounding
from app.llm.clova import MockClovaClient


# ── 규칙 기반 추출 ───────────────────────────────────────────

def test_LLM_없이도_슬롯과_계산함수가_나온다():
    """mock/실패 상황에서도 파이프라인이 멈추면 안 된다."""
    spec = rule_based_spec("연금저축이랑 IRP 합쳐서 세액공제 얼마까지 되나요?")
    assert spec["intent"] == "세액공제"
    assert any(s["type"] == "calculation" for s in spec["asked_for"])
    assert spec["planned_calls"][0]["function"] == "사적연금_납입한도_세액공제_계산"
    assert spec["plan"]


def test_계획된_호출은_전부_등록된_함수다():
    questions = [
        "연금수령한도가 얼마인가요", "80세면 세금 얼마나 떼나요",
        "퇴직금 2억에 근속 25년이면 퇴직소득세는", "총보수 낮은 클래스 알려주세요",
        "중도인출하면 세금이 어떻게 되나요", "연금 11년차면 감면율이 얼마인가요",
    ]
    for q in questions:
        for call in rule_based_spec(q)["planned_calls"]:
            assert call["function"] in CALC_REGISTRY


def test_주제를_못_잡아도_일반_슬롯을_만든다():
    spec = rule_based_spec("연금이 궁금해요")
    assert spec["asked_for"]
    assert spec["asked_for"][0]["required"] is True


def test_미등록_계산함수는_사실슬롯으로_강등된다():
    """L1이 없는 함수를 지어내도 실행되지 않는다."""
    raw = {"intent": "세액공제",
           "asked_for": [{"id": "x", "description": "가짜", "type": "calculation",
                          "calc_function": "존재하지_않는_함수"}],
           "planned_calls": [{"function": "존재하지_않는_함수", "args": {}}]}
    spec = sanitize_spec(raw, "질문")
    assert spec["asked_for"][0]["type"] == "fact"
    assert spec["planned_calls"] == []


def test_DEPRECATED_함수는_현행함수로_교정된다():
    raw = {"asked_for": [{"id": "t", "description": "과세", "type": "calculation",
                          "calc_function": "과세방식_판정_계산"}],
           "planned_calls": [{"function": "과세방식_판정_계산", "args": {}}]}
    spec = sanitize_spec(raw, "질문")
    assert spec["asked_for"][0]["calc_function"] == "과세방식_비교_계산"
    assert spec["planned_calls"][0]["function"] == "과세방식_비교_계산"


def test_mock_클라이언트면_규칙기반으로_폴백한다():
    logs = []
    extract = make_extract_query_spec(
        client=MockClovaClient(Settings()),
        trace_log=lambda step, reason, **kw: logs.append((step, reason)))
    spec = extract("연금수령한도가 얼마인가요?")
    assert spec["source"] == "rule"
    assert any("규칙 기반" in r for _, r in logs)      # 사실이 trace에 남는다


def test_L0_힌트는_주입되지만_근거로_쓰이지_않는다():
    """L0 접지 정보는 분석 참고용이다. spec에 근거로 들어가면 안 된다."""
    extract = make_extract_query_spec(client=MockClovaClient(Settings()),
                                      grounding_hint="관련 영역: 세제")
    spec = extract("세액공제 얼마인가요")
    assert "세제" not in str(spec.get("asked_for"))


# ── 답변 생성 ────────────────────────────────────────────────

def _calc_slot():
    s = RequirementSlot("hando", "연금수령한도", "calculation",
                        calc_function="연금수령한도_계산")
    s.status = SlotStatus.CALC_DONE
    s.calc_result = {"limit": 1200.0, "unlimited": False, "denominator": 10,
                     "source": "doc39"}
    return s


def test_템플릿_답변은_세_블록_형식을_지킨다():
    spec = {"query": "1억이고 1년차면 얼마까지?",
            "user_conditions": {"account_value_manwon": 10000, "pension_year": 1}}
    out = render_template_answer(spec, [], [_calc_slot()])
    assert "[확인된 조건]" in out
    assert "[조건별 결론]" in out
    assert "[한계 고지]" in out
    assert "1,200만원" in out         # 계산 결과 수치가 실제로 들어간다


def test_템플릿_답변에_금지표현이_없다():
    spec = {"query": "질문", "user_conditions": {}}
    out = render_template_answer(spec, [], [_calc_slot()])
    assert not [p for p in FORBIDDEN_EXPRESSIONS if p in out]


def test_금지표현_치환():
    fixed, found = strip_forbidden("C-Pe가 가장 유리합니다. 추천드립니다.")
    assert found
    assert "가장 유리합니다" not in fixed


def test_프롬프트에_계산결과와_금지사항이_들어간다():
    spec = {"query": "질문", "user_conditions": {"age": 80}}
    payload = build_supervisor_payload(
        spec, [EvidenceChunk("doc39", "연금수령한도 규정", score=1.0)],
        [_calc_slot()],
        trap_context={"correction_notes": ["연금수령연차와 연금실제수령연차는 다릅니다"]},
        ask_back_items=["연금실제수령연차"])
    assert "계산 결과" in payload
    assert "주의할 혼동" in payload
    assert "확인이 필요한 항목" in payload


# ── 근거 검증 래퍼 ───────────────────────────────────────────

def test_근거없는_수치는_검증에_걸린다():
    verify = make_verify_grounding("질문", [_calc_slot()], llm_call=None)
    ev = [EvidenceChunk("doc39", "연금수령한도 = 평가액 ÷ (11 - 연금수령연차) × 120%",
                        score=1.0)]
    ok = verify("연금수령한도는 9,999만원입니다.", ev)
    assert not ok
    assert 9999.0 in ok.numeric.ungrounded


def test_계산결과_수치는_통과한다():
    verify = make_verify_grounding("질문", [_calc_slot()], llm_call=None)
    ok = verify("연금수령한도는 1,200만원입니다.", [])
    assert ok
    assert "통과" in ok.as_trace()


def test_의미감사는_결정론적_판정을_완화하지_못한다():
    """권한 계층 — LLM이 APPROVE라 해도 결정론적 REVISE는 유지된다."""
    slot = _calc_slot()
    # 근거 없는 수치 + 단정 표현 → 결정론적 감사가 문제를 잡는다
    verify = make_verify_grounding(
        "질문", [slot],
        llm_call=lambda s, u: '{"verdict":"APPROVE","findings":[]}')
    ok = verify("이 상품이 가장 유리합니다. 세액공제는 9,999만원입니다.", [])
    assert not ok
