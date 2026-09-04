"""저촉 검사가 **공개 진입점을 통해** 실제로 배선돼 있는가.

━━ 왜 별도 파일인가 ━━
CLAUDE.md: "배선을 검사하는 테스트는 배선을 지나가야 한다. 부품을 직접
이어 붙여 흉내낸 테스트는 호출자가 결과를 안 넘겨도 통과한다. 실제로
법령 계층이 통째로 죽은 채 배포된 적이 있는데, 그 상태에서 관련 테스트
16건이 전부 통과했다."

test_law_conflict.py는 부품(선택기·검증기·판정 반영)을 각각 시험한다.
이 파일은 `make_verify_grounding()`으로 들어가, 조문이 감사 페이로드에
실리고 저촉 판정이 실제 등급으로 돌아오는지를 본다. 중간을 손으로
이어 붙이지 않는다.
"""

from __future__ import annotations

import json

import pytest

from app.core.coverage_pipeline import EvidenceChunk
from app.core.supervisory_board import Verdict
from app.generation.grounding import make_verify_grounding
from app.law.schema import LawArticle
from app.law.store import LawStore

_ART = LawArticle(
    law_name="시험소득세법", article_no="제14조", clause_no="제3항",
    text=("가목 및 나목 외의 연금소득의 합계액이 연 1천500만원 이하인 경우 "
          "그 연금소득. 다만 이연퇴직소득을 연금수령하는 경우의 연금소득은 "
          "합계액에 포함하지 아니한다."),
    effective_date="2024-01-01", source_url="", fetched_at="")

_ANSWER = ("연간 연금소득이 1,500만원을 넘으면 초과분에 대해서만 "
           "종합과세됩니다. 나머지는 그대로 분리과세됩니다.")
_QUESTION = "연금소득이 1,500만원을 넘으면 세금이 어떻게 되나요?"
_EVIDENCE = [EvidenceChunk(doc_id="doc1", score=0.9,
                           text="연금소득 과세 관련 안내입니다.")]

_QUOTE = "연금소득의 합계액이 연 1천500만원 이하인 경우"
_SPAN = "초과분에 대해서만 종합과세됩니다"


@pytest.fixture(autouse=True)
def _law_store(monkeypatch):
    """법령 저장소를 시험용 조문 하나로 갈아 끼운다.

    relevance는 저장소 객체마다 색인을 새로 만들므로 캐시 오염이 없다.
    """
    store = LawStore([_ART])
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    return store


class _Auditor:
    """감사자 대역. 받은 페이로드를 그대로 보관한다."""

    def __init__(self, conflicts=None):
        self.payloads: list[str] = []
        self.conflicts = conflicts or []

    def __call__(self, system: str, payload: str) -> str:
        self.payloads.append(payload)
        return json.dumps({"verdict": "APPROVE", "findings": [],
                           "law_conflicts": self.conflicts},
                          ensure_ascii=False)


def _verify(auditor, trap_ids=None):
    vg = make_verify_grounding(question=_QUESTION, slots=[],
                               llm_call=auditor, citations=["근거1"],
                               trap_ids=trap_ids or [], trap_checks=[])
    return vg(_ANSWER, _EVIDENCE)


# ════════════════════════════════════════════════════════════
# 1. 조문이 감사자에게 실제로 도달하는가
# ════════════════════════════════════════════════════════════

def test_함정이_0건이어도_조문이_페이로드에_실린다():
    """★ 이 배선이 없으면 실측 55%의 질의에서 저촉 검사가 죽는다.

    trap_ids를 비워 두는 것이 핵심이다 — 예전 `_law_context`는 여기서
    빈 값을 돌려주고 끝났다.
    """
    a = _Auditor()
    _verify(a, trap_ids=[])

    assert a.payloads, "감사자가 호출되지 않았다"
    p = a.payloads[0]
    assert "시험소득세법 제14조 제3항" in p, "조문이 페이로드에 실리지 않았다"
    assert "law_conflicts" in p, "저촉 판정을 요구하지 않았다"


def test_조문_원문이_그대로_실린다():
    """인용 검증은 원문 대조라, 감사자가 원문을 못 보면 통과할 수 없다."""
    a = _Auditor()
    _verify(a, trap_ids=[])
    assert _QUOTE in a.payloads[0]


def test_수집본이_비면_저촉_검사를_못_했다고_남긴다(monkeypatch):
    """★ '검사했는데 저촉 없음'과 '검사를 못 함'은 다른 사건이다."""
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: LawStore([]))
    v = _verify(_Auditor(), trap_ids=[])

    codes = [(f.auditor, f.code) for f in v.supervision.findings]
    assert ("법령저촉", "NOT_RUN") in codes, codes


def test_조문이_있으면_저촉_없음도_기록된다():
    """아무 기록이 없으면 검사가 돌았는지 밖에서 알 수 없다."""
    v = _verify(_Auditor(conflicts=[]), trap_ids=[])
    codes = [(f.auditor, f.code) for f in v.supervision.findings]
    assert ("법령저촉", "NONE") in codes, codes


# ════════════════════════════════════════════════════════════
# 2. 판정이 실제 등급으로 돌아오는가
# ════════════════════════════════════════════════════════════

def test_검증된_저촉이_등급에_반영된다():
    """★ "감사가 있다는 주장은 결과가 반영될 때만 참이다" (CLAUDE.md)."""
    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": _SPAN, "conflict": "조문은 합계액 전체를 대상으로 한다"}])
    v = _verify(a, trap_ids=[])

    assert v.supervision.verdict == Verdict.REVISE, (
        f"저촉이 확인됐는데 등급이 {v.supervision.verdict}다")
    assert not bool(v), "저촉이 있는데 검증을 통과시켰다"


def test_저촉_지시가_재생성으로_전달된다():
    """지시가 directives에 없으면 재생성이 무엇을 고칠지 모른다."""
    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": _SPAN, "conflict": "조문은 합계액 전체를 대상으로 한다"}])
    v = _verify(a, trap_ids=[])

    joined = " ".join(v.supervision.directives)
    assert _SPAN in joined and "제14조" in joined, joined


def test_지어낸_저촉은_등급을_바꾸지_못한다():
    """★ 답변에 없는 문장을 지목한 경우 — 오탐이 강제 강등이 되면 안 된다."""
    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": "1,500만원을 넘으면 전액 종합과세됩니다",   # 답변에 없다
        "conflict": "어긋남"}])
    v = _verify(a, trap_ids=[])

    assert v.supervision.verdict != Verdict.REVISE or \
        not any(f.code == "LAW_CONFLICT" for f in v.supervision.findings), \
        "답변에 없는 문장을 지목한 저촉이 채택됐다"


def test_LLM_호출은_여전히_1회다():
    """★ 저촉 검사를 별도 호출로 만들면 단일 GET의 예산이 무너진다."""
    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": _SPAN, "conflict": "어긋남"}])
    _verify(a, trap_ids=[])
    assert len(a.payloads) == 1, f"감사 호출이 {len(a.payloads)}회로 늘었다"


# ════════════════════════════════════════════════════════════
# 3. Sub-Agent가 미해소 저촉을 넘겨받는가
# ════════════════════════════════════════════════════════════

def test_미해소_저촉을_SubAgent가_이상으로_감지한다():
    """★ 사용자 요청의 핵심 — 저촉이 남으면 구제 재생성까지 열려야 한다."""
    from app.core.sub_agent import SEVERITY_CRITICAL, detect_anomalies

    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": _SPAN, "conflict": "조문은 합계액 전체를 대상으로 한다"}])
    v = _verify(a, trap_ids=[])

    found = detect_anomalies([], _ANSWER, v.supervision)
    codes = [x.code for x in found]
    assert "LAW_CONFLICT" in codes, codes
    assert any(x.severity == SEVERITY_CRITICAL
               for x in found if x.code == "LAW_CONFLICT")


def test_저촉이_없으면_SubAgent를_깨우지_않는다():
    """★ 정상 답변마다 진단 호출이 붙으면 예산이 샌다."""
    from app.core.sub_agent import detect_anomalies

    v = _verify(_Auditor(conflicts=[]), trap_ids=[])
    found = detect_anomalies([], _ANSWER, v.supervision)
    assert "LAW_CONFLICT" not in [x.code for x in found]


def test_구제_재생성_지시에_조문_저촉이_최우선으로_실린다():
    from app.core.sub_agent import build_rewrite_payload

    a = _Auditor(conflicts=[{
        "law_ref": "시험소득세법 제14조 제3항", "quote": _QUOTE,
        "answer_span": _SPAN, "conflict": "조문은 합계액 전체를 대상으로 한다"}])
    v = _verify(a, trap_ids=[])

    payload = build_rewrite_payload(_QUESTION, _ANSWER, v.supervision)
    assert "[법령 저촉" in payload
    # 다른 지적 목록보다 앞에 와야 목록 한가운데 파묻히지 않는다
    if "[감사가 지적한 것" in payload:
        assert payload.index("[법령 저촉") < payload.index("[감사가 지적한 것")
    assert _QUOTE in payload, "조문 원문이 재생성 지시에 실리지 않았다"
