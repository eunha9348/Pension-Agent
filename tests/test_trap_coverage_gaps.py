"""외부 평가 10문항에서 드러난 함정 커버리지 갭 (2026-09-01).

규칙은 있는데 **트리거가 좁아서 정작 필요한 질의에서 발화하지 않던** 것들이다.
규칙이 없는 것보다 나쁘다 — 있다고 믿고 넘어가기 때문이다.

각 항목은 298건 실측으로 오탐을 확인한 뒤 확장했다. 아래 테스트는
발화해야 할 질의와 **걸리면 안 되는 질의**를 함께 고정한다.
"""

from __future__ import annotations

import pytest

from app.core.trap_rules import detect_traps, verify_terms_for


def _ids(q: str) -> list[str]:
    return [r.id for r in detect_traps(q)]


# ── C2 · 1,500만원 계산에서 제외되는 소득 ────────────────────

def test_C2는_퇴직금이라는_일상어에도_발화한다():
    """★ 이번 세트에서 가장 심각했던 결함.

    "IRP에 퇴직금 3억이 있는데 연 1500만원 넘게 받으면 종합과세되나요?"
    에서 C2가 불발하고 C1(초과 시 전액 과세)만 걸렸다. 그 결과 답변이
    **틀린 전제를 바로잡기는커녕 강화해** "1,500만원 이하로 조정하라"고
    조언했다. 퇴직급여를 재원으로 하는 연금소득은 금액과 무관하게
    1,500만원 계산에서 빠지므로, 이 고객에게는 실질적 손해다.

    원인은 맥락어에 '이연퇴직소득'만 있고 '퇴직금'·'IRP'가 없던 것이다 —
    사용자는 법령 용어를 쓰지 않는다.
    """
    q = "IRP에 퇴직금 3억이 있는데 연금으로 연 1500만원 넘게 받으면 종합과세되나요?"
    assert "C2" in _ids(q)


def test_C2는_1500만원_맥락이_없으면_발화하지_않는다():
    for q in ("퇴직금을 IRP로 옮기면 어떤 점이 좋나요?",
              "IRP 세액공제 한도가 얼마인가요?"):
        assert "C2" not in _ids(q), q


# ── A7 · IRP 부분인출 불가 ───────────────────────────────────

@pytest.mark.parametrize("q", [
    "DC형과 IRP는 운용 주체랑 인출 시점이 어떻게 다른가요?",
    "IRP를 중도해지하면 세금이 어떻게 되나요?",
    "IRP는 아무 때나 자유롭게 중도인출할 수 있죠?",
])
def test_A7은_인출_질의에서_발화한다(q):
    """★ 내적 비일관성의 원인.

    같은 시스템이 어떤 질의에서는 doc55를 근거로 "IRP는 부분인출 불가"라고
    답하고, 다른 질의에서는 "횟수 제한 없이 중도인출 가능"이라고 답했다.
    후자에서 A7이 불발했기 때문이다.
    """
    assert "A7" in _ids(q)


@pytest.mark.parametrize("q", [
    "IRP 수수료는 어떻게 정해지는 건가요?",
    "IRP 한도가 결정해지면 알려주세요",
])
def test_A7의_해지_주제어는_부분문자열_오탐이_없다(q):
    """★ '해지'를 맨몸으로 넣으면 '정해지는'·'결정해지'가 전부 걸린다."""
    assert "A7" not in _ids(q)


# ── E8 · 퇴직급여 연금수령 시 이연퇴직소득세 감면 ────────────

def test_E8은_퇴직금_절세_질의에서_발화한다():
    """★ 퇴직금 IRP 절세의 **핵심 메커니즘**이 답변에서 통째로 빠졌다.

    대신 'IRP 900만원 세액공제'가 절세 포인트로 제시됐는데, 세액공제는
    개인이 추가 납입하는 돈에만 적용되므로 회사에서 이체된 퇴직금과는
    무관하다 — 질문의 핵심을 비껴간 답이다.
    """
    q = ("희망퇴직한 은행원인데 퇴직금을 IRP에 넣으면 세금을 거의 안 "
         "낸다는데 절세법 알려주세요")
    assert "E8" in _ids(q)


def test_E8은_연금수령_기간_질의에서_발화한다():
    """감면율이 갈리는 지점이라 반드시 다뤄야 한다."""
    for q in ("퇴직금 2억원을 IRP로 이체하고 10년간 연금으로 받으면 "
              "세금이 얼마나 절약되나요?",
              "산출 퇴직소득세가 2000만원인 퇴직금을 15년에 걸쳐 연금으로 "
              "받으면 세금은 얼마인가요?"):
        assert "E8" in _ids(q), q


def test_E8은_퇴직_맥락이_없으면_발화하지_않는다():
    for q in ("연금저축 세액공제 절세 방법 알려주세요",
              "연금저축을 연금으로 받으면 세율이 어떻게 되나요?"):
        assert "E8" not in _ids(q), q


def test_E8의_교정문은_세액공제와의_구분을_담는다():
    """실측 오답이 '세액공제'를 절세 포인트로 든 것이었다."""
    from app.core.trap_rules import TRAPS

    e8 = next(t for t in TRAPS if t.id == "E8")
    assert "이연퇴직소득" in e8.correction
    assert "세액공제" in e8.correction
    assert verify_terms_for("E8") == ["이연퇴직소득"]


# ── D2 · 위험등급 비교 주의 ──────────────────────────────────

@pytest.mark.parametrize("q", [
    "TDF2030이랑 TDF2045 중에 뭐가 나을까요? 저는 1990년생입니다.",
    "TDF 2045와 TDF 2050 중 어느 것이 더 나은가요?",
])
def test_D2는_TDF_비교_질의에서_발화한다(q):
    """★ 근거 0건인데 "TDF2030은 주식비중이 낮다"고 단정했다.

    D2가 불발해 되묻기 후보가 0건이 되면 답변이 그냥 단정한다.
    """
    assert "D2" in _ids(q)


@pytest.mark.parametrize("q", [
    "저는 55세이고 자산이 5억인데 연금을 지금 받는 게 나을까요 미루는 게 나을까요?",
    "연금저축을 해지하고 그 돈으로 부동산에 투자하는 게 나을까요?",
])
def test_D2는_상품_비교가_아니면_발화하지_않는다(q):
    """'뭐가 나을까' 같은 일반 비교 표현을 넣었을 때 걸리던 것들.

    확인 항목은 최대 2건이라 무관한 되묻기가 자리를 차지하면 안 된다.
    """
    assert "D2" not in _ids(q)


# ── A4 · 요양 인출 ───────────────────────────────────────────

def test_A4의_교정문이_방향과_한도를_모두_담는다():
    """★ 두 결함을 한 번에 고쳤다.

    ① 방향 — '기준이 다르다'만 말하면 뉘앙스를 반대로 전달할 수 있다.
       실측 답변은 "요양비도 부득이한 사유가 아니면 기타소득세 16.5%"라고
       썼는데, 요양(3개월 이상)은 부득이한 사유의 **대표 사례**다.
    ② 한도 — fact에는 있었지만 correction에 없었다. 답변 생성 프롬프트의
       [주의할 혼동] 블록은 correction만 싣기 때문에, 정작 사용자가 물은
       "얼마나 인출할 수 있나요"에 쓸 재료가 도달하지 않았다.
    """
    from app.core.trap_rules import TRAPS

    a4 = next(t for t in TRAPS if t.id == "A4")
    assert "부득이한 사유에 해당" in a4.correction
    assert "200만원" in a4.correction and "150만원" in a4.correction
    # 검증용어는 그대로 유지돼야 한다
    assert all(t in a4.correction for t in verify_terms_for("A4"))


# ── 의미 감사 프롬프트 ───────────────────────────────────────

def test_감사_프롬프트가_세율_귀속과_전제검증을_본다():
    """★ 결정론적으로 잡을 수 없어 의미 감사로 보낸 두 가지.

    · "종합과세되면 5.5~3.3%" — 코퍼스 원문에 "연금소득세 5.5~3.3%
      (…종합과세 가능)"라는 **정상** 문장이 있어 문자열 규칙은 오탐이 난다.
    · "질문의 전제가 틀렸는데 그대로 수용" — 의미 판단이다.
    """
    from app.core.supervisory_board import LLM_AUDIT_SYSTEM_PROMPT as P

    assert "종합과세" in P and "누진세율" in P
    assert "질문 전제의 검증" in P
    assert "출생연도" in P, "질문이 준 조건을 쓰지 않는 것도 봐야 한다"
    assert "위 7개 항목" in P, "항목을 늘렸으면 응답 스키마 설명도 맞춰야 한다"


def test_감사_프롬프트가_위험등급_방향_오귀속도_본다():
    """★ 2026-09-05 실물 확인 — "2등급을 중간 정도의 위험"이라고 서술한
    답변이 실제로 나갔다. 표준 6단계(1등급 최고위험·6등급 최저위험)에서
    2등급은 최고위험 바로 다음 구간인데 "중간"으로 방향을 잘못 짚었다.

    등급 번호 자체는 근거(product_facts)에서 맞게 가져왔으므로 문자열
    대조로는 못 잡는다 — "숫자는 맞는데 그 숫자가 무엇인지 잘못 말한다"는
    수치–서술 정합 항목과 같은 결함 유형이라 세율 오귀속과 같은 자리에
    예시를 추가했다.
    """
    from app.core.supervisory_board import LLM_AUDIT_SYSTEM_PROMPT as P

    assert "위험등급 방향" in P
    assert "1등급이 가장 높은 위험" in P and "6등급" in P


def test_조건부_값의_서술_순서_지침이_세_프롬프트_모두에_있다():
    """★ M2 · 2026-09-05 외부 콘솔 심사 리포트로 발견.

    "600만원 이내 13.2%가 적용됩니다. 다만 소득이 낮으면 16.5%도
    가능합니다"처럼, 소득 구간에 따라 **둘 중 하나만** 적용되는 세율을
    하나는 확정으로, 다른 하나는 예외처럼 서술하면 두 세율이 동시에
    적용되는 것 같은 잘못된 인상을 준다. 수치 자체는 둘 다 맞았으므로
    verify_numeric_grounding으로는 못 잡는다 — 문자열로 결정할 수 없는
    "서술 순서가 오해를 부르는가"는 규칙이 아니라 프롬프트 지침 +
    의미 감사로 다룬다(위험등급 방향 오귀속과 같은 처리 방식).

    L5'(SUPERVISOR)·L4-sub(ADVISORY) 두 경로 모두 조건부 값을 만들 수
    있으므로 한쪽에만 지침을 넣으면 F3(함정 교정이 ADVISORY에만 빠진
    것)과 같은 비대칭이 생긴다. 감사 프롬프트에도 안전망으로 넣는다 —
    프롬프트 지시만으로는 HCX가 관성적으로 어길 수 있기 때문이다
    (마크다운 지시를 무시하고 굵게를 쓴 사례와 같은 계열).
    """
    from app.core.supervisory_board import LLM_AUDIT_SYSTEM_PROMPT
    from app.generation.advisory import ADVISORY_SYSTEM_PROMPT
    from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT

    for name, prompt in (("SUPERVISOR", SUPERVISOR_SYSTEM_PROMPT),
                         ("ADVISORY", ADVISORY_SYSTEM_PROMPT)):
        assert "조건을 먼저" in prompt, f"{name}에 서술 순서 지침이 없다"
        assert "13.2%" in prompt and "16.5%" in prompt, (
            f"{name}에 구체적 반례가 없다")

    assert "조건부 값의 서술 순서" in LLM_AUDIT_SYSTEM_PROMPT, (
        "의미 감사 안전망이 없다 — 프롬프트 지시만으로는 HCX가 어길 수 있다")
