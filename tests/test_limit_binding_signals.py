"""결정적 수치가 답변에 도달하지 않던 문제 — 한도 초과 신호 (2026-09-03).

━━ 실측 결함 (E-03) ━━
"연금저축에 900만원 넣으면 다 공제되나요?"

계산 자체는 정확했다 — 연금저축 단독 한도 600만원으로 잘라 공제액
99만원을 낸다. 문제는 답변에 실리도록 **강제되는 값이 99만원 하나뿐**
이었다는 것이다. `_presence_targets`는 계산값(A_tax_credit)이 있으면
한도 상수를 전부 면제하는데, 이 질문은 "한도를 넘었는가"를 묻는 전제
확인형이라 600만원이 곧 답이다.

더 근본적인 문제: `IsLimitExceeded`는 연간 총납입한도(1,800만원)만 본다.
900은 1,800을 안 넘으므로 이 플래그는 항상 False였다 — 정작 걸린
**연금저축 단독 한도(600만원) 초과**는 어떤 신호로도 드러나지 않았다.
LLM이 "다 공제됩니다"라고 써도 어떤 판정도 이를 못 잡는다.

━━ 수정 ━━
계산함수가 어느 한도가 실제로 결과를 깎았는지 두 개의 새 플래그로 밝힌다
(`IsPensionSavingLimitExceeded` · `IsCombinedLimitExceeded`). 이 신호를
두 곳이 함께 읽는다:
  · `numeric_verifier._presence_targets` — 그 한도가 결과를 깎았을 때만
    계산값이 있어도 한도 상수를 답변에 강제로 싣는다.
  · `supervisory_board.audit_anomaly` — 한도 초과 사실을 답변에 명시했는지
    REVISE 대상으로 삼는다(기존 IsLimitExceeded와 같은 자리, 다른 한도).

━━ 무엇을 완화하지 않는가 ━━
A08 결함(900·1,800이 무관한데도 강제돼 정답이 강등된 사례)을 되돌리지
않는다. computed 값이 있으면 여전히 원칙적으로 한도를 면제하고,
**실제로 그 한도가 결과를 깎았을 때만** 예외로 되살린다.
"""

from __future__ import annotations

from app.core.numeric_verifier import _presence_targets, verify_calc_presence
from app.core.pension_calc_functions import calc_private_contribution_limit
from app.core.supervisory_board import audit_anomaly


# ── 계산함수가 정확한 신호를 내는가 ───────────────────────────

def test_단독_한도만_초과하면_그_플래그만_선다():
    """★ 실측 E-03 그대로 — 900만원 단독 납입."""
    r = calc_private_contribution_limit(X_pension_saving=900)
    assert r["IsPensionSavingLimitExceeded"] is True
    assert r["IsCombinedLimitExceeded"] is False
    assert r["IsLimitExceeded"] is False, "1,800만원(총납입한도)은 안 넘었다"


def test_합산_한도만_초과하면_그_플래그만_선다():
    r = calc_private_contribution_limit(X_pension_saving=600, Y_irp_personal=500)
    assert r["IsPensionSavingLimitExceeded"] is False, "단독 600은 안 넘었다"
    assert r["IsCombinedLimitExceeded"] is True, "600+500=1100 > 900"


def test_아무_한도도_안_넘으면_전부_False():
    r = calc_private_contribution_limit(X_pension_saving=500)
    assert r["IsPensionSavingLimitExceeded"] is False
    assert r["IsCombinedLimitExceeded"] is False
    assert r["IsLimitExceeded"] is False


def test_총납입한도_초과는_기존대로_작동한다():
    """회귀 방지 — 기존 IsLimitExceeded 판정을 건드리지 않았는지."""
    r = calc_private_contribution_limit(X_pension_saving=600, Y_irp_personal=1300)
    assert r["IsLimitExceeded"] is True, "600+1300=1900 > 1800"


# ── presence 강제가 정확한 한도만 되살리는가 ──────────────────

def test_E03_단독한도_초과분이_강제_대상에_들어간다():
    """★ 이번 결함의 핵심 — 계산값이 있어도 600만원이 요구돼야 한다."""
    r = calc_private_contribution_limit(X_pension_saving=900)
    labels = [lbl for lbl, _v, _s in _presence_targets(r)]
    assert "연금저축 단독 세액공제 한도" in labels
    assert "세액공제액" in labels


def test_한도를_안_넘으면_계산값만_요구된다():
    """대조군 — 한도가 안 걸리면 예전처럼 계산값만 요구돼야 한다."""
    r = calc_private_contribution_limit(X_pension_saving=500)
    labels = [lbl for lbl, _v, _s in _presence_targets(r)]
    assert labels == ["세액공제액"]


def test_A08_회귀_없음_단독한도만_강제되고_무관한_한도는_안_실린다():
    """★ 예전 결함의 정반대 방향 — 900·1,800을 억지로 요구하면 안 된다.

    1,200만원 단독 납입은 단독 한도(600)만 넘고 합산·총액은 안 넘는다.
    강제 대상에 900이나 1,800이 섞이면 A08이 재발한다.
    """
    r = calc_private_contribution_limit(X_pension_saving=1200)
    labels = [lbl for lbl, _v, _s in _presence_targets(r)]
    assert "연금저축 단독 세액공제 한도" in labels
    assert "연금저축+IRP 합산 세액공제 한도" not in labels
    assert "연간 총 납입한도" not in labels


def test_600만원을_답변에_쓰면_E03_검증을_통과한다():
    """★ 정답 형태 — '한도만 실었을 때' verify_calc_presence가 통과하는지."""
    r = calc_private_contribution_limit(X_pension_saving=900)
    answer = "연금저축은 단독으로는 600만원까지만 세액공제되며, 99만원이 공제됩니다."
    result = verify_calc_presence(answer, [r])
    assert result.passed


def test_한도를_안_쓰면_E03_검증이_실패한다():
    """★ 오답 형태 — '다 공제됩니다'류 답변을 잡아야 한다."""
    r = calc_private_contribution_limit(X_pension_saving=900)
    answer = "네, 900만원 전액 세액공제 대상입니다."
    result = verify_calc_presence(answer, [r])
    assert not result.passed


# ── 이상치 감사가 한도 초과를 REVISE로 잡는가 ─────────────────

def test_단독한도_초과가_REVISE를_낸다():
    r = calc_private_contribution_limit(X_pension_saving=900)
    findings = audit_anomaly([r], {})
    codes = [f.code for f in findings]
    assert "PENSION_SAVING_LIMIT_EXCEEDED" in codes


def test_합산한도_초과가_REVISE를_낸다():
    r = calc_private_contribution_limit(X_pension_saving=600, Y_irp_personal=500)
    findings = audit_anomaly([r], {})
    codes = [f.code for f in findings]
    assert "COMBINED_LIMIT_EXCEEDED" in codes


def test_한도_안_넘으면_새_판정이_안_뜬다():
    r = calc_private_contribution_limit(X_pension_saving=500)
    findings = audit_anomaly([r], {})
    codes = [f.code for f in findings]
    assert "PENSION_SAVING_LIMIT_EXCEEDED" not in codes
    assert "COMBINED_LIMIT_EXCEEDED" not in codes
