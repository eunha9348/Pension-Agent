"""법령 계층 ↔ 감독 이사회(L6) 배선 테스트.

부품이 아무리 잘 만들어져 있어도 실제 답변 흐름이 호출하지 않으면
아무 일도 일어나지 않는다. 이 파일은 **배선 자체**를 검사한다:

  · 조문과 판정 대상이 감사 페이로드에 실리는가
  · 검증을 통과한 판정이 함정 목록을 바꾸는가
  · 바뀐 목록으로 결정론적 감사가 **다시** 도는가
  · 지어낸 근거는 아무것도 바꾸지 못하는가  ★
  · 법령 수집 전에도 예전과 똑같이 동작하는가 (무해성)
"""

from __future__ import annotations

import json

import pytest

from app.core.supervisory_board import (Verdict, build_llm_audit_payload,
                                        supervise, supervise_hybrid)
from app.law.schema import LawArticle
from app.law.store import LawStore

_ART = LawArticle(
    law_name="시험법", article_no="제10조", clause_no="제1항",
    text="가입자가 적립금을 중도인출하는 경우에는 대통령령으로 "
         "정하는 사유에 해당하여야 한다.",
    effective_date="2026-01-01", source_url="u", fetched_at="t")

_QUOTE = "대통령령으로 정하는 사유에 해당하여야 한다"

_CHECKS = [{"id": "A1", "severity": "critical", "title": "중도인출 사유",
            "correction": "중도인출 사유를 확인해야 합니다",
            "docs": [], "verify_any": ["중도인출"]}]


@pytest.fixture(autouse=True)
def _law_store(monkeypatch):
    """모든 테스트가 픽스처 저장소를 보게 한다 (실제 수집본과 무관)."""
    store = LawStore([_ART])
    monkeypatch.setattr("app.law.store.get_store", lambda **k: store)
    monkeypatch.setattr("app.law.anchors.get_store", lambda **k: store)
    monkeypatch.setattr("app.law.anchors.ANCHORS", {"A1": ("시험법 제10조 제1항",)})
    return store


def _audit(verdict="APPROVE", judgements=None) -> str:
    body = {"verdict": verdict, "findings": []}
    if judgements is not None:
        body["law_judgements"] = judgements
    return json.dumps(body, ensure_ascii=False)


def _hybrid(raw: str, trap_ids=("A1",), **kw):
    """감사 LLM이 raw를 돌려준다고 가정하고 supervise_hybrid를 돌린다."""
    from app.generation.grounding import _law_context

    articles, candidates = _law_context(list(trap_ids), _CHECKS)
    return supervise_hybrid(
        answer="중도인출에 대한 답변입니다.",
        question="중도인출 되나요?",
        llm_call=lambda s, u: raw,
        evidence_texts=[],
        law_articles=articles, candidate_traps=candidates,
        trap_ids=list(trap_ids), trap_checks=_CHECKS, **kw)


# ════════════════════════════════════════════════════════════════
# ★ 끝에서 끝까지 — 공개 API를 실제로 지나가는가
# ════════════════════════════════════════════════════════════════
# ⚠️ 아래 다른 테스트들은 _law_context와 supervise_hybrid를 **직접 이어
#    붙여** 배선을 흉내낸다. 그래서 부품이 다 멀쩡하면 통과해 버린다.
#    실제로 며칠간 배포됐던 결함이 정확히 그 형태였다 — 페이로드 빌더는
#    law_articles를 받을 수 있었고 _law_context도 있었지만, 호출자가 그
#    결과를 넘기지 않아 법령 계층이 통째로 죽어 있었다. 그런 상태에서도
#    이 파일의 나머지 16건은 전부 통과했다(실측).
#
#    아래 두 건만이 make_verify_grounding이라는 **공개 진입점**으로 들어가
#    감사 페이로드에 조문이 실제로 도달하는지를 본다. 배선을 검사하는
#    테스트는 배선을 지나가야 한다.

def _run_pipeline(llm_call, answer="중도인출에 대한 답변입니다.",
                  trap_ids=("A1",)):
    """make_verify_grounding — 파이프라인이 실제로 쓰는 진입점."""
    from app.generation.grounding import make_verify_grounding

    vg = make_verify_grounding(
        question="중도인출 되나요?", slots=[], llm_call=llm_call,
        trap_ids=list(trap_ids), trap_checks=_CHECKS)
    return vg(answer, [])


def test_공개진입점을_통해_조문이_감사_페이로드에_도달한다():
    seen: dict = {}

    def spy(system, payload):
        seen["payload"] = payload
        return _audit()

    _run_pipeline(spy)
    assert "payload" in seen, "감사 LLM이 호출되지 않았다"
    assert _QUOTE in seen["payload"], (
        "조문 원문이 감사 페이로드에 도달하지 않았다 — "
        "make_verify_grounding이 법령 컨텍스트를 넘기지 않는다")
    assert "law_judgements" in seen["payload"], "판정 지시가 누락됐다"


def test_공개진입점을_통해_검증된_판정이_실제로_반영된다():
    """부품이 아니라 흐름 전체가 동작하는지 — 판정이 최종 결과를 바꿔야 한다."""
    verdict = _run_pipeline(
        lambda s, u: _audit(judgements=[{
            "trap_id": "A1", "applies": False,
            "law_ref": "시험법 제10조 제1항", "quote": _QUOTE}]),
        answer="무관한 답변입니다.")
    codes = [f.code for f in verdict.supervision.findings]
    assert "TRAP_ADJUSTED" in codes, (
        "검증된 판정이 파이프라인 끝까지 반영되지 않았다")


# ════════════════════════════════════════════════════════════════
# 페이로드 — 조문이 감사자에게 실제로 전달되는가
# ════════════════════════════════════════════════════════════════

def test_법령_컨텍스트가_등재된_함정만_고른다():
    from app.generation.grounding import _law_context

    articles, candidates = _law_context(["A1", "B2"], _CHECKS)
    assert [a.ref for a in articles] == ["시험법 제10조 제1항"]
    assert [c["id"] for c in candidates] == ["A1"], "미등재 B2가 섞였다"


def test_조문_원문과_인용_강제_지시가_페이로드에_실린다():
    payload = build_llm_audit_payload(
        answer="답변", evidence_texts=[], calc_results=[], question="질문",
        law_articles=[_ART],
        candidate_traps=[{"id": "A1", "title": "중도인출 사유",
                          "verify_any": ["중도인출"]}])
    assert _QUOTE in payload
    assert "글자 그대로" in payload
    assert "law_judgements" in payload
    assert "A1" in payload


def test_긴_조문은_검증용어_주변을_실어_보낸다():
    """앞에서 자르면 정작 필요한 조항이 밀려난다(소득세법 제14조 제3항 사례)."""
    long_art = LawArticle(
        "시험법", "제14조", "제3항",
        "머리말. " + ("무관한 호가 길게 이어진다. " * 200)
        + "9. 핵심 조항은 여기 뒤쪽에 있다 중도인출 기준.",
        "2026-01-01", "u", "t")
    payload = build_llm_audit_payload(
        answer="답변", evidence_texts=[], calc_results=[], question="질문",
        law_articles=[long_art],
        candidate_traps=[{"id": "A1", "title": "t", "verify_any": ["중도인출"]}])
    assert "핵심 조항은 여기 뒤쪽에 있다" in payload


# ════════════════════════════════════════════════════════════════
# 반영 — 검증된 판정이 실제로 판정을 바꾸는가
# ════════════════════════════════════════════════════════════════

def test_검증된_제거판정이_함정을_빼고_감사를_다시_돌린다():
    res = _hybrid(_audit(judgements=[{
        "trap_id": "A1", "applies": False,
        "law_ref": "시험법 제10조 제1항", "quote": _QUOTE}]))
    codes = [f.code for f in res.findings]
    assert "TRAP_ADJUSTED" in codes, "함정 목록이 조정되지 않았다"
    detail = " ".join(f.detail for f in res.findings)
    assert "['A1'] → []" in detail


def test_미해소_함정_지적이_제거와_함께_사라진다():
    """A1을 안 다룬 답변이라 결정론적 감사는 원래 이를 지적한다.

    조문 근거로 A1이 빠지면 그 지적도 함께 사라져야 한다 — 남아 있으면
    없는 함정을 이유로 답변을 계속 깎는다.
    """
    before = supervise("무관한 답변입니다.", trap_ids=["A1"], trap_checks=_CHECKS)
    assert before.verdict != Verdict.APPROVE, "전제 확인: 원래는 지적된다"

    res = supervise_hybrid(
        answer="무관한 답변입니다.", question="q",
        llm_call=lambda s, u: _audit(judgements=[{
            "trap_id": "A1", "applies": False,
            "law_ref": "시험법 제10조 제1항", "quote": _QUOTE}]),
        evidence_texts=[], law_articles=[_ART],
        candidate_traps=[{"id": "A1", "title": "t", "verify_any": ["중도인출"]}],
        trap_ids=["A1"], trap_checks=_CHECKS)
    assert res.verdict == Verdict.APPROVE, [f.detail for f in res.findings]


# ════════════════════════════════════════════════════════════════
# 차단 ★ — 지어낸 근거는 아무것도 바꾸지 못한다
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("label,ref,quote", [
    ("조문 창작", "시험법 제999조", "있지도 않은 조문에서 옮긴 인용문입니다"),
    ("내용 창작", "시험법 제10조 제1항", "중도인출은 사유 제한 없이 허용된다"),
    ("의역",     "시험법 제10조 제1항", "대통령령이 정하는 사유에 해당해야 합니다"),
    ("짧은 인용", "시험법 제10조 제1항", "중도인출"),
])
def test_지어낸_근거는_함정을_제거하지_못한다(label, ref, quote):
    res = _hybrid(_audit(judgements=[{
        "trap_id": "A1", "applies": False, "law_ref": ref, "quote": quote}]))
    codes = [f.code for f in res.findings]
    assert "TRAP_ADJUSTED" not in codes, f"{label}으로 함정이 제거됐다 — 차단선이 뚫렸다"
    detail = " ".join(f.detail for f in res.findings)
    assert "폐기" in detail, f"{label} 폐기 사유가 기록되지 않았다"


def test_의미감사의_단조성은_그대로다():
    """법령 판정이 붙어도 LLM verdict는 심각도를 올리기만 해야 한다."""
    res = _hybrid(_audit(verdict="REVISE", judgements=[{
        "trap_id": "A1", "applies": True,
        "law_ref": "시험법 제10조 제1항", "quote": _QUOTE}]))
    assert res.verdict == Verdict.REVISE


def test_LLM이_APPROVE해도_결정론적_REVISE는_유지된다():
    answer = "A상품을 추천드립니다."          # 결정론적 준법 감사가 REVISE로 잡는다
    assert supervise(answer).verdict == Verdict.REVISE, "전제 확인"

    res = supervise_hybrid(
        answer=answer, question="q",
        llm_call=lambda s, u: _audit(verdict="APPROVE"),
        evidence_texts=[], trap_ids=[], trap_checks=[])
    assert res.verdict in (Verdict.REVISE, Verdict.DOWNGRADE, Verdict.BLOCK)


# ════════════════════════════════════════════════════════════════
# 무해성 — 법령이 없어도 예전과 똑같이 돈다
# ════════════════════════════════════════════════════════════════

def test_저장소가_비면_법령_계층이_통째로_비활성된다(monkeypatch):
    empty = LawStore([])
    monkeypatch.setattr("app.law.store.get_store", lambda **k: empty)
    monkeypatch.setattr("app.law.anchors.get_store", lambda **k: empty)
    from app.generation.grounding import _law_context

    assert _law_context(["A1"], _CHECKS) == ([], [])


def test_등재된_앵커가_없으면_판정_대상도_없다(monkeypatch):
    monkeypatch.setattr("app.law.anchors.ANCHORS", {})
    from app.generation.grounding import _law_context

    assert _law_context(["A1"], _CHECKS) == ([], [])


def test_법령_계층이_터져도_감사는_계속된다(monkeypatch):
    """법령 쪽 사고가 감사 전체를 죽이면 안 된다."""
    def boom(*a, **k):
        raise RuntimeError("저장소 폭발")
    monkeypatch.setattr("app.law.citation_guard.verify_judgements", boom)

    res = _hybrid(_audit(judgements=[{
        "trap_id": "A1", "applies": False,
        "law_ref": "시험법 제10조 제1항", "quote": _QUOTE}]))
    assert res.verdict is not None
    assert any("실패" in f.detail for f in res.findings)


def test_판정이_없으면_아무것도_바뀌지_않는다():
    res = _hybrid(_audit())          # law_judgements 자체가 없음
    assert "TRAP_ADJUSTED" not in [f.code for f in res.findings]


def test_LLM_호출은_여전히_1회다():
    """법령 판정을 기존 감사 호출에 접었다 — 호출이 늘면 예산이 무너진다."""
    calls = []
    supervise_hybrid(
        answer="답변", question="q",
        llm_call=lambda s, u: (calls.append(1), _audit())[1],
        evidence_texts=[], law_articles=[_ART],
        candidate_traps=[{"id": "A1", "title": "t", "verify_any": ["중도인출"]}],
        trap_ids=["A1"], trap_checks=_CHECKS)
    assert len(calls) == 1
