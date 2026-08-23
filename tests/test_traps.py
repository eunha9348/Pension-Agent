"""함정 규칙 테스트 — 감지 정확도 + 오탐 회귀.

━━ 오탐 회귀가 중요한 이유 ━━
과거 "정해지는"의 '해지'가 규칙에 걸리는 오탐이 있었다.
부분 문자열 매칭은 한국어에서 이런 사고를 반드시 낸다.
아래 참고 질의 5개는 **걸리면 안 되는** 질의다.
"""

from __future__ import annotations

import pytest

from app.core.trap_rules import TRAPS, build_trap_context, detect_traps

# 함정에 걸리면 안 되는 평범한 질의 5개 (오탐 회귀 세트)
CLEAN_QUERIES = [
    "기준가격은 어떻게 정해지는 건가요?",
    "연금 개시 시점은 언제로 정해지나요?",
    "펀드 수익률은 어떻게 산정되나요?",
    "환매는 며칠이나 걸리나요?",
    "가입자 교육은 언제 받아야 하나요?",
]


def test_규칙이_26종이다():
    assert len(TRAPS) == 26


def test_규칙_스키마가_온전하다():
    for r in TRAPS:
        assert r.id and r.title and r.trigger_keywords
        assert r.severity in ("critical", "high", "medium")
        assert r.fact, f"{r.id}: fact가 비어 있으면 교정 근거가 없다"


@pytest.mark.parametrize("q", CLEAN_QUERIES)
def test_오탐_회귀_평범한_질의는_걸리지_않는다(q):
    """'정해지는' → '해지' 오탐 재발 방지."""
    assert detect_traps(q) == [], f"오탐: {q} → {[r.id for r in detect_traps(q)]}"


@pytest.mark.parametrize("q,expected_id", [
    ("주택 사려고 중도인출하면 세금이 어떻게 되나요", "A1"),
    ("연금 11년째인데 퇴직소득세 40% 감면 맞나요", "B1"),
])
def test_대표_함정을_감지한다(q, expected_id):
    assert expected_id in [r.id for r in detect_traps(q)]


def test_감지되면_교정_문구가_제공된다():
    ctx = build_trap_context("주택 사려고 중도인출하면 세금이 어떻게 되나요")
    assert ctx["detected"]
    assert ctx["facts"]
    assert ctx["trace"]


def test_감지되지_않으면_빈_컨텍스트():
    ctx = build_trap_context("펀드 수익률은 어떻게 산정되나요?")
    assert ctx["detected"] == []
    assert ctx["critical_count"] == 0
    assert "없음" in ctx["trace"]


def test_trigger_all은_모든_단어가_있어야_확정된다():
    rules = [r for r in TRAPS if r.trigger_all]
    for r in rules:
        # trigger_keywords만 있고 trigger_all이 빠진 질의는 걸리지 않아야 한다
        partial = r.trigger_keywords[0]
        missing = [k for k in r.trigger_all if k not in partial]
        if missing:
            assert r.id not in [x.id for x in detect_traps(partial)]


def test_심각도_필터():
    critical = detect_traps("주택 사려고 중도인출하면 세금이 어떻게 되나요",
                            severity_filter="critical")
    assert all(r.severity == "critical" for r in critical)


# ════════════════════════════════════════════════════════════════
# 함정 근거 인용 · critical 교정 강제
# ════════════════════════════════════════════════════════════════
#
# 평가 L-01(doc55) · E-19(doc20) · E-26(doc40)이 3회 연속 실패했다.
# 임베딩을 켜서 검색을 개선해도 같은 문서가 빠진 것이, 이것이 검색 문제가
# 아니라 **인용 경로** 문제임을 증명했다. 함정은 요구사항 슬롯이 아니라서
# 슬롯-근거 매칭에 잡히지 않는다.

def test_checks에_근거_문서가_실린다():
    from app.core.trap_rules import build_trap_context

    ctx = build_trap_context("연금수령연차랑 연금실제수령연차랑 같은 말 아닌가요?")
    b1 = next(c for c in ctx["checks"] if c["id"] == "B1")
    assert b1["docs"] == ["doc40"]


def test_반영한_함정의_문서만_인용된다():
    from app.core.trap_rules import build_trap_context
    from app.pipeline import _addressed_trap_docs

    ctx = build_trap_context("연금수령연차랑 연금실제수령연차랑 같은 말 아닌가요?")
    addressed = "연금수령연차와 연금실제수령연차는 다릅니다. 실제로 인출한 해만 누적됩니다."
    assert "doc40" in _addressed_trap_docs(addressed, ctx["checks"])
    # 다루지 않은 답변은 인용하지 않는다 — 안 쓴 근거를 붙이면 거짓이 된다
    assert _addressed_trap_docs("두 용어는 같은 말입니다.", ctx["checks"]) == {}


def test_critical_함정은_끝내_누락되면_강제_삽입된다():
    """감지·검증·REVISE는 정확히 돌았는데 재생성이 또 빠뜨리면 등급만 낮추고
    나갔다 — 사용자는 틀린 전제를 교정받지 못한 답을 받는다(E-08)."""
    from app.core.coverage_pipeline import TraceLogger
    from app.core.trap_rules import build_trap_context
    from app.pipeline import _enforce_critical_traps

    ctx = build_trap_context("연금 개시하고 11년 됐는데 퇴직소득세 40% 감면 맞나요?")
    out = _enforce_critical_traps("네, 11년차이므로 40% 감면이 적용됩니다.",
                                  ctx["checks"], TraceLogger())
    assert "연금실제수령연차" in out


def test_이미_반영했으면_덧붙이지_않는다():
    """중복 경고는 답변을 읽기 어렵게 만든다."""
    from app.core.coverage_pipeline import TraceLogger
    from app.core.trap_rules import build_trap_context
    from app.pipeline import _enforce_critical_traps

    ctx = build_trap_context("연금 개시하고 11년 됐는데 퇴직소득세 40% 감면 맞나요?")
    good = "감면율은 연금실제수령연차로 결정됩니다."
    assert _enforce_critical_traps(good, ctx["checks"], TraceLogger()) == good


def test_high_이하는_강제하지_않는다():
    """전부 끼워 넣으면 답변이 경고문 더미가 된다."""
    from app.core.coverage_pipeline import TraceLogger
    from app.pipeline import _enforce_critical_traps

    checks = [{"id": "X1", "severity": "high", "correction": "주의하십시오",
               "verify_any": ["없는말"]}]
    assert _enforce_critical_traps("답변", checks, TraceLogger()) == "답변"


# ════════════════════════════════════════════════════════════════
# ask_back이 비면 함정을 감지하고도 단정해 버린다
# ════════════════════════════════════════════════════════════════
#
# 실사고(L-02): "솔로몬 국공채 단기/중장기/장기 뭐가 달라요? 안정적인 걸
# 원해요."에서 D2가 정확히 감지됐는데도 답변이 단정했다. D2의 ask_back이
# 빈 문자열이라 되묻기 후보가 0건이 됐기 때문이다 — 감지는 성공하고
# 그 결과가 답변에 도달하지 못한 유형이다.

def test_D2는_되묻기_문구를_제공한다():
    from app.core.trap_rules import TRAPS

    d2 = next(t for t in TRAPS if t.id == "D2")
    assert (d2.ask_back or "").strip(), "ask_back이 비면 감지하고도 단정한다"


def test_위험등급_비교_질의는_되묻기_후보가_생긴다():
    from app.core.trap_rules import build_trap_context

    ctx = build_trap_context("솔로몬 국공채 단기 중장기 장기 뭐가 달라요? 안정적인 걸 원해요.")
    assert "D2" in ctx["detected"]
    assert ctx["ask_back_candidates"], "되묻기 후보가 0건이면 단정으로 흐른다"


def test_무관한_질의에는_D2_되묻기가_붙지_않는다():
    """D2는 '안전·위험·비교' 같은 넓은 단어로 발동한다 — 확인 항목은
    최대 2건이라 무관한 되묻기가 자리를 차지하면 안 된다."""
    from app.core.trap_rules import build_trap_context

    for q in ("연금수령한도가 얼마인가요?",
              "연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?"):
        ctx = build_trap_context(q)
        assert "D2" not in ctx["detected"], q
