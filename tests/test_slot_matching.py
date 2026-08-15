"""슬롯-근거 매칭 / 답변-슬롯 커버리지 테스트."""

from __future__ import annotations

from app.analysis.slot_matching import (answer_covers_slot,
                                        make_slot_evidence_matcher)
from app.core.coverage_pipeline import (EvidenceChunk, RequirementSlot,
                                        SlotStatus)


def _slot(sid="s1", desc="연금저축 세액공제 한도", stype="fact") -> RequirementSlot:
    return RequirementSlot(slot_id=sid, description=desc, slot_type=stype)


def test_주제가_같은_근거는_매칭된다():
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="doc39",
        text="연금저축계좌의 세액공제 대상 납입한도는 연 600만원이며 IRP와 합산 시 900만원이다.",
        score=0.8)
    assert match(_slot(), chunk) is True


def test_주제가_다른_근거는_걸러진다():
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="doc99",
        text="펀드의 환매는 청구일로부터 제3영업일에 공고되는 기준가격을 적용한다.",
        score=0.8)
    assert match(_slot(), chunk) is False


def test_엔티티가_충돌하면_매칭하지_않는다():
    """의미는 비슷해도 대상 상품이 다르면 근거가 아니다."""
    spec = {"entities": {"product_name": "솔로몬국공채단기"}}
    match = make_slot_evidence_matcher(spec)
    chunk = EvidenceChunk(
        doc_id="docA", text="본 상품의 총보수는 연 0.5440%입니다.",
        entities={"product_name": "솔로몬국공채장기"}, score=0.9)
    assert match(_slot(desc="총보수 수준"), chunk) is False


def test_비교질의는_엔티티가_달라도_허용된다():
    spec = {"intent": "상품_비교", "entities": {"product_name": "솔로몬국공채단기"}}
    match = make_slot_evidence_matcher(spec)
    chunk = EvidenceChunk(
        doc_id="docA", text="본 상품의 총보수는 연 0.5440%입니다.",
        entities={"product_name": "솔로몬국공채장기"}, score=0.9)
    assert match(_slot(desc="총보수 비교"), chunk) is True


def test_부분문자열_오탐이_발생하지_않는다():
    """'정해지는'의 '해지'가 걸리는 종류의 오탐 회귀 방지.

    토큰 경계 기준으로 비교하므로 '정해지는'은 '중도해지' 슬롯에 걸리지 않는다."""
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="docB",
        text="기준가격은 매 영업일마다 새로 정해지는 방식으로 산출됩니다.",
        score=0.9)
    assert match(_slot(desc="중도해지 시 세금"), chunk) is False


# ── 답변 커버리지 ────────────────────────────────────────────

def test_계산슬롯은_수치가_답변에_있어야_반영으로_본다():
    s = _slot(sid="limit", desc="연금수령한도", stype="calculation")
    s.status = SlotStatus.CALC_DONE
    s.calc_result = {"limit": 1200.0, "denominator": 10, "source": "doc39"}

    assert answer_covers_slot("연금수령한도는 1,200만원입니다.", s) is True
    # 숫자가 빠지면 '계산은 했는데 설명에 안 넣은' 상태 → 미반영
    assert answer_covers_slot("연금수령한도는 계산식에 따라 산출됩니다.", s) is False


def test_사실슬롯은_핵심어로_판정한다():
    s = _slot(desc="연금저축 세액공제 한도")
    assert answer_covers_slot("연금저축의 세액공제 한도는 연 600만원입니다.", s) is True
    assert answer_covers_slot("환매 절차는 다음과 같습니다.", s) is False


def test_빈_답변은_어떤_슬롯도_반영하지_않는다():
    assert answer_covers_slot("", _slot()) is False


def test_비율표기_변형을_흡수한다():
    s = _slot(sid="rate", desc="원천징수세율", stype="calculation")
    s.status = SlotStatus.CALC_DONE
    s.calc_result = {"r_withholding": 0.055}
    assert answer_covers_slot("연금소득세 5.5%가 원천징수됩니다.", s) is True
