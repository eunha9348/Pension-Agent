"""본문 근거 인용 — 매칭이 거부한 근거가 인용문으로 새면 안 된다.

━━ 실측 결함 (2026-08-29, 사용자 직접 지적) ━━
"연간 연금수령액 2,000만원" 질의 답변에 이런 줄이 실렸다:

    · 연간 2천만원 수령 시 부과되는 세금 범위: …설정된 집합투자재산의 매매 및
      평가이익을 포함한 개인의 연간 금융소득(이자, 배당소득)이 2천만원을
      초과하는 경우에는 유형별 소득을 합산하여 개인소득세율로 종합과세 됩니다.

이건 **연금소득이 아니라 금융소득종합과세(이자·배당)** 조항이다. 연금과
아무 관계가 없는데, '연간'·'천만원'이라는 일반어만 겹쳐서 끌려왔다.

━━ 원인: 같은 판단을 두 곳이 다른 기준으로 하고 있었다 ━━
  · slot_matching._overlap_ok       — 도메인 핵심어가 겹쳐야 통과 (엄격)
  · answer_prompt._evidence_snippet — 아무 핵심어나 **부분문자열**로 1개
                                      있으면 통과 (느슨)

느슨한 쪽이 답변 본문에 직접 들어가므로, 매칭이 거부한 근거가 인용문으로
새어 나갔다. 게다가 상위 1건만 보고 판단해서, 한 문서 안에서 엉뚱한 청크가
앞서면 **맞는 근거까지 통째로 버려졌다.**

━━ 지켜야 할 불변식 ━━
인용 기준은 매칭 기준과 같아야 한다. 근거를 못 찾았다고 밝히는 편이
엉뚱한 원문을 내미는 것보다 낫다.
"""

from __future__ import annotations

from app.analysis.vocab import key_terms
from app.core.coverage_pipeline import EvidenceChunk
from app.generation.answer_prompt import _evidence_snippet

# 실제로 답변에 실렸던 그 문단 (금융소득종합과세 — 연금과 무관)
_OFF_TOPIC = (
    "투자신탁재산에 귀속되는 이자,배당소득은 귀속되는 시점에는 원천징수하지 "
    "아니하고 집합투자기구로부터의 이익이 투자자에게 지급하는 날에 "
    "집합투자기구로부터의 이익으로 원천징수합니다. 설정된 집합투자재산의 매매 및 "
    "평가이익을 포함한 개인의 연간 금융소득(이자, 배당소득)이 2천만원을 초과하는 "
    "경우에는 유형별 소득을 합산하여 개인소득세율로 종합과세 됩니다.")

# 이 질의에 실제로 맞는 근거
_ON_TOPIC = (
    "【1,500만원 초과 시 과세방식 선택】 연간 사적연금 수령액이 1,500만원을 "
    "초과하는 경우, 연금소득 분리과세(16.5%) 또는 종합과세 중 선택할 수 있다. "
    "이때 과세 대상은 1,500만원을 초과한 금액이 아니라 사적연금소득 전액이다.")


def _quote(desc: str, chunks: list[EvidenceChunk]) -> str:
    return _evidence_snippet(chunks, [c.doc_id for c in chunks],
                             keywords=key_terms(desc))


# ── 엉뚱한 근거는 인용하지 않는다 ────────────────────────────

def test_일반어만_겹치는_근거는_인용하지_않는다():
    """★ 실측 사고 재현 — '연간'·'천만원'만 겹친 금융소득 조항이 실렸다."""
    out = _quote("연간 2천만원 수령 시 부과되는 세금 범위",
                 [EvidenceChunk("d1", _OFF_TOPIC, score=0.6)])
    assert out == "", f"연금과 무관한 근거가 인용됐다: {out[:80]!r}"


def test_금융소득_조항이_연금_슬롯에_붙지_않는다():
    """도메인이 다르면 숫자가 겹쳐도 근거가 아니다."""
    out = _quote("연금소득 과세방식 선택 기준",
                 [EvidenceChunk("d1", _OFF_TOPIC, score=0.9)])
    assert "금융소득" not in out
    assert "집합투자" not in out


# ── 맞는 근거는 그대로 인용한다 (과잉 차단 방지) ──────────────

def test_맞는_근거는_그대로_인용된다():
    """★ 이쪽이 깨지면 '엄격하게 고쳤다'가 아니라 '망가뜨렸다'가 된다."""
    out = _quote("연금소득 과세방식 선택 기준",
                 [EvidenceChunk("d2", _ON_TOPIC, score=0.9)])
    assert "사적연금" in out
    assert "1,500만원" in out


def test_한_문서_안에서_맞는_청크를_골라낸다():
    """★ 상위 1건만 보면 안 되는 이유.

    엉뚱한 청크가 앞서 있다고 전체를 포기하면 맞는 근거까지 버린다.
    한 문서는 여러 청크로 쪼개지므로 실제로 흔한 상황이다.
    """
    out = _quote("연금소득 과세방식 선택 기준",
                 [EvidenceChunk("d1", _OFF_TOPIC, score=0.9),   # 앞선다
                  EvidenceChunk("d1", _ON_TOPIC, score=0.5)])
    assert "사적연금" in out, "같은 문서의 맞는 청크를 찾지 못했다"
    assert "금융소득" not in out


# ── 기준이 두 곳에서 어긋나지 않는가 ─────────────────────────

def test_인용_기준이_매칭_기준과_어긋나지_않는다():
    """★ 근본 원인 고정 — 두 계층이 같은 판단을 다르게 하면 또 샌다.

    매칭(slot_evidence_matcher)이 거부한 청크는 인용(_evidence_snippet)
    에서도 거부돼야 한다. 한쪽만 고치면 다음 문서에서 같은 일이 난다.
    """
    from app.analysis.slot_matching import make_slot_evidence_matcher
    from app.core.coverage_pipeline import RequirementSlot

    desc = "연간 2천만원 수령 시 부과되는 세금 범위"
    slot = RequirementSlot("s1", desc, "fact")
    chunk = EvidenceChunk("d1", _OFF_TOPIC, score=0.6)

    matched = make_slot_evidence_matcher({})(slot, chunk)
    quoted = bool(_quote(desc, [chunk]))

    assert matched is False, "매칭이 엉뚱한 근거를 받아들였다"
    assert quoted is False, "매칭은 거부했는데 인용은 통과시켰다 — 기준 불일치"


def test_부분문자열이_아니라_토큰_경계로_센다():
    """slot_matching 모듈 규약과 같아야 한다 ('정해지는'의 '해지' 오탐 차단)."""
    import inspect

    from app.generation import answer_prompt

    src = inspect.getsource(answer_prompt._evidence_snippet)
    assert "key_terms" in src, "토큰 경계 기준(key_terms)을 쓰지 않는다"
    assert "domain_hits" in src, "도메인 핵심어 겹침을 요구하지 않는다"


# ── 배선 — 템플릿 답변까지 실제로 지나가는가 ──────────────────

def test_템플릿_답변에_무관한_근거가_실리지_않는다():
    """★ 부품이 아니라 답변까지 확인한다.

    사용자가 본 것은 _evidence_snippet의 반환값이 아니라 최종 답변이었다.
    호출자가 결과를 안 쓰면 부품만 고쳐도 아무것도 안 바뀐다.
    """
    from app.core.coverage_pipeline import RequirementSlot, SlotStatus
    from app.generation.answer_prompt import render_template_answer

    slot = RequirementSlot("s1", "연간 2천만원 수령 시 부과되는 세금 범위", "fact")
    slot.status = SlotStatus.COVERED
    slot.evidence_ids = ["d1"]

    spec = {"query": "연간 연금수령액 2,000만원 받는데 세금이 어떻게 되나요?",
            "user_conditions": {"private_pension_annual_manwon": 2000}}
    out = render_template_answer(
        spec, [EvidenceChunk("d1", _OFF_TOPIC, score=0.9)], [slot])

    assert "금융소득" not in out, f"무관한 근거가 답변에 실렸다:\n{out}"
    assert "집합투자" not in out
