"""상품명 접지 — 근거에 없는 상품을 지어내지 않는가 (2026-09-01).

━━ 실측 결함 ━━
"은퇴 5년 남았는데 안전한 상품 추천해줘"에 대해, **근거 문서가 0건인데도**
답변이 실존 펀드명을 콕 집어 추천했다. 과제 자료의 "근거 문서 고정" 원칙
위반이고, 채점의 'Hallucination 방지' 항목에 직접 걸린다.

━━ 왜 기존 감사가 못 잡았나 ━━
상품 감사(`audit_fitness`)는 `_products_in_answer()`가 넘긴
**검색 후보와 답변의 교집합**만 본다. 근거가 0건이면 후보가 비고, 후보가
비면 교집합도 비어 **지어낸 이름은 검사 대상에 오르지도 않는다.**
즉 "가입 불가 상품을 추천했는가"는 보면서 "존재 근거가 없는 상품을
지어냈는가"는 보지 않았다 — 설계상 사각지대였다.

━━ 오탐 설계 ━━
결정론 계층이라 오탐은 되돌릴 수 없다(단조성). 그래서 세 겹으로 좁혔다:
  ① 공백 없는 8자 이상 토큰이 상품 접미사로 끝날 때만 (일반 서술 제외)
  ② 상품 '종류'를 뜻하는 일반명사는 명시적 제외
  ③ 대조는 공백 무시 + 앞 8자만 — 코퍼스 OCR이 깨져 있어도 브랜드가
     실재하면 지어낸 것이 아니다
"""

from __future__ import annotations

from app.core.citation_system import (extract_product_names,
                                      verify_product_grounding)

# 실제로 답변에 나갔던 이름 (2026-09-01)
_FAKE = "삼성클래식연금증권전환형자투자신탁"
_ANSWER = (f"은퇴가 5년 남으셨다면 {_FAKE} 같은 국공채형 상품을 "
           f"고려해 보실 수 있습니다.")


# ── 탐지 ─────────────────────────────────────────────────────

def test_상품명을_뽑아낸다():
    assert extract_product_names(_ANSWER) == [_FAKE]


def test_일반_서술은_상품명으로_보지_않는다():
    """★ 오탐 방지 — 공백이 있거나 짧은 일반 표현."""
    for text in ("이 투자신탁은 위험등급이 높습니다.",
                 "증권 투자신탁의 총보수는 상품마다 다릅니다.",
                 "집합투자업자가 운용하는 펀드입니다.",
                 "연금저축펀드와 IRP는 다릅니다."):
        assert extract_product_names(text) == [], text


def test_상품_종류를_뜻하는_일반명사는_제외한다():
    """길이 조건만으로는 카테고리명이 걸린다."""
    for text in ("타깃데이트형펀드를 고려하실 수 있습니다.",
                 "원리금보장형펀드와 실적배당형펀드는 다릅니다.",
                 "채권혼합형펀드는 주식 비중이 낮습니다."):
        assert extract_product_names(text) == [], text


# ── 접지 판정 ────────────────────────────────────────────────

def test_근거가_0건이면_지어낸_상품으로_판정한다():
    """★ 실측 사고 재현."""
    r = verify_product_grounding(_ANSWER, [])
    assert r["passed"] is False
    assert r["ungrounded"] == [_FAKE]


def test_근거에_있으면_통과한다():
    r = verify_product_grounding(_ANSWER, [f"{_FAKE} 제1호는 국공채에 투자한다."])
    assert r["passed"] is True


def test_근거의_띄어쓰기가_달라도_통과한다():
    """★ 코퍼스 OCR은 같은 이름도 띄어쓰기가 제각각이다."""
    r = verify_product_grounding(_ANSWER, ["삼성 클래식 연금 증권전환형자투자신탁"])
    assert r["passed"] is True


def test_근거_뒷부분이_깨져_있어도_브랜드가_맞으면_통과한다():
    """★ 겹쳐 그려짐·판독 실패로 이름 뒷부분이 깨진 문서가 실재한다.

    오탐(정상 답변을 버림)이 미탐보다 나쁘므로 앞 8자만 본다.
    """
    r = verify_product_grounding(_ANSWER, ["삼성클래식연금증권전환형자투자(판독불가)"])
    assert r["passed"] is True


def test_상품명이_없으면_검사하지_않는다():
    r = verify_product_grounding("연금저축 세액공제 한도는 600만원입니다.", [])
    assert r["passed"] is True
    assert r["checked"] == 0


# ── 배선 ─────────────────────────────────────────────────────

def test_파이프라인이_수치검증과_같은_자리에서_처리한다():
    """★ 근거 없는 상품명은 근거 없는 수치와 같은 종류의 사고다.

    같은 자리에서 같은 방식(결정론적 답변으로 축퇴)으로 처리해야 한다.
    배선이 빠지면 탐지기만 살아 있고 답변은 그대로 나간다 —
    실제로 인용 무결성 검사가 그 상태였다.
    """
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    assert "verify_product_grounding(" in src, "탐지기가 배선되지 않았다"
    numeric_pos = src.index("verdict.numeric is not None")
    product_pos = src.index("verify_product_grounding(")
    assert product_pos > numeric_pos, "수치 검증 뒤에 와야 한다"
    # 축퇴로 이어지는가
    after = src[product_pos:product_pos + 900]
    assert "render_template_answer" in after, \
        "상품명 접지 실패가 축퇴로 이어지지 않는다"


def test_인용무결성은_더_이상_수치를_보지_않는다():
    """★ 수치 검증과 중복이면서 더 느슨해 오탐 20/298건이던 규칙을 걷어냈다.

    되살리면 "만 55세면 수령 가능한가요"에 "55세"라고 답한 정상 답변이
    '근거 없는 수치'로 잡힌다.
    """
    from app.core.citation_system import verify_citation_integrity

    r = verify_citation_integrity("만 55세부터 수령하실 수 있습니다.", [])
    assert r["ok"] is True, r["issues"]
