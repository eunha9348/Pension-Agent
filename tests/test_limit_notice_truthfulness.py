"""계산이 성공했는데 "확정하기 어렵다"고 고지하던 결함 (2026-09-04 실서버).

━━ 실측 결함 (스크린샷 2) ━━
질의  "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?"
답변  "연금 수령 한도는 1,200만원이므로, 1년차에 인출할 수 있는 최대
       금액은 1,200만원입니다."
그런데 맨 아래에:
      "※ 연금수령한도 **산정 방식** 관련 내용은 제공 자료로 확정하기
        어려워 별도 확인이 필요합니다."

사실과 다르다. `연금수령한도_계산`이 limit=1200.0, denominator=10,
**source=doc39**을 냈다 — 산정 방식은 제공 자료(doc39)에 근거해 이미
산출됐다. 정확히 계산해 놓고 "확정할 수 없다"고 말한 것이다.

━━ 원인 ━━
`verify_requirement_coverage`는 "LLM 문장이 이 슬롯을 설명했는가"만 본다.
슬롯은 같은 TopicRule에서 `{base}_fact` · `{base}_calc` 쌍으로 생기는데
(query_spec.rule_based_spec), 계산 슬롯이 성공했는지는 보지 않았다.
그래서 답변이 공식을 문장으로 풀어 쓰지 않았다는 이유로 fact 슬롯이
미충족으로 잡혀 거짓 고지가 붙었다.

━━ 무엇을 완화하지 않는가 ━━
고지 자체를 무르는 것이 아니다. 계산이 **실패했거나 없는** 주제의 고지는
그대로 남는다 — 없앤 것은 **사실이 아닌 고지**뿐이다.
나이·수령방식에 따라 과세율이 갈린다는 다른 고지들도 이 수정과 무관하게
유지된다(그건 정당한 고지다).
"""

from __future__ import annotations

from app.core.coverage_pipeline import RequirementSlot, SlotStatus, TraceLogger
from app.pipeline import _drop_covered_by_calc


def _slot(slot_id: str, desc: str, status=SlotStatus.COVERED) -> RequirementSlot:
    s = RequirementSlot(slot_id=slot_id, description=desc, slot_type="fact")
    s.status = status
    return s


# ── 거짓 고지를 제거하는가 ────────────────────────────────────

def test_계산이_성공한_주제의_고지는_제거된다():
    """★ 실측 스크린샷2 그대로 — suryeong_hando_calc가 성공했다."""
    fact = _slot("suryeong_hando_fact", "연금수령한도 산정 방식")
    calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)

    kept = _drop_covered_by_calc([fact], [fact, calc], TraceLogger())
    assert kept == [], "계산이 성공했는데도 '확정 불가' 고지가 남았다"


def test_제거_사실이_think_trace에_남는다():
    """조용히 지우면 왜 고지가 사라졌는지 추적할 수 없다."""
    fact = _slot("suryeong_hando_fact", "연금수령한도 산정 방식")
    calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)

    trace = TraceLogger()
    _drop_covered_by_calc([fact], [fact, calc], trace)
    assert "한계고지_교정" in [s.step for s in trace._steps]


# ── 무엇을 완화하지 않는가 (안전판) ──────────────────────────

def test_계산이_실패한_주제의_고지는_유지된다():
    """★ 이게 무너지면 진짜 한계까지 감추게 된다."""
    fact = _slot("suryeong_hando_fact", "연금수령한도 산정 방식")
    calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.MISSING)

    kept = _drop_covered_by_calc([fact], [fact, calc], TraceLogger())
    assert kept == [fact], "계산이 안 됐는데 고지를 지웠다"


def test_계산_슬롯_자체가_없으면_고지가_유지된다():
    fact = _slot("jungdo_fact", "중도인출 사유와 세제")
    kept = _drop_covered_by_calc([fact], [fact], TraceLogger())
    assert kept == [fact]


def test_다른_주제의_계산이_성공해도_영향받지_않는다():
    """★ base가 다르면 남의 계산이다 — 그걸로 고지를 지우면 안 된다."""
    fact = _slot("jungdo_fact", "중도인출 사유와 세제")
    other_calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)

    kept = _drop_covered_by_calc([fact], [fact, other_calc], TraceLogger())
    assert kept == [fact], "무관한 주제의 계산으로 고지를 지웠다"


def test_fact_접미사가_아닌_슬롯은_건드리지_않는다():
    """규약(_fact/_calc 쌍)에 맞지 않는 슬롯은 판단 대상이 아니다."""
    odd = _slot("자유서술_슬롯", "무언가")
    calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)

    kept = _drop_covered_by_calc([odd], [odd, calc], TraceLogger())
    assert kept == [odd]


def test_미충족이_없으면_아무_일도_하지_않는다():
    calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)
    assert _drop_covered_by_calc([], [calc], TraceLogger()) == []


def test_여러_주제가_섞이면_해당_주제만_제거한다():
    done_fact = _slot("suryeong_hando_fact", "연금수령한도 산정 방식")
    done_calc = _slot("suryeong_hando_calc", "연금수령한도", SlotStatus.CALC_DONE)
    open_fact = _slot("jungdo_fact", "중도인출 사유와 세제")

    kept = _drop_covered_by_calc(
        [done_fact, open_fact], [done_fact, done_calc, open_fact], TraceLogger())
    assert kept == [open_fact]


# ── end-to-end — 실제 답변에서 거짓 고지가 사라지는가 ─────────

def test_실서버_시나리오에서_거짓_고지가_사라진다():
    """★ 스크린샷2를 파이프라인 전체로 재현한다."""
    import json

    from app.analysis.query_spec import rule_based_spec
    from app.pipeline import answer_question

    # 한도는 말하되 산정 공식은 문장으로 풀지 않는 초안 (실제 답변 형태)
    draft = ("현재 연금 계좌에는 1억원이 있으며 연금 수령 1년차입니다. "
             "이 경우 연금 수령 한도는 1,200만원이므로, 1년차에 인출할 수 있는 "
             "최대 금액은 1,200만원입니다.")

    class _Client:
        is_mock = False

        def call(self, system, user, purpose="?", **kw):
            if purpose == "l1_query_spec":
                q = user.split("[질의]")[-1].strip() or user
                b = rule_based_spec(q)
                return json.dumps({"intent": b.get("intent"),
                                   "asked_for": b.get("asked_for") or []},
                                  ensure_ascii=False)
            if "감사자" in system:
                return '{"verdict":"APPROVE","findings":[]}'
            return draft

        def call_with_functions(self, s, u, t, purpose="?", **kw):
            return {"name": None, "arguments": None, "raw": ""}

    r = answer_question(
        "S2", "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?",
        client=_Client())

    assert "산정 방식 관련 내용은 제공 자료로 확정하기 어려워" not in r["answer"], (
        "계산이 성공했는데도 '확정 불가' 고지가 답변에 남았다")
    assert "1,200만원" in r["answer"], "정작 계산 결과가 사라졌다"
