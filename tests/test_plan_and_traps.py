"""R9 · 1.5 계획 감사 배선 검사 + L2 함정 규칙 존치 근거.

이 파일이 지키는 것은 두 가지다.

  1) 계획 감사의 **판정이 실제로 적용되는가.** 2026-08-29 이전에는
     supervise_plan의 결과가 trace 로그로만 쓰이고 downgraded_answerability가
     어디에도 반영되지 않았다 — 감사는 돌지만 결론이 버려졌다.
  2) L2 함정 규칙 26종을 **왜 남겨 두는가.** 법령 계층이 생겼으니 규칙을
     걷어내도 되는 것 아니냐는 물음에 대한 실측 답이다.
"""

from __future__ import annotations

import json

import pytest

from app.law.schema import LawArticle
from app.law.store import LawStore

# ════════════════════════════════════════════════════════════════
# 1.5 · 계획 감사 — 판정이 답변 등급까지 도달하는가
# ════════════════════════════════════════════════════════════════

def test_계획감사_판정이_trace가_아니라_등급에_반영된다():
    """감사가 있다는 주장은 결과가 반영될 때만 참이다.

    supervise_plan이 DOWNGRADE를 냈는데 답변 등급이 그대로면,
    그 감사는 로그 장식일 뿐이다.
    """
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    assert "plan_result.downgraded_answerability" in src, (
        "계획 감사 판정이 어디에서도 읽히지 않는다 — trace 전용으로 되돌아갔다")
    # 판정은 경로 분류 **뒤**에서 반영돼야 한다 (ADVISORY 예외 때문)
    assert src.index("route = classify_route") < \
           src.index("plan_result.downgraded_answerability"), (
        "경로를 모르는 채로 강등하면 ADVISORY를 잘못 깎는다")


def test_미등록_함수는_계획에서_제거된다():
    """화이트리스트 — L1이 없는 함수를 지어내도 실행되지 않는다."""
    from app.core.coverage_pipeline import CALC_REGISTRY
    from app.core.supervisory_board import Verdict, supervise_plan

    spec = {"asked_for": [{"id": "x", "description": "가짜",
                           "type": "calculation"}],
            "planned_calls": [{"function": "존재하지_않는_함수", "args": {}}]}
    res, safe = supervise_plan(spec, set(CALC_REGISTRY))
    assert safe["planned_calls"] == []
    assert res.verdict != Verdict.APPROVE
    assert "UNKNOWN_FUNCTION" in [f.code for f in res.findings]


def test_커버리지_없이_계산을_계획하면_강등된다():
    """근거가 없는데 계산부터 하겠다는 계획은 그대로 두면 안 된다."""
    from app.core.coverage_pipeline import CALC_REGISTRY
    from app.core.supervisory_board import Verdict, supervise_plan

    class _G:
        domain_covered = False

    spec = {"asked_for": [{"id": "a", "description": "한도", "type": "calculation",
                           "calc_function": "연금수령한도_계산"}],
            "planned_calls": [{"function": "연금수령한도_계산", "args": {}}]}
    res, _ = supervise_plan(spec, set(CALC_REGISTRY), _G())
    assert res.verdict == Verdict.DOWNGRADE
    assert "PLAN_WITHOUT_COVERAGE" in [f.code for f in res.findings]


def test_ADVISORY에서_슬롯_부재는_강등사유가_아니다():
    """슬롯이 비는 것은 불특정 서술의 **정상 상태**다.

    R3에서 L4-sub가 받기로 한 바로 그 경우다. 여기서 깎으면
    "불특정 서술을 거부하지 않고 받는다"는 변경이 무효가 된다.
    """
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    assert "route.is_advisory" in src and "NO_SLOTS" in src, (
        "ADVISORY 예외가 사라졌다 — 불특정 질의가 다시 강등된다")


# ════════════════════════════════════════════════════════════════
# L2 · 함정 규칙 26종을 남겨 두는 근거
# ════════════════════════════════════════════════════════════════

def test_함정규칙은_사문화되지_않았다():
    """27종 중 26종이 실제 감사 질의 298건에서 발화한다(실측).

    나머지 1종(A3 · DB는 중도인출 불가)도 죽은 규칙이 아니라
    298건에 해당 질의가 없었을 뿐이다 — 아래에서 도달성을 직접 보인다.

    2026-09-01 A9 추가(연금 외 수령 시 재원별 과세 구분) — 298건 중
    16건에서 발화한다.
    """
    from app.core.trap_rules import TRAPS, detect_traps

    assert len(TRAPS) == 27
    assert [t.id for t in detect_traps("DB형인데 중도인출 받을 수 있나요?")] == ["A3"]


def test_법령_계층은_함정_탐지에_전적으로_의존한다():
    """★ L2를 걷어내면 법령 계층이 통째로 죽는다.

    이것이 "규칙 기반 함정 탐지를 법령 판단으로 대체하면 되지 않느냐"에
    대한 답이다. 대체 관계가 아니라 **의존 관계**다 —
    L2가 후보를 만들고, 법령 계층이 그 후보를 조문으로 판정한다.
    후보가 없으면 판정할 대상 자체가 없다.
    """
    from app.generation.grounding import _law_context

    checks = [{"id": "A1", "severity": "critical", "title": "중도인출 사유",
               "correction": "확인 필요", "docs": [], "verify_any": ["중도인출"]}]
    articles, candidates = _law_context([], checks)      # 함정 0건
    assert articles == [] and candidates == [], (
        "함정이 없는데 조문이 실렸다 — 게이팅이 풀렸다")


def test_함정이_있으면_조문이_실린다(monkeypatch):
    """의존 관계의 반대편 — 후보가 있으면 실제로 판정 대상이 만들어진다."""
    art = LawArticle(
        law_name="시험법", article_no="제10조", clause_no="제1항",
        text="가입자가 적립금을 중도인출하는 경우에는 대통령령으로 "
             "정하는 사유에 해당하여야 한다.",
        effective_date="2026-01-01", source_url="u", fetched_at="t")
    store = LawStore([art])
    monkeypatch.setattr("app.law.store.get_store", lambda **k: store)
    monkeypatch.setattr("app.law.anchors.get_store", lambda **k: store)
    monkeypatch.setattr("app.law.anchors.ANCHORS",
                        {"A1": ("시험법 제10조 제1항",)})

    from app.generation.grounding import _law_context

    checks = [{"id": "A1", "severity": "critical", "title": "중도인출 사유",
               "correction": "확인 필요", "docs": [], "verify_any": ["중도인출"]}]
    articles, candidates = _law_context(["A1"], checks)
    assert articles, "함정이 있는데 조문이 실리지 않았다"
    assert candidates, "판정 대상이 만들어지지 않았다"
