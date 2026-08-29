"""L4-sub · 상담 답변 계층 테스트.

이 계층이 지켜야 하는 것은 세 가지다.
  ① 예전 같으면 거절되던 개인 서술형 질의가 답변을 받는다
  ② 답변 형식과 검증 경로는 L5'와 동일하다 (검증 면제 없음)
  ③ 근거가 없으면 지어내지 않고, 무엇이 필요한지 정확히 말한다
"""

from __future__ import annotations

import pytest

from app.core.coverage_pipeline import EvidenceChunk
from app.generation.advisory import (ADVISORY_SYSTEM_PROMPT,
                                     build_advisory_payload,
                                     make_generate_advisory,
                                     render_advisory_fallback)

_SPEC = {
    "query": "24살이고 현금 3500만원이 있는데 연금계획을 어떻게 세워야할까?",
    "user_conditions": {"age": 24},
    "extra_conditions": {"보유현금_만원": "3500", "부동산": "없음"},
}
_EV = [EvidenceChunk(doc_id="doc1", text="연금저축계좌 세액공제 한도는 600만원이다.",
                     score=1.0)]


# ── 페이로드 ─────────────────────────────────────────────────

def test_사용자가_밝힌_그밖의_사정이_페이로드에_실린다():
    """정규 스키마에 자리가 없어 예전에는 통째로 버려지던 정보다."""
    p = build_advisory_payload(_SPEC, _EV)
    assert "보유현금_만원" in p and "3500" in p
    assert "부동산" in p and "없음" in p


def test_정규_조건도_함께_실린다():
    assert "24" in build_advisory_payload(_SPEC, _EV)


def test_근거가_있으면_원문이_실린다():
    assert "600만원" in build_advisory_payload(_SPEC, _EV)


def test_근거가_없으면_지어내지_말라고_지시한다():
    """근거 0건일 때 일반론을 늘어놓으면 그게 곧 날조다."""
    p = build_advisory_payload(_SPEC, [])
    assert "확보된 근거가 없습니다" in p
    assert "일반적인 제도 설명도 하지 마십시오" in p


def test_경로_사유가_전달된다():
    p = build_advisory_payload(_SPEC, _EV, route_reason="개인 사정 서술 + 상담 요청")
    assert "개인 사정 서술" in p


# ── 프롬프트 규율 ────────────────────────────────────────────

def test_프롬프트가_수치_날조를_금지한다():
    assert "숫자를 새로 만들지 마십시오" in ADVISORY_SYSTEM_PROMPT
    assert "근거에 없는" in ADVISORY_SYSTEM_PROMPT


def test_프롬프트가_단정적_추천을_금지한다():
    for banned in ("가장 유리합니다", "추천드립니다", "무조건"):
        assert banned in ADVISORY_SYSTEM_PROMPT


def test_프롬프트가_되돌려보내지_말라고_지시한다():
    """이 계층의 존재 이유다 — 거절하지 않고 답한다."""
    assert "되돌려보내지 마십시오" in ADVISORY_SYSTEM_PROMPT


def test_프롬프트가_한계와_필요정보를_요구한다():
    assert "무엇이 확인되지 않아" in ADVISORY_SYSTEM_PROMPT
    assert "어떤 정보를 주시면" in ADVISORY_SYSTEM_PROMPT


def test_프롬프트가_확인항목을_2건으로_제한한다():
    """확인 항목 최대 2건은 CLAUDE.md 원칙이다."""
    assert "최대 2건" in ADVISORY_SYSTEM_PROMPT


def test_프롬프트가_대괄호_구획을_금지한다():
    """L5'와 달리 사람처럼 이어지는 문장이어야 한다."""
    assert "대괄호로 구획을 나누지 마십시오" in ADVISORY_SYSTEM_PROMPT


# ── 생성기 ───────────────────────────────────────────────────

def test_시그니처가_L5프라임과_같다():
    """파이프라인이 두 경로를 같은 자리에서 갈아 끼울 수 있어야 한다.

    이게 어긋나면 이후 검증·인용·감독이 경로마다 두 벌이 되고,
    두 벌이 되면 반드시 어긋난다.
    """
    import inspect

    from app.generation.answer_prompt import make_generate_answer

    a = inspect.signature(make_generate_advisory(client=_FakeLLM("x")))
    b = inspect.signature(make_generate_answer(client=_FakeLLM("x")))
    assert list(a.parameters) == list(b.parameters) == \
        ["query_spec", "evidence", "slots"]


class _FakeLLM:
    is_mock = False

    def __init__(self, out="", raise_exc=None):
        self.out, self.raise_exc, self.calls = out, raise_exc, []

    def call(self, system, user, **kw):
        self.calls.append(kw.get("purpose"))
        if self.raise_exc:
            raise self.raise_exc
        return self.out


def test_생성기가_HCX_응답을_그대로_돌려준다():
    llm = _FakeLLM("말씀하신 상황이라면 연금저축부터 보시는 게 순서입니다.")
    out = make_generate_advisory(client=llm)(_SPEC, _EV, [])
    assert "연금저축부터" in out
    assert llm.calls == ["l4sub_advisory"]


def test_호출_실패는_조용히_넘어가지_않는다():
    logged = []
    gen = make_generate_advisory(client=_FakeLLM(raise_exc=RuntimeError("타임아웃")),
                                 trace_log=lambda k, d, **kw: logged.append((k, d)))
    out = gen(_SPEC, _EV, [])
    assert out                                   # 답변은 나온다
    assert any("실패" in k for k, _ in logged)     # 사유는 남는다


def test_빈_응답도_기록되고_축퇴한다():
    logged = []
    gen = make_generate_advisory(client=_FakeLLM(""),
                                 trace_log=lambda k, d, **kw: logged.append((k, d)))
    out = gen(_SPEC, _EV, [])
    assert out
    assert any("축퇴" in k for k, _ in logged)


# ── 결정론적 축퇴 ────────────────────────────────────────────

def test_축퇴답변이_확인항목을_알려준다():
    """지어낼 것이 없을 때도 '무엇을 말해야 하는지'는 알려줄 수 있다."""
    out = render_advisory_fallback(_SPEC, _EV)
    assert "연금계좌 유형" in out
    assert "알려주시면" in out


def test_축퇴답변이_사용자가_말한_것을_반영한다():
    out = render_advisory_fallback(_SPEC, _EV)
    assert "24" in out


def test_근거없을때_축퇴답변은_근거없음을_밝힌다():
    out = render_advisory_fallback(_SPEC, [])
    assert "근거를 찾지 못했" in out


def test_조건이_전혀_없어도_답변을_만든다():
    out = render_advisory_fallback({"query": "연금 계획 좀"}, [])
    assert len(out) > 40


# ── 파이프라인 통합 ──────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "나는 24살이고 부동산은 없고 현금 3500만원이 있는데 연금계획을 어떻게 세워야할까?",
    "나 몇살인데 연금 계획 좀",
])
def test_개인서술형_질의가_거절되지_않고_답변을_받는다(q):
    """이번 개편의 목적 그 자체 — 예전에는 조기 거절되던 질의다."""
    from app.pipeline import answer_question

    r = answer_question("ADV", q)
    assert set(r) == {"question_id", "question", "retrieved_context",
                      "think_trace", "answer"}
    assert len(r["answer"]) > 40
    assert "답변드릴 수 없습니다" not in r["answer"]
    assert "경로 ADVISORY" in r["think_trace"]


def test_계산질의는_여전히_일반경로로_간다():
    from app.pipeline import answer_question

    r = answer_question("GEN", "계좌에 1억원 있고 연금수령 1년차인데 "
                               "얼마까지 인출할 수 있나요?")
    assert "경로 GENERAL" in r["think_trace"]
    assert "1,200만원" in r["answer"]
