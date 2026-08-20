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


# ════════════════════════════════════════════════════════════════
# L1 tools 스키마 — HCX-005 거부(400 · 40009) 회귀
# ════════════════════════════════════════════════════════════════
#
# 실사고: QUERY_SPEC_TOOL에 계산함수 15종 enum이 들어 있어서 HCX-005가
# tools를 통째로 거부했다(HTTP 400 {"code":"40009"}). 그 결과 평가 42건
# **전부**에서 L1이 실패하고 규칙 폴백으로만 돌았다. 진단 사다리에서
# 스칼라·문자열배열·중첩객체·객체배열은 통과했고 값 많은 enum만 깨졌다.

def test_스키마에_큰_enum이_없다():
    """enum을 되살리면 L1이 다시 통째로 죽는다 — 그건 조용히 일어난다."""
    import json

    from app.analysis.query_spec import QUERY_SPEC_TOOL

    def _enums(node):
        if isinstance(node, dict):
            if "enum" in node:
                yield node["enum"]
            for v in node.values():
                yield from _enums(v)
        elif isinstance(node, list):
            for v in node:
                yield from _enums(v)

    for e in _enums(QUERY_SPEC_TOOL):
        assert len(e) <= 5, f"값이 많은 enum은 HCX-005가 거부한다: {e[:3]}..."
    # 스키마가 통째로 비대해지는 것도 막는다
    assert len(json.dumps(QUERY_SPEC_TOOL, ensure_ascii=False)) < 6000


def test_계산함수명은_description으로_안내된다():
    """enum을 뺐으므로, 모델이 함수명을 알 수 있는 경로가 남아 있어야 한다."""
    import json

    from app.analysis.query_spec import QUERY_SPEC_TOOL
    from app.core.coverage_pipeline import CALC_REGISTRY

    blob = json.dumps(QUERY_SPEC_TOOL, ensure_ascii=False)
    assert "연금수령한도_계산" in blob
    assert "퇴직소득세_감면율_계산" in blob
    # 전부 들어 있어야 모델이 고를 수 있다
    assert all(name in blob for name in CALC_REGISTRY)


def test_미등록_함수는_스키마가_아니라_계획감사가_막는다():
    """enum을 뺀 근거 — 검증 관문은 그대로 남아 있어야 한다."""
    from app.core.coverage_pipeline import CALC_REGISTRY
    from app.core.supervisory_board import supervise_plan

    spec = {
        "intent": "연금수령한도",
        "asked_for": [{"id": "s1", "description": "한도", "type": "calculation"}],
        "planned_calls": [
            {"function": "존재하지_않는_함수", "args": {}},
            {"function": "연금수령한도_계산", "args": {}},
        ],
    }
    result, fixed = supervise_plan(spec, CALC_REGISTRY)
    names = [c["function"] for c in fixed["planned_calls"]]
    assert "존재하지_않는_함수" not in names
    assert "연금수령한도_계산" in names
    assert any(f.code == "UNKNOWN_FUNCTION" for f in result.findings)


def test_축소_스키마도_큰_enum을_쓰지_않는다():
    """폴백이 본 스키마와 같은 이유로 깨지면 폴백이 아니다."""
    import json

    from app.analysis.query_spec import MINIMAL_QUERY_SPEC_TOOL

    blob = json.dumps(MINIMAL_QUERY_SPEC_TOOL, ensure_ascii=False)
    assert "enum" not in blob
    assert len(blob) < len(json.dumps(
        __import__("app.analysis.query_spec", fromlist=["QUERY_SPEC_TOOL"]
                   ).QUERY_SPEC_TOOL, ensure_ascii=False))


def test_스키마_거부시_축소_스키마로_재시도한다():
    """400을 맞았다고 곧장 규칙 폴백으로 떨어지면 search_terms를 잃는다."""
    from app.analysis.query_spec import make_extract_query_spec

    seen: list[int] = []

    class _C:
        is_mock = False

        def call_with_functions(self, system, user, tools, purpose="", **kw):
            seen.append(len(__import__("json").dumps(tools, ensure_ascii=False)))
            if len(seen) == 1:
                raise RuntimeError('HTTP 400: {"status":{"code":"40009"}}')
            return {"name": "extract_query_spec",
                    "arguments": {"intent": "연금수령한도",
                                  "asked_for": [{"id": "s1",
                                                 "description": "한도",
                                                 "type": "calculation"}],
                                  "search_terms": ["연금수령한도"]}}

    extract = make_extract_query_spec(client=_C())
    spec = extract("1억이고 10년차면 한도가?")
    assert len(seen) == 2, "축소 스키마로 재시도해야 한다"
    assert seen[1] < seen[0], "재시도는 더 작은 스키마여야 한다"
    assert spec["source"].startswith("llm")


def test_타임아웃은_축소_스키마로_재시도하지_않는다():
    """스키마 문제가 아닌데 또 부르면 지연만 두 배가 된다."""
    from app.analysis.query_spec import make_extract_query_spec

    calls: list[str] = []

    class _C:
        is_mock = False

        def call_with_functions(self, system, user, tools, purpose="", **kw):
            calls.append(purpose)
            raise RuntimeError("timeout")

    extract = make_extract_query_spec(client=_C())
    spec = extract("1억이고 10년차면 한도가?")
    assert len(calls) == 1
    assert spec["source"] != "llm"
