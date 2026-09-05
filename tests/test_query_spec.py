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


def test_제도차원_가입질문은_판매클래스_적합성으로_새지_않는다():
    """★ 2026-09-05 외부 심사 리포트로 발견 — "IRP는 아무나 가입할 수
    있나요?"(제도 차원)가 "가입할 수 있"이라는 키워드 때문에 상품
    판매클래스 적합성 판정(제도가 아니라 개별 펀드 클래스 문제)으로
    잘못 라우팅돼, 질문과 무관한 "가입하려는 판매 클래스가 무엇인가요?"
    라는 역질문만 돌아왔다(사실상 무응답, 72초 지연). '아무나'·'누구나'
    같은 제도 차원 표현이 있으면 이 규칙을 배제해 검색이 실제 가입대상
    안내를 찾아오게 한다.
    """
    for q in ("IRP는 아무나 가입할 수 있나요?", "연금저축은 누구나 가입 가능한가요?"):
        spec = rule_based_spec(q)
        assert spec["intent"] != "가입자격", q
        assert not any(c["function"] == "판매클래스_적합성_판정"
                       for c in spec["planned_calls"]), q

    # 상품 클래스를 묻는 진짜 질의는 여전히 정상 라우팅돼야 한다(회귀 방지)
    spec = rule_based_spec("이 펀드 판매 클래스 가입자격이 어떻게 되나요?")
    assert spec["intent"] == "가입자격"


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


def test_템플릿_답변이_세_항목을_모두_담는다():
    """2026-08-29 — 대괄호 구획은 없앴지만 **항목은 그대로**다.

    검사 대상이 형식이 아니라 내용으로 바뀌었다. 사람처럼 이어지는
    문장으로 쓰되, 조건 이해·결론·한계 셋이 빠지면 안 된다.
    """
    spec = {"query": "1억이고 1년차면 얼마까지?",
            "user_conditions": {"account_value_manwon": 10000, "pension_year": 1}}
    out = render_template_answer(spec, [], [_calc_slot()])

    assert "조건으로 이해했습니다" in out           # ① 조건 이해
    assert "1,200만원" in out                      # ② 결론 (계산 수치)
    assert ("확인해 주시면" in out or "달라질 수 있습니다" in out
            or "확정하기 어렵습니다" in out)          # ③ 한계 고지

    # 딱딱한 구획은 쓰지 않는다 — L5'와 같은 어조여야 한다
    for bracket in ("[확인된 조건]", "[조건별 결론]", "[한계 고지]"):
        assert bracket not in out, f"구획 표시가 남아 있다: {bracket}"


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
# L1 · tools 없이 JSON 텍스트로 받는다
# ════════════════════════════════════════════════════════════════
#
# HCX-005는 tools 페이로드를 간헐적으로 거부한다(400 · 40009).
# 진단 3회에서 대조군 "tools 없는 일반 채팅"은 전부 200이었으므로,
# 오류는 스키마의 특정 요소가 아니라 tools 기능 자체에 붙어 있다.
# 그래서 평범한 텍스트 호출로 JSON을 받는다.

def test_tools를_쓰지_않는다():
    """되살리면 40009가 함께 돌아온다 — 조용히 일어난다."""
    import inspect

    from app.analysis import query_spec as qs

    src = inspect.getsource(qs)
    assert "call_with_functions" not in src
    assert not hasattr(qs, "QUERY_SPEC_TOOL")
    assert not hasattr(qs, "SCHEMA_LADDER")


def test_계산함수_목록이_프롬프트로_전달된다():
    """enum으로 강제하지 않으므로 모델이 알 경로는 프롬프트뿐이다."""
    from app.analysis.query_spec import l1_system_prompt
    from app.core.coverage_pipeline import CALC_REGISTRY

    p = l1_system_prompt()
    assert all(name in p for name in CALC_REGISTRY)
    assert "{calc_functions}" not in p       # 치환이 실제로 일어났는가


def test_JSON_응답을_형태에_관계없이_회수한다():
    from app.analysis.query_spec import parse_spec_json

    ok = '{"intent":"세액공제","asked_for":[]}'
    assert parse_spec_json(ok)["intent"] == "세액공제"
    assert parse_spec_json(f"```json\n{ok}\n```")["intent"] == "세액공제"
    assert parse_spec_json(f"분석 결과입니다.\n{ok}\n이상.")["intent"] == "세액공제"


def test_우리_스키마가_아닌_JSON은_거부한다():
    """모델이 분석 대신 답변을 지어낸 경우 — 규칙 폴백으로 가야 한다."""
    from app.analysis.query_spec import parse_spec_json

    assert parse_spec_json('{"answer":"연금은 좋습니다"}') is None
    assert parse_spec_json("죄송합니다 분석할 수 없습니다") is None
    assert parse_spec_json("") is None


def test_호출이_실패하면_규칙_폴백으로_간다():
    from app.analysis.query_spec import make_extract_query_spec

    class _Boom:
        is_mock = False
        def __init__(self):
            self.calls = 0
        def call(self, system, user, purpose="", **kw):
            self.calls += 1
            raise RuntimeError("timeout")

    c = _Boom()
    spec = make_extract_query_spec(client=c)("1억이고 10년차면 한도가?")
    assert c.calls == 1, "재시도로 지연을 늘리지 않는다"
    assert spec["source"] != "llm"
    assert spec["asked_for"], "규칙 폴백은 여전히 슬롯을 채운다"


def test_JSON을_받으면_LLM_경로로_처리한다():
    from app.analysis.query_spec import make_extract_query_spec

    class _Ok:
        is_mock = False
        def call(self, system, user, purpose="", **kw):
            return ('{"intent":"연금수령한도",'
                    '"asked_for":[{"id":"h","description":"한도",'
                    '"type":"calculation","calc_function":"연금수령한도_계산"}],'
                    '"search_terms":["연금수령한도"]}')

    spec = make_extract_query_spec(client=_Ok())("1억이고 10년차면 한도가?")
    assert spec["source"].startswith("llm")
    assert "연금수령한도" in spec["search_terms"]
