"""함정 근거 문서가 인용까지 살아남는 보장이 없던 문제 (2026-09-03, E-19).

━━ 실측 결함 ━━
"연금저축은 아무 때나 중도인출 되는데 IRP도 똑같나요?"(함정 A2, 근거 doc20)
가 `must_cite=doc20` 미충족으로 실패했다.

경로를 따라가 보면: L3 `retrieval_steer`가 doc20을 예약(pinned, score
0.9)해 재순위 경쟁에서 보호한다. 그런데 그 **뒤** `_exploit`의
`filter_irrelevant_evidence`가 엔티티 충돌이나 점수 임계값으로 조용히
떨어뜨릴 수 있었다 — pinned 여부를 이 단계는 전혀 몰랐다. 떨어지면
`_addressed_trap_docs`가 인용 후보로 넣어도 `_used_evidence`는 evidence를
순회하므로 결국 인용되지 않는다.

같은 계열 실패가 실측에서 3회 반복됐다(L-01/doc55 · E-19/doc20 · E-26/doc40)
— 우연이 아니라 구조적 누수였다.

━━ 수정 ━━
`_exploit`에서 `query_spec["_steered_docs"]`(L3가 이미 기록해 두던 값 —
지금까지 트레이스 표시에만 쓰였다)를 읽어, 그 문서의 청크는 엔티티
충돌·점수 임계값 필터를 면제한다. **구법 배제(1단계)는 면제하지 않는다**
— 순서상 이미 그 전에 적용됐고, 다시 살려주지 않는다.
"""

from __future__ import annotations

from app.core.coverage_pipeline import EvidenceChunk, TraceLogger
from app.pipeline import _exploit


def _chunk(doc_id: str, text: str, score: float = 0.9,
          entities: dict | None = None) -> EvidenceChunk:
    return EvidenceChunk(doc_id=doc_id, text=text, score=score,
                         entities=entities or {})


# ── 실측 재현 — E-19 ────────────────────────────────────────

def test_함정_근거는_엔티티_충돌로_탈락하지_않는다():
    """★ 실측 E-19 그대로 재현 — doc20이 엔티티 충돌 필터를 통과해야 한다."""
    trap_doc = _chunk("doc20", "IRP는 근로자퇴직급여보장법 적용을 받아 "
                              "법정 사유에만 중도인출이 가능합니다.",
                      entities={"product_name": "IRP표준상품"})
    query_spec = {
        "intent": "중도인출",
        "entities": {"product_name": "다른상품"},   # 충돌을 일부러 만든다
        "_steered_docs": ["doc20"],
    }
    kept, _warn = _exploit([trap_doc], query_spec, {}, TraceLogger())
    assert any(c.doc_id == "doc20" for c in kept), (
        "함정이 지목한 근거가 엔티티 충돌로 탈락했다 — E-19가 재발한다")


def test_함정_근거는_점수_임계값_미달로도_탈락하지_않는다():
    low_score_trap_doc = _chunk("doc20", "IRP 중도인출은 법정 사유가 필요합니다.",
                                score=0.05)   # 기본 임계값 0.35 미달
    query_spec = {"intent": "중도인출", "entities": {}, "_steered_docs": ["doc20"]}
    kept, _warn = _exploit([low_score_trap_doc], query_spec, {}, TraceLogger())
    assert any(c.doc_id == "doc20" for c in kept)


def test_보호_사실이_think_trace에_남는다():
    """조용히 살려주면 나중에 왜 통과했는지 추적할 수 없다."""
    trap_doc = _chunk("doc20", "본문", entities={"product_name": "IRP표준상품"})
    query_spec = {"intent": "중도인출", "entities": {"product_name": "다른상품"},
                 "_steered_docs": ["doc20"]}
    trace = TraceLogger()
    _exploit([trap_doc], query_spec, {}, trace)
    steps = [s.step for s in trace._steps]
    assert "L4_함정근거_보호" in steps


def test_보호가_불필요하면_트레이스도_안_남는다():
    """이미 필터를 통과할 근거를 보호했다고 광고하면 안 된다."""
    trap_doc = _chunk("doc20", "본문", entities={})   # 충돌 없음, 통과함
    query_spec = {"intent": "중도인출", "entities": {}, "_steered_docs": ["doc20"]}
    trace = TraceLogger()
    _exploit([trap_doc], query_spec, {}, trace)
    steps = [s.step for s in trace._steps]
    assert "L4_함정근거_보호" not in steps


# ── 무엇을 완화하지 않는가 — 구법 배제는 그대로 ──────────────

def test_함정_근거라도_구법_문서면_세제_질의에서_제외된다():
    """★ '구법 제외는 유지' — 함정 보호가 구법 필터를 무력화하면 안 된다.

    C5(구법 수치 혼재) 함정 자체가 '구법 문서가 섞여 있다'는 사실을
    지목하는 규칙이다. 만약 함정 보호가 구법 배제까지 면제하면, 세제
    질의에서 개정 전 수치가 답변 근거로 그대로 살아난다.
    """
    legacy_doc = _chunk(
        "R2_KR514X450008",
        "세액공제 한도는 700만원이며 종합소득금액 4천만원 이하인 경우 적용됩니다.")
    query_spec = {"intent": "세액공제", "entities": {},
                 "_steered_docs": ["R2_KR514X450008"]}
    kept, warnings = _exploit([legacy_doc], query_spec, {}, TraceLogger())
    assert not any(c.doc_id == "R2_KR514X450008" for c in kept), (
        "함정 보호가 구법 배제를 무력화했다 — C5가 재발한다")
    assert warnings, "구법 문서 제외 경고가 남아야 한다"


# ── 대조군 — 함정과 무관한 근거는 기존 필터가 그대로 적용된다 ──

def test_함정과_무관한_근거는_기존_필터가_그대로_적용된다():
    """회귀 방지 — 보호 대상이 아닌 근거까지 통과시키면 안 된다.

    ⚠️ '필터 후 근거 0건이면 임계값을 낮춰 되살린다'는 기존 폴백과
    섞이지 않도록, 정상 통과하는 근거를 하나 함께 둔다 — 그래야 이
    테스트가 보호 로직만 격리해서 본다.
    """
    normal_doc = _chunk("doc01", "정상 통과하는 근거", score=0.9)
    unrelated_doc = _chunk("doc99", "무관한 내용", score=0.05)
    query_spec = {"intent": "중도인출", "entities": {}, "_steered_docs": ["doc20"]}
    kept, _warn = _exploit([normal_doc, unrelated_doc], query_spec, {}, TraceLogger())
    assert any(c.doc_id == "doc01" for c in kept)
    assert not any(c.doc_id == "doc99" for c in kept), (
        "보호 대상이 아닌 저점수 근거까지 통과시켰다")


def test_steered_docs가_없으면_기존_동작_그대로다():
    """대조군 — retrieval_steer가 없는 평범한 질의는 영향받지 않는다."""
    normal_doc = _chunk("doc01", "정상 통과하는 근거", score=0.9)
    low_score_doc = _chunk("doc99", "본문", score=0.05)
    query_spec = {"intent": "일반", "entities": {}}
    kept, _warn = _exploit([normal_doc, low_score_doc], query_spec, {}, TraceLogger())
    assert not any(c.doc_id == "doc99" for c in kept)
