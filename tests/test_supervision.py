"""감독 계층 테스트 — 권한 계층(단조성)이 핵심.

━━ 왜 단조성이 전부인가 ━━
L6에서 HyperCLOVA X는 의미 감사를 맡되 **심각도를 올릴 수만 있다.**
LLM이 APPROVE라고 해도 결정론적 REVISE는 REVISE로 남아야 한다.
이 성질이 깨지면 L6 전체가 무의미해진다 — LLM이 자기 답변을 통과시키는
구조가 되기 때문이다. 리팩터링 시 이 테스트가 방어선이다.
"""

from __future__ import annotations

import pytest

from app.core.supervisory_board import (Finding, SupervisionResult, Verdict,
                                        audit_plan, build_remediation_prompt,
                                        merge_supervision, parse_llm_audit,
                                        supervise, supervise_plan)

DET_VERDICTS = [Verdict.APPROVE, Verdict.REVISE, Verdict.DOWNGRADE, Verdict.BLOCK]
_ORDER = {Verdict.APPROVE: 0, Verdict.REVISE: 1, Verdict.DOWNGRADE: 2, Verdict.BLOCK: 3}


# ════════════════════════════════════════════════════════════════
# 권한 계층 (단조성)
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("det", DET_VERDICTS)
@pytest.mark.parametrize("llm", DET_VERDICTS)
def test_LLM은_심각도를_올릴_수만_있다(det, llm):
    """어떤 조합에서도 최종 판정은 결정론적 판정보다 약해지지 않는다."""
    merged = merge_supervision(SupervisionResult(det), llm, [])
    assert _ORDER[merged.verdict] >= _ORDER[det]
    assert _ORDER[merged.verdict] == max(_ORDER[det], _ORDER[llm])


def test_결정론_REVISE는_LLM_APPROVE로_완화되지_않는다():
    merged = merge_supervision(SupervisionResult(Verdict.REVISE), Verdict.APPROVE, [])
    assert merged.verdict == Verdict.REVISE


def test_결정론_BLOCK은_무엇으로도_풀리지_않는다():
    for llm in DET_VERDICTS:
        assert merge_supervision(SupervisionResult(Verdict.BLOCK), llm, []).verdict \
            == Verdict.BLOCK


def test_LLM이_더_엄격하면_상향된다():
    merged = merge_supervision(SupervisionResult(Verdict.APPROVE), Verdict.BLOCK,
                               [Finding("의미감사", "X", Verdict.BLOCK, "문제", "고칠 것")])
    assert merged.verdict == Verdict.BLOCK
    assert merged.findings


def test_확인항목은_최대_2건으로_제한된다():
    merged = merge_supervision(SupervisionResult(Verdict.APPROVE), Verdict.APPROVE, [],
                               llm_questions=["a", "b", "c", "d"])
    assert len(merged.revised_ask_back) <= 2


# ════════════════════════════════════════════════════════════════
# LLM 감사 응답 파싱
# ════════════════════════════════════════════════════════════════

def test_감사응답_파싱():
    raw = '{"verdict":"REVISE","findings":[{"code":"의미 정합성",' \
          '"detail":"연차 혼동","directive":"구분해 서술"}],' \
          '"most_critical_questions":["연금실제수령연차"]}'
    verdict, findings, questions = parse_llm_audit(raw)
    assert verdict == Verdict.REVISE
    assert findings[0].detail == "연차 혼동"
    assert questions == ["연금실제수령연차"]


def test_코드펜스가_붙어도_파싱된다():
    verdict, _, _ = parse_llm_audit('```json\n{"verdict":"BLOCK","findings":[]}\n```')
    assert verdict == Verdict.BLOCK


def test_파싱_실패는_문제없음과_구분된다():
    """감사자가 응답을 못 준 것과 '문제없음'은 다르다."""
    verdict, findings, _ = parse_llm_audit("응답을 드릴 수 없습니다")
    assert verdict == Verdict.APPROVE          # 심각도를 임의로 올리지도 않는다
    assert findings and findings[0].code == "PARSE_FAIL"


# ════════════════════════════════════════════════════════════════
# 결정론적 4대 감사
# ════════════════════════════════════════════════════════════════

def test_단정적_추천은_지적된다():
    result = supervise("C-Pe 클래스가 가장 유리합니다. 이걸 추천드립니다.",
                       citations=[object()])
    assert result.verdict != Verdict.APPROVE
    assert any(f.auditor == "준법" for f in result.findings)


def test_계산결과를_제시하는데_근거가_없으면_지적된다():
    """근거 문서 없이 수치를 내놓으면 등급을 강등한다."""
    result = supervise("세액공제는 148.5만원입니다.",
                       calc_results=[{"A_tax_credit": 148.5}], citations=[])
    assert result.verdict != Verdict.APPROVE
    assert any(f.code == "NO_BASIS" for f in result.findings)


def test_시정지시문이_생성된다():
    result = supervise("이 상품이 가장 유리합니다.", citations=[object()])
    if result.verdict == Verdict.REVISE:
        prompt = build_remediation_prompt(result, "이 상품이 가장 유리합니다.")
        assert "수정" in prompt
        assert "새로운 수치를 만들지 말고" in prompt


# ════════════════════════════════════════════════════════════════
# 계획 감사 (1.5 계층)
# ════════════════════════════════════════════════════════════════

def test_미등록_함수는_계획에서_제거된다():
    spec = {"asked_for": [{"id": "a", "description": "계산", "type": "calculation",
                           "calc_function": "존재하지_않는_함수"}],
            "planned_calls": [{"function": "존재하지_않는_함수", "args": {}}],
            "plan": ["계산"]}
    findings, safe = audit_plan(spec, {"연금수령한도_계산"})
    assert findings
    assert not [c for c in safe.get("planned_calls", [])
                if c["function"] == "존재하지_않는_함수"]


def test_정상_계획은_승인된다():
    spec = {"asked_for": [{"id": "a", "description": "연금수령한도",
                           "type": "calculation",
                           "calc_function": "연금수령한도_계산"}],
            "planned_calls": [{"function": "연금수령한도_계산", "args": {}}],
            "plan": ["한도 계산"]}
    result, safe = supervise_plan(spec, {"연금수령한도_계산"})
    assert result.verdict == Verdict.APPROVE
    assert safe["planned_calls"]
