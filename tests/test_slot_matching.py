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


def test_일반_폴백_슬롯은_도메인_겹침_없이도_매칭된다():
    """UI-021 실사용 재현 (2026-09-06).

    어떤 TopicRule에도 걸리지 않은 질의는 query_spec.py의
    _FALLBACK_SLOT("ilban", "질의 주제에 대한 제공 자료 근거")으로
    넘어간다. 이 description 자체가 도메인 핵심어를 하나도 담고 있지
    않아서(질의/주제/제공/자료/근거 — 전부 DOMAIN_TERMS 밖), 일반
    _overlap_ok 판정으로는 **검색이 완벽하게 관련 문서를 찾아와도
    이 슬롯 하나 때문에 항상 실패**했다. 실사용에서 "IRP 계좌를 중도
    해지했을때 불이익이 있을까"가 관련 문서 8건을 정확히 찾고도
    전부 매칭 실패로 떨어져, HCX가 근거 없이 수치를 지어냈고
    (수치검증 실패로 결국 잡히긴 했지만) 사용자에게는 "근거 없음"
    템플릿만 나갔다.
    """
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="doc47",
        text="개인형 퇴직연금제도(IRP)에서 중도인출·해지 시 세법상 부득이한 "
             "사유에 해당하지 않으면 불이익이 있습니다.",
        score=0.8)
    assert match(_slot(sid="ilban", desc="질의 주제에 대한 제공 자료 근거"),
                chunk) is True


def test_일반_폴백이_아닌_슬롯은_여전히_도메인_겹침을_요구한다():
    """반대 방향 회귀 — ilban 예외가 다른 슬롯까지 느슨하게 만들면 안 된다."""
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="doc99",
        text="펀드의 환매는 청구일로부터 제3영업일에 공고되는 기준가격을 적용한다.",
        score=0.8)
    assert match(_slot(sid="s1", desc="연금저축 세액공제 한도"), chunk) is False


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


# ════════════════════════════════════════════════════════════════
# F57 · "원천징수(세율)"만 겹쳐도 연금 밖 문단이 근거로 통과하던 결함
#        (2026-09-07 실측, Q10)
# ════════════════════════════════════════════════════════════════
#
# "80세인데 연금소득세율이 얼마인가요"에 3.3%/4.4%(실제 세법과 맞는
# 정확한 값)를 답했지만, 함께 인용된 근거 문서는 그 값과 무관한
# 내용이었다 — 일반 펀드의 이자·배당소득 원천징수세율(15.4%), 장기주택
# 마련저축 비과세 조항 등. 감독심사는 "수치 대조 검증 통과"라고 표시했지만
# 그 수치는 계산함수(하드코딩된 세율표)에서 나온 것이지 인용된 문서에서
# 나온 것이 아니었다 — 근거 표시가 실제 근거 유무를 보증하지 못했다.
#
# 원인: "연령별 연금소득 원천징수세율" 슬롯이 "원천징수"/"원천징수세율"
# 하나만 겹쳐도 통과했다. 이 단어는 동의어 확장으로 4개 토큰처럼
# 보이지만 실제로는 한 단어이고, 연금 밖(일반 펀드 원천징수 등)에서도
# 흔히 쓰인다.

def test_원천징수만_겹치고_연금맥락이_없으면_거절한다():
    match = make_slot_evidence_matcher({})
    slot = _slot(sid="wonchen_fact", desc="연령별 연금소득 원천징수세율")
    irrelevant = EvidenceChunk(
        doc_id="R2_dummy",
        text=("본 펀드의 이자소득 및 배당소득에 대한 원천징수세율은 15.4%"
              "이며, 비거주자에게는 별도 협약세율이 적용된다."),
        score=0.8)
    assert match(slot, irrelevant) is False


def test_원천징수와_연금맥락어가_함께_있으면_통과한다():
    """★ 오탐 경계 — 실제로 연금 관련 문단이면 그대로 통과해야 한다."""
    match = make_slot_evidence_matcher({})
    slot = _slot(sid="wonchen_fact", desc="연령별 연금소득 원천징수세율")
    relevant = EvidenceChunk(
        doc_id="doc39",
        text=("【연금소득 원천징수세율】 연금계좌에서 연금수령하는 사적연금"
              "소득에 대해서는 다음 세율로 원천징수한다. 만 80세 이상 "
              "3.3%, 종신형 연금 4.4%"),
        score=0.8)
    assert match(slot, relevant) is True


def test_다른_애매한_도메인어_없이는_기존_동작이_그대로다():
    """★ 회귀 방지 — 애매한 도메인어(원천징수 계열)가 아닌 정상 매칭은
    이 가드의 영향을 받지 않는다."""
    match = make_slot_evidence_matcher({})
    chunk = EvidenceChunk(
        doc_id="doc39",
        text="연금저축계좌의 세액공제 대상 납입한도는 연 600만원이며 IRP와 합산 시 900만원이다.",
        score=0.8)
    assert match(_slot(), chunk) is True
