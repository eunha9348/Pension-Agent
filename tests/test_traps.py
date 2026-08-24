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


# ════════════════════════════════════════════════════════════════
# 교정문 자기충족성 — 강제 삽입이 실제로 판정을 푸는가
# ════════════════════════════════════════════════════════════════
# _enforce_critical_traps는 미반영 critical 함정의 correction을 답변에
# 덧붙인다. 그런데 그 correction이 **자기 검증용어를 담지 않으면**,
# 삽입해도 미반영 판정이 그대로 남는다 — 안전망이 돌긴 도는데 아무것도
# 고치지 못한다. C1이 정확히 그 상태였다(평가 E-09 '전액' 누락).
#
# 규칙별 개별 테스트로는 이런 결함을 또 놓친다. 26종 전수로 못 박는다.

def test_교정문은_자기_검증용어를_담는다():
    from app.core.trap_rules import TRAPS, term_present, verify_terms_for

    broken = []
    for r in TRAPS:
        terms = verify_terms_for(r.id)
        corr = (r.correction or "").strip()
        # correction이 빈 규칙은 '임의 판단 금지'라 의도적으로 비워 둔 것이다
        # (C6). 강제 삽입 대상도 아니므로 검사하지 않는다.
        if not terms or not corr:
            continue
        if not any(term_present(corr, t) for t in terms):
            broken.append((r.id, terms))

    assert not broken, (
        "교정문을 삽입해도 미반영 판정이 풀리지 않는 규칙: "
        + ", ".join(f"{i}(검증용어 {t})" for i, t in broken))


def test_C1_교정문은_전액을_명시한다():
    """초과분 과세 오해를 푸는 문장에서 '전액'은 빠질 수 없는 단어다."""
    from app.core.trap_rules import TRAPS

    c1 = next(t for t in TRAPS if t.id == "C1")
    assert "전액" in c1.correction
    assert "초과분" in c1.correction


def test_critical_함정_강제삽입은_한_번에_해소된다():
    """삽입 후 다시 검사하면 미반영 목록이 비어야 한다.

    비지 않으면 같은 문장을 몇 번이고 덧붙이는 무한 반복이 된다.
    """
    from app.core.trap_rules import build_trap_context, unaddressed_traps

    ctx = build_trap_context(
        "연금을 연 2000만원 받으면 1500만원 넘는 500만원에만 세금 붙나요?")
    assert "C1" in ctx["detected"]

    draft = "1,500만원을 초과하면 분리과세 또는 종합과세를 선택할 수 있습니다."
    remaining = [t for t in unaddressed_traps(draft, ctx["checks"])
                 if t.get("severity") == "critical" and t.get("correction")]
    assert remaining, "이 질의는 critical 함정이 미반영 상태여야 한다"

    draft += "\n\n" + "\n".join(f"※ {t['correction']}" for t in remaining)

    still = [t for t in unaddressed_traps(draft, ctx["checks"])
             if t.get("severity") == "critical" and t.get("correction")]
    assert not still, f"삽입 후에도 미반영으로 남는 함정: {[t['id'] for t in still]}"
    assert "전액" in draft


# ════════════════════════════════════════════════════════════════
# 오탐 억제 — 넓은 주제어가 엉뚱한 질의를 끌어오지 않는다
# ════════════════════════════════════════════════════════════════
# 2026-08-24 실배포에서 "연금저축이랑 IRP 합쳐서 세액공제 얼마까지"에
# 함정 4건이 걸렸고 그 중 3건이 오탐이었다. 결과는 두 가지였다.
#   · 1,500만원 분리과세 설명(C2)과 구법 경고(C5)가 답변에 강제 삽입
#   · 무관한 B2 미반영을 이유로 답변 등급이 ASK_BACK으로 강등
# 원인은 주제어가 너무 넓었던 것이다 — '총'이 총보수를, '한도'가
# 세액공제 한도를, '세액공제'가 세제 질의 전부를 걸었다.

_FP_QUERIES = {
    "E-01": "연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?",
    "E-02": "총급여 4000만원인데 연금저축에 600만원 넣으면 세액공제 얼마인가요?",
    "E-04": "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?",
    "E-05": "1억이고 연금수령 10년차면 한도가 어떻게 되나요?",
    "E-07": "연금수령한도가 얼마인가요?",
    "E-09": "연금을 연 2000만원 받으면 1500만원 넘는 500만원에만 세금 붙나요?",
    "E-15": "총보수가 가장 낮은 클래스로 가입하고 싶은데 어떤 게 좋나요?",
    "E-33": "ISA 만기 자금을 연금계좌로 옮기면 1800만원 한도에 포함되나요?",
}


@pytest.mark.parametrize("qid,question", sorted(_FP_QUERIES.items()))
def test_B2는_수령한도_연차_맥락에서만_걸린다(qid, question):
    """'한도'·'얼마까지'로 걸리면 세액공제·납입한도 질의가 전부 끌려온다."""
    from app.core.trap_rules import detect_traps

    assert "B2" not in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("qid,question", sorted(_FP_QUERIES.items()))
def test_C2는_1500만원_합산_맥락에서만_걸린다(qid, question):
    """'총'이 '총보수'를, '합쳐서'가 '연금저축이랑 IRP 합쳐서'를 걸었다."""
    from app.core.trap_rules import detect_traps

    assert "C2" not in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("qid,question", sorted(_FP_QUERIES.items()))
def test_C5는_구법_수치가_등장할_때만_걸린다(qid, question):
    """답변이 현행 수치를 쓰는 한 구법 경고를 붙일 이유가 없다."""
    from app.core.trap_rules import detect_traps

    assert "C5" not in [t.id for t in detect_traps(question)], question


# ── 정탐은 그대로 살아 있어야 한다 ─────────────────────────────
# 오탐만 보고 조이면 감지를 통째로 죽이게 된다. 양쪽을 함께 못 박는다.

@pytest.mark.parametrize("trap_id,question", [
    ("B2", "연금 받은 지 12년 됐는데 아직도 인출한도가 있나요?"),
    ("C2", "국민연금까지 합쳐서 1500만원 넘으면 분리과세 선택해야 하나요?"),
    ("C5", "자료에 세액공제 한도가 700만원이라고 나오는데 맞나요?"),
])
def test_정탐은_유지된다(trap_id, question):
    from app.core.trap_rules import detect_traps

    assert trap_id in [t.id for t in detect_traps(question)], question


def test_C5_교정문은_답변을_구법이라고_부정하지_않는다():
    """지시대명사를 쓰면 맞는 답에 붙었을 때 그 답을 부정한다.

    예전 문구 "해당 수치는 개정 전 기준입니다"는 답변이 현행 수치를
    정확히 제시한 경우에도 그대로 삽입돼, 맞는 답을 구법으로 만들었다.
    """
    from app.core.trap_rules import TRAPS

    c5 = next(t for t in TRAPS if t.id == "C5")
    assert "해당 수치는 개정 전 기준" not in c5.correction
    # 현행 기준을 직접 밝힌다 — 무엇이 현행인지 문장 자체가 담아야 한다
    assert "600만원" in c5.correction and "900만원" in c5.correction
    assert "현행" in c5.correction


def test_트리거_맥락조건이_실제로_좁힌다():
    """trigger_context가 붙은 규칙은 주제어만으로는 걸리지 않아야 한다."""
    from app.core.trap_rules import TRAPS, detect_traps

    for r in TRAPS:
        if not r.trigger_context:
            continue
        bare = r.trigger_keywords[0]
        if any(c in bare for c in r.trigger_context):
            continue        # 주제어가 맥락어를 이미 품은 경우는 건너뛴다
        assert r.id not in [t.id for t in detect_traps(bare)], (
            f"{r.id}: 주제어 '{bare}'만으로 확정됐다 — 맥락 조건이 무력하다")


# ════════════════════════════════════════════════════════════════
# 미탐 — 사람이 실제로 쓰는 표현을 놓치지 않는다
# ════════════════════════════════════════════════════════════════
# 오탐 감사 중에 발견됐다. 평가셋은 42문항 전부 통과하는데도 두 함정이
# 조용히 안 잡히고 있었다 — 함정 라벨은 채점 대상이 아니기 때문이다.
#
#   A7  "IRP에서 3000만원만 빼서 쓸 수 있나요?"   → 부분인출 불가 경고 없음
#   E6  "작년 퇴직금이랑 올해 퇴직금 정산이…"      → 정산특례 경고 없음
#
# A7이 놓친 이유가 핵심이다. 사람은 부분 인출을 '일부·부분'이라 말하지 않고
# **금액을 집어서** 말한다("3000만원만 빼서").

@pytest.mark.parametrize("question", [
    "IRP에서 3000만원만 빼서 쓸 수 있나요?",
    "IRP에서 500만원만 인출할 수 있나요?",
    "IRP에서 필요한 만큼만 찾아 쓸 수 있나요?",
    "IRP 일부만 해지하고 싶어요",
])
def test_A7은_금액을_집은_부분인출_표현도_잡는다(question):
    from app.core.trap_rules import detect_traps

    assert "A7" in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("question", [
    "작년 퇴직금이랑 올해 퇴직금 정산이 어떻게 되나요?",
    "퇴직금 정산 특례가 뭔가요?",
    "퇴직금을 두 번 받았는데 세금은 어떻게 되나요?",
    "중간정산 받은 게 있는데 합산되나요?",
])
def test_E6은_퇴직금_정산_표현을_잡는다(question):
    from app.core.trap_rules import detect_traps

    assert "E6" in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("question", [
    # 연말정산은 전혀 다른 제도다 — 맨 '정산'을 넣었다면 여기서 걸렸을 것이다
    "연말정산 때 뭐 챙겨야 하나요?",
    "연말정산에서 연금저축 공제 받으려면 어떻게 하나요?",
])
def test_E6은_연말정산을_끌어오지_않는다(question):
    from app.core.trap_rules import detect_traps

    assert "E6" not in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("question", [
    "집 사려고 IRP에서 중도인출하면 세금이 어떻게 되나요?",
    "연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?",
    "IRP 계좌를 새로 만들고 싶은데 어떻게 하나요?",
    "퇴직금을 IRP 말고 제 통장으로 바로 받을 수 있나요?",
])
def test_A7은_부분인출_맥락이_없으면_걸리지_않는다(question):
    """IRP가 나왔다는 이유만으로 '전액 해지해야 합니다'를 붙이면 안 된다."""
    from app.core.trap_rules import detect_traps

    assert "A7" not in [t.id for t in detect_traps(question)], question


def test_평가셋_전문항의_기대_함정이_전부_감지된다():
    """문항별로 하나씩 놓치지 않도록 전수로 못 박는다."""
    from app.core.trap_rules import detect_traps
    from tests.eval_set import EVAL_CASES

    missed = []
    for case in EVAL_CASES:
        if not case.trap:
            continue
        expected = [e.strip().split(" ")[0]
                    for e in case.trap.replace("—", " ").split("·") if e.strip()]
        expected = [e for e in expected if len(e) == 2 and e[0].isalpha()]
        got = [t.id for t in detect_traps(case.question)]
        lost = [e for e in expected if e not in got]
        if lost:
            missed.append((case.id, lost))
    assert not missed, f"기대 함정을 놓친 문항: {missed}"


# ════════════════════════════════════════════════════════════════
# B2 — 세야 하는 건 연금수령연차지 근속연수가 아니다
# ════════════════════════════════════════════════════════════════
# 오탐을 좁히면서 맥락어로 맨 "N년"(11~40)을 썼는데, 이게 근속연수·
# 가입기간·근무기간까지 연금수령연차로 착각했다. 평가셋 42문항에는
# 그런 질의가 없어 드러나지 않았다 — 평가셋이 좁아서지 안전해서가 아니다.
#
#   "근속 20년인데 연금수령한도가 얼마인가요?"  → B2 발동 (근속 ≠ 수령연차)
#
# B2는 medium이라 강제 삽입은 안 되지만, 미반영 시 답변 등급을 깎는다.

@pytest.mark.parametrize("question", [
    "근속 20년인데 연금수령한도가 얼마인가요?",
    "가입한 지 15년 된 계좌인데 연금수령한도가 어떻게 되나요?",
    "30년 근무하고 퇴직하는데 인출한도가 있나요?",
    "연금수령한도가 얼마인가요?",
])
def test_B2는_근속연수를_연금수령연차로_읽지_않는다(question):
    from app.core.trap_rules import detect_traps

    assert "B2" not in [t.id for t in detect_traps(question)], question


@pytest.mark.parametrize("question", [
    "연금 받은 지 12년 됐는데 아직도 인출한도가 있나요?",
    "연금수령 12년차인데 수령한도가 있나요?",
    "연금 개시하고 15년 됐는데 인출한도가 남아있나요?",
    "연금수령한도는 언제까지 적용되나요?",
])
def test_B2는_연금수령_연차_맥락은_잡는다(question):
    from app.core.trap_rules import detect_traps

    assert "B2" in [t.id for t in detect_traps(question)], question
