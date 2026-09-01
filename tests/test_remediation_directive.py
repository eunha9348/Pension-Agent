"""재생성 지시가 '합격 조건'을 알려주는가 (2026-09-02).

━━ 실측 결함 ━━
REVISE가 떠도 재생성이 계속 기각됐다(L6_재생성_기각 → SubAgent_구제_기각
→ 축퇴). 원인은 지시가 약해서였다:

  · `unaddressed_traps`는 `verify_any` 표현이 답변에 **그대로** 있는지로
    해소 여부를 판정한다.
  · 그런데 시정 지시에는 `correction` 문장만 실렸다. 모델이 취지는
    반영하면서 다른 말로 바꿔 쓰면 판정은 여전히 '미해소'다.
  · 즉 **합격 조건을 알려주지 않은 채 다시 쓰라고 시킨 것**이다.

━━ 무엇을 바꿨고 무엇을 바꾸지 않았는가 ━━
판정 로직(`unaddressed_traps`)은 손대지 않았다 — 기준을 **밝혔을 뿐**
완화하지 않았다. 재생성 답변도 여전히 `verify_grounding`(수치 검증 +
의미 감사)을 전부 다시 통과해야 채택된다.
"""

from __future__ import annotations

from app.core.supervisory_board import (Finding, SupervisionResult, Verdict,
                                        _required_terms, audit_fitness,
                                        build_remediation_prompt)

_CHECKS = [
    {"id": "C2", "severity": "critical",
     "title": "1,500만원 계산에서 제외되는 소득",
     "correction": "이연퇴직소득은 1,500만원 계산에서 제외됩니다.",
     "docs": ["doc39"], "verify_any": ["이연퇴직소득", "퇴직소득 과세기준"]},
]


# ── 합격 조건이 지시에 실리는가 ───────────────────────────────

def test_해소_판정어가_시정_지시에_실린다():
    """★ 이번 결함의 핵심 — 무엇을 써야 통과하는지 알려줘야 한다."""
    findings = audit_fitness("연 1,500만원 이하로 조절하세요.",
                             trap_checks=_CHECKS)
    trap = [f for f in findings if f.code == "TRAP_UNADDRESSED"]
    assert trap, "미해소 함정이 지적되지 않았다"
    directive = trap[0].directive
    assert "이연퇴직소득" in directive
    assert "그대로 쓸 것" in directive


def test_교정_문장도_함께_남는다():
    """판정어만 주면 '단어만 끼워 넣기'가 된다 — 취지도 함께 줘야 한다."""
    findings = audit_fitness("연 1,500만원 이하로 조절하세요.",
                             trap_checks=_CHECKS)
    directive = [f for f in findings if f.code == "TRAP_UNADDRESSED"][0].directive
    assert "1,500만원 계산에서 제외" in directive


def test_판정어가_없는_규칙에는_덧붙이지_않는다():
    """verify_any가 없으면 붙일 근거가 없다 — 빈 괄호를 만들면 안 된다."""
    checks = [{"id": "X1", "severity": "critical", "title": "제목",
               "correction": "교정 문구", "docs": [], "verify_any": []}]
    findings = audit_fitness("아무 말", trap_checks=checks)
    directive = [f for f in findings if f.code == "TRAP_UNADDRESSED"][0].directive
    assert "그대로 쓸 것" not in directive
    assert "교정 문구" in directive


def test_required_terms는_판정_기준을_그대로_돌려준다():
    """★ 두 곳이 다른 기준을 쓰면 이번 결함이 그대로 재발한다."""
    assert _required_terms(_CHECKS[0]) == ["이연퇴직소득", "퇴직소득 과세기준"]
    assert _required_terms({"verify_any": None}) == []
    assert _required_terms({}) == []


def test_판정어를_그대로_쓴_답변은_애초에_지적되지_않는다():
    """★ 회귀 방지 — 판정 로직 자체는 바뀌지 않았다."""
    findings = audit_fitness(
        "이연퇴직소득은 1,500만원 계산에서 제외됩니다.", trap_checks=_CHECKS)
    assert not [f for f in findings if f.code == "TRAP_UNADDRESSED"]


# ── 지시문 자체의 강제력 ──────────────────────────────────────

def _revise(*directives: str) -> SupervisionResult:
    findings = [Finding("적합성", "TRAP_UNADDRESSED", Verdict.REVISE, d, d)
                for d in directives]
    return SupervisionResult(verdict=Verdict.REVISE, findings=findings,
                             directives=list(directives))


def test_지시문이_원본_유지를_금지한다():
    """★ '표현만 다듬는 수정'이 재생성 기각의 주된 원인이었다."""
    prompt = build_remediation_prompt(_revise("[C2] 이연퇴직소득은 제외됩니다."),
                                      "1,500만원 이하로 조절하세요.")
    assert "그대로 두지 말고" in prompt
    assert "표현만 다듬는 수정은 반려됩니다" in prompt


def test_지시문이_수치_날조를_계속_금지한다():
    """★ 강제력을 높이면서 이 금지를 잃으면 안 된다."""
    prompt = build_remediation_prompt(_revise("[C2] 교정"), "원본")
    assert "새로운 수치를 만들지" in prompt


def test_지시문에_마크다운을_쓰지_않는다():
    """★ HCX가 지시문의 마크다운을 그대로 따라 쓴 이력이 있다(2026-09-01)."""
    prompt = build_remediation_prompt(_revise("[C2] 교정"), "원본")
    assert "**" not in prompt


def test_REVISE가_아니면_지시문을_만들지_않는다():
    """회귀 방지 — 강등·승인에는 재생성 지시가 없어야 한다."""
    res = SupervisionResult(verdict=Verdict.DOWNGRADE, directives=["d"],
                            findings=[
        Finding("적합성", "X", Verdict.DOWNGRADE, "d", "d")])
    assert build_remediation_prompt(res, "원본") == ""
