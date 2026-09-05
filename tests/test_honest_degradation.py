"""C단계 · 정직한 축퇴 회귀.

━━ 이 파일이 지키는 단 하나의 규칙 ━━
**시스템이 자기 답변에 문제가 있다는 걸 알았다면, 사용자도 알아야 한다.**

Q-001에서 L6는 초안과 재생성을 모두 반려했다. 즉 시스템은 자기 답변이
부적절하다는 사실을 두 번 확인했다. 그런데 최종 출력에는 그 흔적이 전혀
없었고, 확신에 찬 어조로 관련 없는 제도를 설명했다. 트레이스에는
"보수적으로 원본 유지"라고 적혀 있었지만, 반려된 답변을 그대로 내보내는
것은 보수적인 처리가 아니다.

평가에서 가중치가 가장 높은 지표가 '정보한계 대응'이다. 여기가 무너지면
다른 걸 아무리 잘해도 만회가 안 된다.
"""

from __future__ import annotations

import pytest

from app.core.coverage_pipeline import Answerability
from app.core.supervisory_board import (Finding, SupervisionResult, Verdict,
                                        supervise)
from app.pipeline import _unresolved_notice


# ════════════════════════════════════════════════════════════════
# C-2 · REVISE 상태로는 확신 있게 답하지 않는다
# ════════════════════════════════════════════════════════════════

def test_REVISE면_ANSWER_등급을_유지하지_않는다():
    """REVISE는 '고쳐서 내라'지 '이대로 확신 있게 내라'가 아니다."""
    result = supervise(
        "일반적인 답변입니다.",
        answerability="ANSWER",
        trap_checks=[{"id": "E1", "severity": "critical",
                      "verify_any": ["법정 외"], "correction": "법정 외 퇴직급여입니다"}],
    )
    assert result.verdict == Verdict.REVISE
    assert result.downgraded_answerability == "PARTIAL"


def test_지적이_없으면_강등하지_않는다():
    """오탐 회귀 — 멀쩡한 답변까지 PARTIAL로 낮추면 답변 품질이 떨어진다."""
    result = supervise("법정 외 퇴직급여에 해당합니다.",
                       answerability="ANSWER",
                       trap_checks=[{"id": "E1", "severity": "critical",
                                     "verify_any": ["법정 외"], "correction": "…"}])
    assert result.verdict == Verdict.APPROVE
    assert result.downgraded_answerability is None


def test_이미_PARTIAL이면_더_낮추지_않는다():
    """의도된 설계다 — 과잉 축퇴를 막는다.

    강등이 필요한 이유는 ANSWER가 '확신 있게 답한다'는 신호이기 때문이다.
    PARTIAL은 이미 한계를 고지하는 등급이라, 여기서 ASK_BACK으로 더 내리면
    사용자가 받는 정보만 줄어들 뿐 정직해지지는 않는다. 대회 요건도
    '확인조건 제시 후 상황별 결론 제공'이지 답변 회피가 아니다.

    대신 검증 미통과 고지문(C-1)은 등급과 무관하게 언제나 붙는다 —
    정직함은 고지문이 담당하고, 강등은 어조만 조정한다.
    """
    result = supervise(
        "일반적인 답변입니다.",
        answerability="PARTIAL",
        trap_checks=[{"id": "E1", "severity": "critical",
                      "verify_any": ["법정 외"], "correction": "…"}])
    assert result.verdict == Verdict.REVISE
    assert result.downgraded_answerability is None


# ════════════════════════════════════════════════════════════════
# C-1 · 해소되지 않은 지적을 사용자에게 알린다
# ════════════════════════════════════════════════════════════════

def test_해소되지_않은_지적이_고지문이_된다():
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding("적합성", "TRAP_UNADDRESSED", Verdict.REVISE,
                          "명예퇴직급여 처리가 답변에 없음",
                          "명예퇴직수당은 법정 외 퇴직급여라 수령 방법을 선택할 수 있음을 밝힐 것")],
    )
    notice, asks = _unresolved_notice(sup)
    assert "내부 검증을 완전히 통과하지 못했습니다" in notice
    assert "법정 외" in notice
    assert asks and "법정 외" in asks[0]


def test_고지문에_내부_용어를_그대로_쓰지_않는다():
    """'TRAP_UNADDRESSED', 'REVISE'는 우리 쪽 사정이다.
    사용자에게 필요한 건 무엇을 더 확인해야 하는지다."""
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding("적합성", "TRAP_UNADDRESSED", Verdict.REVISE,
                          "지적 내용", "연금실제수령연차 기준을 함께 밝힐 것")],
    )
    notice, _ = _unresolved_notice(sup)
    assert "TRAP_UNADDRESSED" not in notice
    assert "REVISE" not in notice


def test_경미한_지적만_있으면_고지하지_않는다():
    """DOWNGRADE 수준까지 매번 경고하면 고지문이 소음이 된다."""
    sup = SupervisionResult(
        verdict=Verdict.DOWNGRADE,
        findings=[Finding("부담", "SOMETHING", Verdict.DOWNGRADE, "사소함", "사소한 지시")],
    )
    notice, asks = _unresolved_notice(sup)
    assert notice == "" and asks == []


def test_지적이_없으면_고지문도_없다():
    notice, asks = _unresolved_notice(SupervisionResult(verdict=Verdict.APPROVE))
    assert notice == "" and asks == []


def test_고지_항목은_최대_2건이다():
    """확인 항목은 최대 2건 (CLAUDE.md) — 사용자 부담을 늘리지 않는다."""
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding("적합성", f"C{i}", Verdict.REVISE, f"지적{i}", f"지시{i}")
                  for i in range(5)],
    )
    notice, asks = _unresolved_notice(sup)
    assert len(asks) <= 2
    assert notice.count("· ") <= 2


# ════════════════════════════════════════════════════════════════
# F21 · 내부 진단·오류 텍스트가 고객 답변에 노출 (2026-09-05, 외부 심사
# 리포트로 발견)
#
# 콘솔에 자체 설계 질의 10건을 넣어본 심사 리포트가, l6_semantic_audit이
# 429로 실패하거나 재생성이 끝내 미해소로 남을 때 사용자 화면에
#   "의미 감사 호출 실패: HTTP 429: {"status":{"code":"42901",...}} —
#    결정론적 판정만 적용"
# 이라는 원시 예외 텍스트와,
#   "다음을 답변에 명시적으로 반영할 것 — [A9] ... (반드시 '이연퇴직소득'
#    중 한 표현을 답변 본문에 그대로 쓸 것)"
# 라는 재생성용 LLM 지시문이 그대로 나갔다는 것을 재현해 보여줬다.
# 둘 다 _unresolved_notice()가 "감사 판정"과 "감사 진행 로그/LLM 지시"를
# 구분하지 않고 f.directive or f.detail을 그대로 붙였기 때문이다.
# ════════════════════════════════════════════════════════════════

def test_의미감사_호출실패의_원시_예외_텍스트는_고지문에_나가지_않는다():
    """★ 실사용에서 확인된 치명적 결함 그대로 재현.

    supervise_with_llm_audit()의 except 분기가 만드는 CALL_FAIL Finding은
    HyperCLOVA X 429 등 원시 예외 문자열을 detail에 담는다. 이건 "감사
    진행 상황"이지 "감사 판정"이 아니므로 고객에게 보이면 안 된다.
    """
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding(
            "의미감사", "CALL_FAIL", Verdict.REVISE,
            '의미 감사 호출 실패: HTTP 429: '
            '{"status":{"code":"42901","message":"Too many requests: '
            'rate exceeded..."}} — 결정론적 판정만 적용', "")],
    )
    notice, asks = _unresolved_notice(sup)
    assert "429" not in notice and "42901" not in notice
    assert "HTTP" not in notice
    assert notice == "" and asks == [], (
        "이 finding 하나만으로는 담을 내용이 없어야 한다 — "
        "감사 진행 로그를 고지문 재료로 쓰면 안 된다")


def test_트랩_미해소_directive의_LLM_지시절은_잘라내고_교정내용은_남긴다():
    """★ TRAP_UNADDRESSED의 directive는 재생성 프롬프트용 "합격 조건"
    (2026-09-02 도입)을 담을 수 있다 — "반드시 '…' 중 한 표현을 답변
    본문에 그대로 쓸 것". 이 절은 모델에게 하는 지시이지 고객에게 하는
    말이 아니다. directive 자체는(재생성 소비자를 위해) 건드리지 않고,
    고지문에 노출할 때만 이 절을 잘라낸다 — 교정 취지(재원 구분 등)는
    남아야 한다.
    """
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding(
            "적합성", "TRAP_UNADDRESSED", Verdict.REVISE,
            "감지된 함정 중 답변에서 다뤄지지 않은 것: ['A9'] (critical 1건)",
            "다음을 답변에 명시적으로 반영할 것 — [A9] 단, 이연퇴직소득은 "
            "퇴직소득 과세기준 적용 (반드시 '이연퇴직소득' 중 한 표현을 "
            "답변 본문에 그대로 쓸 것)")],
    )
    notice, asks = _unresolved_notice(sup)
    assert "그대로 쓸 것" not in notice
    # 고정 서두 문구("반드시 별도로 확인해 주십시오")는 제외하고,
    # finding에서 온 항목 줄만 검사한다.
    item_line = next(ln for ln in notice.splitlines() if ln.startswith("· "))
    assert "반드시" not in item_line
    assert "이연퇴직소득" in item_line, "지시절만 지우고 교정 취지는 남겨야 한다"


@pytest.mark.parametrize("auditor,code", [
    ("의미감사", "SKIPPED"),
    ("법령근거", "TRACE"),
    ("법령저촉", "TRACE"),
    ("법령저촉", "NONE"),
])
def test_감사_진행_로그성_finding은_고지문에_실리지_않는다(auditor, code):
    """★ 이 넷은 "판정"이 아니라 "진행 상황" 기록이다 — 대조 대상 조문
    수·생략 사유처럼 내부 트레이스일 뿐, 사용자에게 확인을 요구할 내용이
    아니다. severity가 REVISE/BLOCK인 채로 남는 경우가 있어(예: 법령
    검사가 REVISE 판정 위에 얹힐 때) code만으로는 실수로 노출될 수 있다.
    """
    sup = SupervisionResult(
        verdict=Verdict.REVISE,
        findings=[Finding(auditor, code, Verdict.REVISE,
                          "내부 트레이스 문구", "")],
    )
    notice, asks = _unresolved_notice(sup)
    assert notice == "" and asks == []


# ════════════════════════════════════════════════════════════════
# 전체 경로 — Q-001 실패 재현 차단
# ════════════════════════════════════════════════════════════════

def test_Q001_실패_답변은_이제_확신있게_나갈_수_없다():
    """당시 최종 출력은 ANSWER 등급에, 검증 실패 표시가 전혀 없었다.

    같은 답변을 같은 조건으로 넣으면 이제는
      ① REVISE 판정을 받고  ② 등급이 강등되고  ③ 고지문이 생성돼야 한다.
    """
    from app.core.trap_rules import build_trap_context

    ctx = build_trap_context(
        "명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
        "어마어마하다던데, 절세법만 알려주세요.")
    failed_answer = (
        "연금계좌에 납입한 금액은 종합소득 산출세액에서 공제가 가능합니다. "
        "연간 600만 원 이내의 금액 또는 총 900만 원 중에서 선택할 수 있습니다.")

    result = supervise(failed_answer, answerability="ANSWER",
                       trap_checks=ctx["checks"])

    assert result.verdict == Verdict.REVISE, "감독이 이 답변을 반려하지 않는다"
    assert result.downgraded_answerability == "PARTIAL", "등급이 강등되지 않는다"

    notice, asks = _unresolved_notice(result)
    assert notice, "검증 미통과 사실이 사용자에게 전달되지 않는다"
    assert asks, "확인 항목으로 전환되지 않는다"
