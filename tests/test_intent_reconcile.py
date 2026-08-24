"""D단계 · 제도 분류 교정 회귀.

━━ Q-001이 실패한 진짜 이유 ━━
"명퇴수당을 연금계좌에 넣으면 **세금감면**이 어마어마하다던데"

L1이 '세금감면'이라는 표현에 이끌려 의도를 '세액공제'로 잡았다.
명퇴수당은 이미 발생한 퇴직소득이므로 맞는 제도는 **이연퇴직소득세 감면**이고,
세액공제는 새로 납입하는 금액에 적용되는 전혀 다른 제도다.

더 나빴던 건 구조다. LLM이 슬롯을 주면 규칙 슬롯을 통째로 버려서,
화면에 표시된 실행 계획과 실제 실행된 슬롯이 서로 달랐다.

CLAUDE.md — "판단은 코드, 문장은 LLM". 어떤 제도를 다루는지는 코드가 정한다.
"""

from __future__ import annotations

from app.analysis.query_spec import reconcile_spec, rule_based_spec

Q_MYEONGTOE = ("명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
               "어마어마하다던데, 절세법만 알려주세요.")


def _llm_said_tax_credit() -> dict:
    """당시 L1이 실제로 내놓은 것과 같은 형태의 산출물."""
    return {
        "query": Q_MYEONGTOE,
        "intent": "세액공제",
        "asked_for": [{"id": "seaek_gongje", "description": "연금저축·IRP 세액공제",
                       "type": "calculation",
                       "calc_function": "사적연금_납입한도_세액공제_계산",
                       "required": True}],
        "planned_calls": [{"function": "사적연금_납입한도_세액공제_계산", "args": {}}],
        "source": "llm",
    }


# ════════════════════════════════════════════════════════════════
# D-1 · 제도 오분류 교정
# ════════════════════════════════════════════════════════════════

def test_명퇴_질의를_세액공제로_분류하면_교정한다():
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert out["intent"] != "세액공제", "퇴직소득 질의가 세액공제로 남아 있다"


def test_교정_시_퇴직소득_슬롯이_앞에_온다():
    """계산 함수가 먼저 잡혀야 답변이 그 제도를 중심으로 만들어진다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    ids = [s["id"] for s in out["asked_for"]]
    assert not ids[0].startswith("seaek_gongje")
    assert any(i.startswith("toejik_gamnyeon") for i in ids)


def test_명예퇴직_슬롯을_잃지_않는다():
    """규칙이 찾아낸 명예퇴직급여 처리가 LLM 슬롯에 밀려 사라지면 안 된다.
    이게 빠지면 '명퇴수당은 법정 외 퇴직급여라 수령 방법을 선택할 수 있다'는,
    이 사용자에게 가장 실행 가능한 정보가 답변에서 통째로 사라진다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert any(s["id"].startswith("myeongtoe") for s in out["asked_for"])


def test_오분류된_세액공제_슬롯은_밀려난다():
    """퇴직소득 질의에 신규 납입 세액공제는 다룰 제도가 아니다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert not any(s["id"].startswith("seaek_gongje") for s in out["asked_for"])


def test_세액공제를_명시적으로_물으면_교정하지_않는다():
    """오탐 회귀 — 진짜 세액공제 질의까지 뒤집으면 안 된다."""
    q = "퇴직금 받은 것과 별개로 연금저축 세액공제 한도가 얼마인가요?"
    llm = {"query": q, "intent": "세액공제", "asked_for": [], "source": "llm"}
    out = reconcile_spec(llm, rule_based_spec(q), q)
    assert out["intent"] == "세액공제"


def test_퇴직소득_신호가_없으면_교정하지_않는다():
    q = "연금저축과 IRP 합쳐서 세액공제 얼마까지 되나요"
    llm = {"query": q, "intent": "세액공제", "asked_for": [], "source": "llm"}
    out = reconcile_spec(llm, rule_based_spec(q), q)
    assert out["intent"] == "세액공제"


def test_LLM_판단을_무조건_버리지는_않는다():
    """규칙이 못 잡는 질의에서는 LLM 분석이 살아 있어야 한다."""
    q = "연금 받을 때 세금이 어떻게 되나요"
    llm = {"query": q, "intent": "원천징수",
           "asked_for": [{"id": "wonchen", "description": "원천징수세율",
                          "type": "fact", "required": True}],
           "source": "llm"}
    out = reconcile_spec(llm, rule_based_spec(q), q)
    assert out["intent"] == "원천징수"
    assert any(s["id"] == "wonchen" for s in out["asked_for"])


# ════════════════════════════════════════════════════════════════
# 계획과 실행의 일치
# ════════════════════════════════════════════════════════════════

def test_표시된_계획과_실행_슬롯이_어긋나지_않는다():
    """Q-001에서는 계획에 '퇴직소득세 감면율 계산'이 적혀 있는데
    실제로는 세액공제 슬롯이 실행됐다. 트레이스를 믿을 수 없게 된다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    slot_fns = {s.get("calc_function") for s in out["asked_for"]
                if s.get("calc_function")}
    planned_fns = {c["function"] for c in out["planned_calls"]}
    assert planned_fns == slot_fns


def test_슬롯이_과도하게_늘지_않는다():
    """합치기만 하면 슬롯이 불어나 답변이 산만해진다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert len(out["asked_for"]) <= 3


def test_교정_사실이_source에_남는다():
    """왜 LLM 판단이 뒤집혔는지 추적 가능해야 한다."""
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert "교정" in out.get("source", "")


# ════════════════════════════════════════════════════════════════
# D-2 · 함정 지침의 강제력
# ════════════════════════════════════════════════════════════════

def test_프롬프트가_반영할_용어를_구체적으로_지시한다():
    """'주의가 필요합니다' 같은 일반 문장으로 때우지 못하게 한다."""
    from app.core.trap_rules import build_trap_context
    from app.generation.answer_prompt import build_supervisor_payload

    ctx = build_trap_context(Q_MYEONGTOE)
    payload = build_supervisor_payload({"query": Q_MYEONGTOE}, [], [], ctx)
    assert "반드시 답변에 등장해야 함" in payload
    assert "법정 외" in payload


def test_시스템_프롬프트가_두_제도를_구분하게_한다():
    from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT
    assert "이연퇴직소득세" in SUPERVISOR_SYSTEM_PROMPT
    assert "세액공제" in SUPERVISOR_SYSTEM_PROMPT


def test_시스템_프롬프트가_수치_관계를_요구한다():
    """'600만원 또는 900만원 중 선택'은 근거의 '적은 금액'을 오독한 것이다."""
    from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT
    assert "적은 금액" in SUPERVISOR_SYSTEM_PROMPT


# ════════════════════════════════════════════════════════════════
# 계산 슬롯은 MAX_SLOTS 절단에 잘려나가지 않는다 (E-14)
# ════════════════════════════════════════════════════════════════
# 규칙 슬롯은 LLM 슬롯 뒤에 붙는다. L1이 슬롯을 MAX_SLOTS만큼 내놓으면
# 규칙이 찾은 계산 슬롯이 통째로 잘렸다. 계산이 안 돌면 calc_results가
# 비고, 비면 verify_calc_presence가 대조 없이 통과한다 — 계산값이
# 답변에 도달하는지 보장하는 장치 전체가 무력화된다.

Q_WONCHEON = "만 80세인데 연금 받을 때 세금 몇 퍼센트 떼나요?"


def _llm_gave_three_fact_slots() -> dict:
    """계산함수를 안 고르고 사실 슬롯만 MAX_SLOTS만큼 채운 L1 산출물."""
    from app.analysis.query_spec import sanitize_spec

    return sanitize_spec({
        "intent": "연금과세",
        "asked_for": [
            {"id": "s1", "description": "연령별 원천징수세율",
             "type": "fact", "required": True},
            {"id": "s2", "description": "연금소득 과세 방식",
             "type": "fact", "required": True},
            {"id": "s3", "description": "지방소득세 포함 여부",
             "type": "fact", "required": False},
        ],
        "search_terms": ["원천징수세율"], "plan": [],
    }, Q_WONCHEON)


def test_LLM_슬롯이_꽉_차도_계산슬롯은_살아남는다():
    out = reconcile_spec(_llm_gave_three_fact_slots(),
                         rule_based_spec(Q_WONCHEON), Q_WONCHEON)
    fns = {s.get("calc_function") for s in out["asked_for"]}
    assert "사적연금_원천징수_계산" in fns


def test_계산슬롯을_살려도_슬롯_상한은_지킨다():
    """상한을 넘기면 답변이 산만해진다 — 자리를 늘리는 게 아니라 바꾸는 것이다."""
    from app.analysis.query_spec import MAX_SLOTS

    out = reconcile_spec(_llm_gave_three_fact_slots(),
                         rule_based_spec(Q_WONCHEON), Q_WONCHEON)
    assert len(out["asked_for"]) <= MAX_SLOTS


def test_살아남은_계산슬롯은_실행계획에도_오른다():
    """슬롯만 남고 planned_calls에 없으면 계산은 여전히 안 돈다."""
    out = reconcile_spec(_llm_gave_three_fact_slots(),
                         rule_based_spec(Q_WONCHEON), Q_WONCHEON)
    assert any(c["function"] == "사적연금_원천징수_계산"
               for c in out["planned_calls"])


def test_오분류_경로는_계산슬롯_우대에_흔들리지_않는다():
    """명퇴 질의의 '세액공제'는 계산 슬롯이지만 되살리면 안 된다.

    계산 슬롯을 무조건 우대하면 이미 내린 오분류 판정을 뒤집는다.
    """
    out = reconcile_spec(_llm_said_tax_credit(),
                         rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    ids = [s["id"] for s in out["asked_for"]]
    assert not any(i.startswith("seaek_gongje") for i in ids)
    assert any(i.startswith("myeongtoe") for i in ids)


def test_평가셋_전문항에서_규칙_계산슬롯이_보존된다():
    """문항별로 하나씩 놓치지 않도록 전수로 못 박는다."""
    from app.analysis.query_spec import MAX_SLOTS, sanitize_spec
    from tests.eval_set import EVAL_CASES

    filler = {"intent": "일반", "search_terms": [], "plan": [],
              "asked_for": [{"id": f"x{i}", "description": f"설명{i}",
                             "type": "fact", "required": True}
                            for i in range(MAX_SLOTS)]}
    lost = []
    for case in EVAL_CASES:
        fb = rule_based_spec(case.question)
        want = {s.get("calc_function") for s in fb["asked_for"]
                if s.get("calc_function")}
        if not want:
            continue
        out = reconcile_spec(sanitize_spec(dict(filler), case.question),
                             fb, case.question)
        got = {s.get("calc_function") for s in out["asked_for"]}
        if not want <= got or len(out["asked_for"]) > MAX_SLOTS:
            lost.append((case.id, sorted(want - got)))
    assert not lost, f"계산 슬롯을 잃은 문항: {lost}"
