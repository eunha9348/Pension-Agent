"""ADVISORY 경로에 함정 교정이 주입되지 않던 문제 (2026-09-03).

━━ 실측 결함 ━━
`generation/advisory.py`에 함정(trap) 관련 코드가 전무했다. ADVISORY로
분류된 질의는 L4-sub가 함정 교정 지침 없이 초안을 썼다. L6 감사는 L5'와
공유되므로 결국 TRAP_UNADDRESSED로 잡히긴 하지만, 그 뒤 재생성이
`SUPERVISOR_SYSTEM_PROMPT`(L5' 전용, 계산 중심 어조)로 돌아갔다.

"두 생성기는 시그니처가 같고 이후 검증·인용·감독을 한 벌로 공유한다"는
설계 의도가 **재생성 단계에서만** 깨져 있었다 — 상담형 답변이 감사에
걸리는 순간 계산형 답변으로 성격이 바뀌었다.

━━ 수정 ━━
① `build_advisory_payload`에 L5'의 `build_supervisor_payload`와 같은
   "[주의할 혼동]" 블록을 주입한다(판정 기준 verify_any 포함).
② `render_advisory_fallback`(LLM 미사용 축퇴)에도 L5'의
   `render_template_answer`와 같은 방식으로 "주의할 점" 문구를 넣는다.
③ L6 REVISE 재생성 시, `route.is_advisory`를 보고 ADVISORY_SYSTEM_PROMPT
   또는 SUPERVISOR_SYSTEM_PROMPT 중 경로에 맞는 것을 쓴다.
"""

from __future__ import annotations

from app.core.coverage_pipeline import EvidenceChunk
from app.generation.advisory import (ADVISORY_SYSTEM_PROMPT,
                                     build_advisory_payload,
                                     render_advisory_fallback)

_TRAP_CONTEXT = {
    "correction_notes": ["IRP와 연금저축은 인출 규칙이 완전히 다릅니다."],
    "checks": [{
        "id": "A2", "severity": "critical",
        "title": "IRP와 연금저축의 중도인출 사유가 다름",
        "correction": "IRP와 연금저축은 인출 규칙이 완전히 다릅니다. "
                      "연금저축은 사유 제한이 없지만, IRP는 법정 사유에 "
                      "해당해야 합니다.",
        "verify_any": ["법정 사유", "사유 제한", "사유와 무관"],
    }],
}

_SPEC = {"query": "연금 계좌를 중간에 해지하고 싶은데 어떻게 해야 하나요?"}
_EV = [EvidenceChunk(doc_id="doc20", text="IRP는 법정 사유에만 인출 가능합니다.",
                     score=0.9)]


# ── build_advisory_payload — 함정 블록 주입 ───────────────────

def test_함정_교정이_advisory_페이로드에_실린다():
    """★ 이번 결함의 핵심 — 예전에는 이 블록 자체가 없었다."""
    p = build_advisory_payload(_SPEC, _EV, trap_context=_TRAP_CONTEXT)
    assert "주의할 혼동" in p
    assert "IRP와 연금저축은 인출 규칙이 완전히 다릅니다" in p


def test_판정_기준_verify_any도_함께_실린다():
    """★ L5'와 같은 형식 — 취지만 주고 판정어를 안 주면 다른 말로 바꿔 써서 계속 REVISE가 뜬다."""
    p = build_advisory_payload(_SPEC, _EV, trap_context=_TRAP_CONTEXT)
    assert "법정 사유" in p and "반드시 답변에 등장해야 함" in p


def test_trap_context가_없으면_블록도_없다():
    """회귀 방지 — 있지도 않은 걸 있다고 광고하면 안 된다."""
    p = build_advisory_payload(_SPEC, _EV, trap_context=None)
    assert "주의할 혼동" not in p


def test_checks가_없는_예전_호출_경로도_correction_notes만으로_동작한다():
    """하위 호환 — checks 없이 correction_notes만 있던 예전 trap_context 형태."""
    legacy_context = {"correction_notes": ["IRP는 사유 제한이 있습니다."]}
    p = build_advisory_payload(_SPEC, _EV, trap_context=legacy_context)
    assert "IRP는 사유 제한이 있습니다" in p


# ── render_advisory_fallback — 축퇴 경로도 방어선을 갖는다 ────

def test_축퇴_안내에도_주의할_점이_실린다():
    out = render_advisory_fallback(_SPEC, _EV, trap_context=_TRAP_CONTEXT)
    assert "주의할 점" in out
    assert "IRP와 연금저축은 인출 규칙이 완전히 다릅니다" in out


def test_trap_context_없이도_축퇴_안내는_정상_작동한다():
    """회귀 방지 — trap_context가 선택 인자라 기존 호출부가 깨지면 안 된다."""
    out = render_advisory_fallback(_SPEC, _EV)
    assert out  # 예외 없이 문자열을 낸다
    assert "주의할 점" not in out


# ── end-to-end — 재생성이 경로에 맞는 프롬프트를 쓰는가 ───────

class _AdvisoryReviseClient:
    """ADVISORY 경로에서 REVISE를 내고, 재생성 호출에 쓰인 시스템
    프롬프트와 초안 페이로드를 기록하는 대역."""

    is_mock = False

    def __init__(self):
        self.advisory_payload: str = ""
        self.regen_system_prompt: str = ""

    def call(self, system, user, purpose="?", **kw):
        if purpose == "l4sub_advisory":
            self.advisory_payload = user
            return "중도해지는 계좌 유형에 따라 다릅니다."
        if "감사자" in system:
            return ('{"verdict":"REVISE","findings":[{"code":"TRAP_UNADDRESSED",'
                    '"detail":"A2 미해소","directive":"IRP는 법정 사유에만 '
                    '인출 가능하다는 점을 반영할 것"}]}')
        if purpose == "l5_regenerate":
            self.regen_system_prompt = system
            return ("연금저축은 사유 제한 없이 인출 가능하고 IRP는 법정 "
                    "사유에 해당해야 합니다.")
        return "분석 실패"

    def call_with_functions(self, s, u, t, purpose="?", **kw):
        return {"name": None, "arguments": None, "raw": ""}


_Q = "연금 계좌를 중간에 해지하고 싶은데 어떻게 해야 하나요? 노후 대비 상담 좀 해주세요"


def test_ADVISORY_경로는_실제로_ADVISORY로_분류된다():
    """전제 확인 — 이 질문이 정말 ADVISORY로 가는지 먼저 확인한다."""
    from app.analysis.routing import classify_route

    route = classify_route(_Q, {}, [])
    assert route.route == "ADVISORY"


def test_ADVISORY_초안에_함정_블록이_실제로_전달된다():
    from app.pipeline import answer_question

    c = _AdvisoryReviseClient()
    answer_question("Q", _Q, client=c)
    assert "주의할 혼동" in c.advisory_payload


def test_ADVISORY_재생성은_ADVISORY_프롬프트를_쓴다():
    """★ 이번 결함의 핵심 — 예전에는 여기서 SUPERVISOR_SYSTEM_PROMPT(L5' 전용)를 썼다."""
    from app.pipeline import answer_question

    c = _AdvisoryReviseClient()
    answer_question("Q", _Q, client=c)
    assert c.regen_system_prompt, "재생성이 호출되지 않았다"
    assert c.regen_system_prompt == ADVISORY_SYSTEM_PROMPT


def test_재생성_단계_이름도_경로에_맞게_찍힌다():
    from app.pipeline import answer_question

    c = _AdvisoryReviseClient()
    r = answer_question("Q", _Q, client=c)
    assert "L4-sub로 1회 되돌림" in r["think_trace"]
    assert "L5'로 1회 되돌림" not in r["think_trace"]
