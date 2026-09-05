"""계산값 표기 회귀 — 계산해 놓고 답변에 안 쓰는 사고를 막는다.

━━ 실제 사고 (2026-08-20 배포 서버, 평가셋 E-04·E-05) ━━
    질의  "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?"
    계산  연금수령한도 = 1,200만원        ← 정확히 계산됨
    답변  "연금수령한도는 계좌평가액과 연금수령연차로 산정됩니다..."
          → 숫자가 없다.

검증기는 전부 통과했다. `verify_numeric_grounding`은 **답변의 수치 →
근거** 한 방향만 보기 때문이다. 답변에 수치가 아예 없으면 대조 대상이
0건이라 그냥 통과한다. 즉 "계산은 함수, 설명은 LLM" 원칙이 절반만
지켜지고 있었다 — 함수는 정확히 계산했는데 그 출력이 사용자에게
도달하지 않았다.

이 파일은 반대 방향(**계산 결과 → 답변**)을 고정한다.
"""

from __future__ import annotations

from app.core.numeric_verifier import verify_calc_presence


# ════════════════════════════════════════════════════════════════
# 기본 판정
# ════════════════════════════════════════════════════════════════

_HANDO = {"limit": 1200.0, "unlimited": False, "denominator": 10,
          "source": "doc39"}


def test_계산값이_답변에_없으면_잡는다():
    """실제 실패 사례 그대로 — 설명만 하고 숫자를 안 쓴 답변."""
    r = verify_calc_presence(
        "연금수령한도는 계좌평가액과 연금수령연차에 따라 산정됩니다.", [_HANDO])
    assert r.passed is False
    assert r.required_count == 1
    assert any(label == "연금수령한도" for label, _v, _s in r.missing)


def test_계산값이_답변에_있으면_통과한다():
    r = verify_calc_presence("연금수령한도는 1,200만원입니다.", [_HANDO])
    assert r.passed is True


def test_쉼표_없는_표기도_같은_값으로_본다():
    """LLM은 '1200만원'으로도 쓴다. 표기 차이로 실패하면 오탐이다."""
    assert verify_calc_presence("한도는 1200만원입니다.", [_HANDO]).passed is True


def test_억_단위_표기를_해석한다():
    """12,000만원 = 1억 2,000만원. 억을 못 읽으면 멀쩡한 답변을 반려한다."""
    calc = [{"limit": 12000.0, "source": "doc39"}]
    assert verify_calc_presence("한도는 1억 2,000만원입니다.", calc).passed is True
    assert verify_calc_presence("한도는 12,000만원입니다.", calc).passed is True


def test_다른_숫자를_썼으면_통과시키지_않는다():
    """숫자가 있기만 하면 되는 게 아니다 — 그 값이어야 한다."""
    assert verify_calc_presence("한도는 1억 2,000만원입니다.", [_HANDO]).passed is False


# ════════════════════════════════════════════════════════════════
# 무엇을 요구하고 무엇을 봐주는가
# ════════════════════════════════════════════════════════════════

def test_분모_같은_중간값은_요구하지_않는다():
    """'적용 분모 10'까지 답변에 쓰라고 하면 멀쩡한 답변이 반려된다.
    금액·비율만 요구한다."""
    r = verify_calc_presence("연금수령한도는 1,200만원입니다.", [_HANDO])
    assert r.passed is True
    assert r.required_count == 1          # limit만 — denominator는 제외


def test_source_같은_내부키는_요구하지_않는다():
    r = verify_calc_presence("한도는 1,200만원입니다.",
                             [{"limit": 1200.0, "source": "doc39",
                               "note": "참고", "unlimited": False}])
    assert r.passed is True


def test_계산이_없으면_검사할_것도_없다():
    r = verify_calc_presence("근거 문서를 찾지 못했습니다.", [])
    assert r.passed is True
    assert r.required_count == 0
    assert "없음" in r.as_trace()


def test_variants_구조의_조건별_결론도_전부_요구한다():
    """'상황별 결론 제공'이 설계 요건이다 — 한쪽만 쓰면 답이 반쪽이다."""
    calc = [{"variants": [
        {"label": "연금수령 시", "result": {"T_withholding": 330.0}},
        {"label": "일시금 수령 시", "result": {"T_withholding": 550.0}},
    ]}]
    assert verify_calc_presence("연금으로 받으면 330만원입니다.", calc).passed is False
    assert verify_calc_presence(
        "연금 수령 시 330만원, 일시금 수령 시 550만원입니다.", calc).passed is True


def test_세액공제율은_소득구간별로_다르게_요구된다():
    """★ 2026-09-05 외부 심사 리포트로 발견 — calc_private_contribution_limit()의
    출력 키는 "세액공제율"인데(파라미터명 r_tax_credit과 다름), render.py의
    단위 분류표에 이 이름이 없어 _presence_targets가 통째로 건너뛰었다.
    그 결과 소득을 몰라 두 구간(13.2%/16.5%)으로 나눠 계산해도, 강제
    표기 대상에는 소득과 무관한 한도 상수(600/900/1800)만 남고 그 상수는
    두 구간에서 항상 같은 값이다 — "계산 결과" 박스가 두 줄 다 "600만원"만
    보여주고 정작 달라야 할 값(세액공제율)은 어느 쪽도 요구하지 않았다.
    """
    calc = [{"variants": [
        {"label": "총급여 5,500만원 이하", "result": {
            "연금저축_단독_한도": 600.0, "세액공제율": 0.165}},
        {"label": "총급여 5,500만원 초과", "result": {
            "연금저축_단독_한도": 600.0, "세액공제율": 0.132}},
    ]}]
    labels = [label for label, _v, _s in
              verify_calc_presence("한도는 600만원입니다.", calc).missing]
    assert any("세액공제율" in lb and "이하" in lb for lb in labels), (
        "16.5% 구간의 세액공제율이 요구 대상에서 빠졌다")
    assert any("세액공제율" in lb and "초과" in lb for lb in labels), (
        "13.2% 구간의 세액공제율이 요구 대상에서 빠졌다")

    # 두 구간을 모두 정확히 쓰면 통과해야 한다
    full_answer = ("총급여 5,500만원 이하면 한도 600만원에 세액공제율 16.5%, "
                   "초과면 한도 600만원에 세액공제율 13.2%입니다.")
    assert verify_calc_presence(full_answer, calc).passed is True


# ════════════════════════════════════════════════════════════════
# 시정 지시 · 트레이스
# ════════════════════════════════════════════════════════════════

def test_시정_지시에_빠진_값이_그대로_들어간다():
    """재생성 프롬프트가 '무엇을 넣어야 하는지' 명시해야 고쳐진다."""
    r = verify_calc_presence("한도는 산정 방식에 따라 달라집니다.", [_HANDO])
    ins = r.instruction()
    assert "1,200만원" in ins
    assert "연금수령한도" in ins
    # 새로 계산하라는 지시로 읽히면 안 된다 (LLM이 숫자를 만들면 안 됨)
    assert "새로 계산하지" in ins


def test_트레이스가_검사_안_함과_통과를_구분한다():
    """예전 사고의 재발 방지 — 0건 검사를 '통과'로 적으면 신뢰가 왜곡된다."""
    none_trace = verify_calc_presence("답변", []).as_trace()
    pass_trace = verify_calc_presence("1,200만원", [_HANDO]).as_trace()
    assert none_trace != pass_trace
    assert "없음" in none_trace
    assert "확인" in pass_trace


# ════════════════════════════════════════════════════════════════
# 파이프라인 연결 — 심각도를 올리기만 한다 (단조성)
# ════════════════════════════════════════════════════════════════

def test_계산값_누락은_검증을_통과시키지_않는다():
    from app.core.coverage_pipeline import RequirementSlot, SlotStatus
    from app.generation.grounding import make_verify_grounding

    slot = RequirementSlot("hando", "연금수령한도", "calculation",
                           calc_function="연금수령한도_계산")
    slot.status = SlotStatus.CALC_DONE
    slot.calc_result = _HANDO

    verify = make_verify_grounding("1억원 1년차 한도?", [slot])
    v = verify("연금수령한도는 계좌평가액으로 산정합니다.", [])
    assert bool(v) is False
    assert v.presence.passed is False
    assert "1,200만원" in v.revise_instruction()
    assert "계산값 표기 누락" in v.as_trace()


def test_LLM용_시정지시가_사용자_답변에_노출되지_않는다():
    """CALC_NOT_SHOWN의 directive는 LLM에게 하는 말이다
    ("반드시 문장 안에 그대로 적으십시오"). 파이프라인이 계산값을
    결정론적으로 덧붙여 해소하므로, 사용자 고지문에 남으면 안 된다 —
    이미 해결된 문제를 사용자에게 떠넘기고 내부 프롬프트까지 새어 나간다."""
    from app.core.supervisory_board import Finding, SupervisionResult, Verdict
    from app.pipeline import _unresolved_notice

    sup = SupervisionResult(Verdict.REVISE, findings=[
        Finding("수치표기", "CALC_NOT_SHOWN", Verdict.REVISE,
                "계산값 표기 누락", "반드시 문장 안에 그대로 적으십시오"),
    ])
    notice, asks = _unresolved_notice(sup)
    assert notice == ""
    assert asks == []


def test_다른_지적은_여전히_고지된다():
    """오탐 회귀 — CALC_NOT_SHOWN만 걸러야지 고지 자체를 죽이면 안 된다."""
    from app.core.supervisory_board import Finding, SupervisionResult, Verdict
    from app.pipeline import _unresolved_notice

    sup = SupervisionResult(Verdict.REVISE, findings=[
        Finding("적합성", "TRAP_UNADDRESSED", Verdict.REVISE,
                "함정 미반영", "연금실제수령연차를 구분해 설명할 것"),
    ])
    notice, asks = _unresolved_notice(sup)
    assert "연금실제수령연차" in notice
    assert asks


def test_계산값이_실린_답변은_이_검사로_막히지_않는다():
    """오탐 회귀 — 이 검사가 멀쩡한 답변을 반려하면 순손실이다."""
    from app.core.coverage_pipeline import RequirementSlot, SlotStatus
    from app.generation.grounding import make_verify_grounding

    slot = RequirementSlot("hando", "연금수령한도", "calculation",
                           calc_function="연금수령한도_계산")
    slot.status = SlotStatus.CALC_DONE
    slot.calc_result = _HANDO

    verify = make_verify_grounding("1억원 1년차 한도?", [slot])
    v = verify("연금수령한도는 1,200만원입니다.", [])
    assert v.presence.passed is True
    assert v.revise_instruction() == ""
