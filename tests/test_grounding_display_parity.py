"""표시하는 근거와 검증하는 근거가 어긋나던 결함 (2026-09-04 실서버 확인).

━━ 실측 결함 (스크린샷 3) ━━
질의  "저는 24살에 현금 3000만원 가지고 있고 주택청약 500만원 있는데
       연금 계획을 어떻게 세워야 하지..?"
답변  "연금저축은 연간 최대 400만 원까지 세액공제가 가능합니다.
       장기주택마련저축은 분기당 300만 원 이내, 총 계약 기간 7년 이상…"
화면  "근거 문서: 해당 없음 (제공 자료에서 확인된 근거가 없습니다)"
      "감독 검증을 통과했습니다."

400만원은 현행 기준(연금저축 단독 600만원)이 아니라 오래된 구법 수치이고,
장기주택마련저축은 폐지된 상품이다. 즉 **근거 0건이라고 표시하면서
지어낸 수치를 통과시켰다.**

━━ 원인 ━━
`verify_grounding(draft, evidence)`는 **검색된 evidence 전체**로 수치 허용
집합을 만드는데, 사용자에게 보이는 `retrieved_context`는 **인용된 것
(used_evidence)** 만 담는다. 두 집합이 어긋나 있었다.

실물 코퍼스는 투자설명서라 숫자 밀도가 높다 — "300만원", "7년", "400"이
검색된 청크 어딘가에 우연히 존재하기만 하면 통과한다. mock 코퍼스(7문서)
에서는 우연 일치가 없어 이 결함이 재현되지 않는다. **실물에서만 보이는
계열**(마크다운 노출·판독 실패 문자와 같은 부류)이다.

━━ 수정 ━━
인용이 하나도 없으면(= 화면에 "근거 문서 없음"이 뜨면) 수치 허용 집합에서
근거 문서를 제외한다. 그 상태에서 허용되는 것은 계산 결과와 질의가 준
값뿐이다 — 뒷받침할 근거를 제시하지 못했으면서 수치를 단정할 수는 없다.
"""

from __future__ import annotations

from app.analysis.query_spec import sanitize_spec
from app.core.coverage_pipeline import EvidenceChunk
from app.generation.grounding import make_verify_grounding

# 실물 투자설명서처럼 숫자가 빽빽한 청크 — 지어낸 값이 우연히 다 들어 있다
_DENSE = EvidenceChunk(doc_id="R2_x", score=0.9, text=(
    "납입한도 연 1,800만원. 전환금액 300만원 한도. 가입기간 5년 이상 7년 미만. "
    "세액공제 400만원 구간은 2013년 이전 기준. 15.4% 원천징수."))

_HALLUCINATED = ("연간 최대 400만 원까지 세액공제가 가능하며, "
                "분기당 300만 원 이내, 7년 이상이어야 합니다.")

_Q = "연금 계획을 어떻게 세워야 하지?"


def _verdict(citations):
    vg = make_verify_grounding(question=_Q, slots=[], llm_call=None,
                               citations=citations)
    return vg(_HALLUCINATED, [_DENSE])


# ── 핵심 불변식 ──────────────────────────────────────────────

def test_인용이_없으면_지어낸_수치를_잡는다():
    """★ 실측 스크린샷3 그대로 — 예전에는 이게 통과했다."""
    v = _verdict(citations=[])
    assert not v.numeric.passed
    assert set(v.numeric.ungrounded) == {400.0, 300.0, 7.0}


def test_인용이_있으면_기존_동작_그대로다():
    """★ 회귀 방지 — 정상 답변까지 조이면 안 된다.

    근거를 제시한 답변은 예전과 똑같이 근거 문서 전체를 허용 집합으로 쓴다.
    """
    v = _verdict(citations=["근거1"])
    assert v.numeric.passed


def test_인용이_없어도_질의가_준_수치는_허용된다():
    """사용자가 말한 숫자를 되짚는 것은 날조가 아니다."""
    vg = make_verify_grounding(
        question="현금 3000만원 있고 주택청약 500만원 있는데 어떻게 하죠?",
        slots=[], llm_call=None, citations=[])
    v = vg("말씀하신 3,000만원과 500만원을 기준으로 보면", [_DENSE])
    assert v.numeric.passed


def test_인용이_없어도_계산_결과는_허용된다():
    """계산은 함수가 한 것이므로 근거 문서와 무관하게 정당하다."""
    class _Slot:
        status = None
        calc_result = {"limit": 1200.0}

    from app.core.coverage_pipeline import SlotStatus
    _Slot.status = SlotStatus.CALC_DONE

    vg = make_verify_grounding(question="한도가 얼마인가요?", slots=[_Slot()],
                               llm_call=None, citations=[])
    v = vg("연금수령한도는 1,200만원입니다.", [_DENSE])
    assert v.numeric.passed


# ── 내부 감사 문구가 사용자 화면에 새지 않는가 ────────────────
#
# 스크린샷1에서 인용 라벨이 이렇게 찍혔다:
#   [1] R2_KR5120420039 (투자설명서) 제40조의2
#       — 원천징수세율 (등록된 계산함수 없음), 연령별 연금소득 원천징수세율
# slot description은 retrieved_context의 supports 목록과 템플릿 답변에
# 그대로 노출된다. 채점자에게는 시스템 오류로 보인다.

def test_미등록_계산함수_강등이_설명문에_새지_않는다():
    """★ 강등은 내부 사정 — 사용자에게 보이는 문구에 적으면 안 된다."""
    spec = sanitize_spec({
        "intent": "원천징수",
        "asked_for": [{"id": "s1", "description": "원천징수세율",
                       "type": "calculation",
                       "calc_function": "존재하지_않는_함수", "required": True}],
    }, "만 80세인데 세금 몇 퍼센트인가요?")
    slot = spec["asked_for"][0]
    assert slot["description"] == "원천징수세율", (
        f"내부 감사 문구가 설명문에 남았다: {slot['description']!r}")
    assert "등록된 계산함수" not in slot["description"]


def test_강등_사실_자체는_내부_키로_남는다():
    """추적은 되어야 한다 — 조용히 사라지면 디버깅이 불가능하다."""
    spec = sanitize_spec({
        "intent": "원천징수",
        "asked_for": [{"id": "s1", "description": "원천징수세율",
                       "type": "calculation",
                       "calc_function": "존재하지_않는_함수", "required": True}],
    }, "질의")
    slot = spec["asked_for"][0]
    assert slot.get("calc_unavailable") is True
    assert slot["type"] == "fact", "계산 슬롯이 사실 슬롯으로 강등돼야 한다"


def test_등록된_함수는_강등되지_않는다():
    """대조군 — 정상 함수까지 강등하면 계산이 통째로 죽는다."""
    spec = sanitize_spec({
        "intent": "원천징수",
        "asked_for": [{"id": "s1", "description": "원천징수세율",
                       "type": "calculation",
                       "calc_function": "사적연금_원천징수_계산", "required": True}],
    }, "질의")
    slot = spec["asked_for"][0]
    assert slot["type"] == "calculation"
    assert slot.get("calc_function") == "사적연금_원천징수_계산"
    assert "calc_unavailable" not in slot


def test_억단위_분리표기가_근거없는_수치로_잡히지_않는다():
    """UI 실사용 재현 (2026-09-06) — 1억을 넘는 계산 결과의 표시형.

    format_manwon은 10,000만원 이상을 "1억 417만원"처럼 억과 만원으로
    쪼개 쓴다. 그런데 검증 대조 집합에는 반올림 보정값(10,417)만 있어서,
    답변에 실제로 찍히는 417이 '근거 없는 수치'로 잡혔다. 그 결과
    **계산이 정확히 성공했는데도** 답변이 통째로 템플릿으로 축퇴했다.

    "표시 반올림과 수치 검증의 기준을 어긋나게 두지 말 것"(CLAUDE.md)과
    정확히 같은 함정의 억 단위 판이다. 오차 허용을 늘려 해결하면 진짜
    날조까지 통과하므로, 표시 함수가 만든 표기를 허용 집합에 넣어 푼다.
    """
    from app.analysis.units import format_manwon
    from app.core.numeric_verifier import _flatten_numbers

    result = {"퇴직급여_적립액": 10416.6667}
    allowed = _flatten_numbers(result)

    assert format_manwon(10416.6667) == "1억 417만원"
    assert 417.0 in allowed, "표시형의 만원 부분이 허용 집합에 없다"
    assert 10416.6667 in allowed, "원본 값이 허용 집합에서 사라졌다"


def test_억단위_표기_허용이_무관한_수치까지_통과시키지_않는다():
    """반대 방향 회귀 — 표시형을 넣는다고 아무 수나 통과하면 안 된다."""
    from app.core.numeric_verifier import _flatten_numbers

    allowed = _flatten_numbers({"퇴직급여_적립액": 10416.6667})
    assert 9999.0 not in allowed
    assert 500.0 not in allowed
