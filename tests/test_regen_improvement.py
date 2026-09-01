"""재생성이 실제로 품질을 높이는가 — 부분 개선 채택 (2026-09-02).

━━ 실측 결함 ━━
"확실히 통과하지 못한 답변은 재생성으로 품질을 높인다"는 설계 의도가
정확히 반대로 동작하고 있었다. 재생성 채택 기준이 전부-아니면-전무
(all-or-nothing)라서, **명백히 개선된 답변이 통째로 버려지고 더 나쁜
원본이 최종 답변으로 나갔다.**

    원본   : 미해소 함정 ['C1', 'C2']  ("1,500만원 이하로 조절하세요" — 오답)
    재생성 : 미해소 함정 ['C1']        (C2를 정확히 반영한 개선된 답변)
    결과   : C1이 남았다는 이유로 재생성 기각 → 원본(오답)이 최종 답변

━━ 수정 ━━
`_is_improvement()`로 **원본보다 확실히 나은가**를 결정론적으로 판정해,
완전히 통과하지 못했어도 개선이면 채택한다. 판정 자체는 완화하지 않는다 —
`unresolved`는 True로 남아 고지·강등이 그대로 유지되므로, 사용자에게는
여전히 "내부 검증을 완전히 통과하지 못했다"고 알린다. 두 후보 중 **덜
나쁜 쪽을 고르는 것**일 뿐 감사 판정을 무르는 것이 아니다.

━━ 무엇을 완화하지 않는가 (중요) ━━
· 수치 검증은 절대 완화하지 않는다 — 근거 없는 수치가 든 답변은 '개선'이
  아니라 날조 위험이다.
· 미해소 함정이 **엄격히 줄었을 때만**(진부분집합) 개선으로 본다. 하나
  고치고 하나 깨뜨린 답변은 채택하지 않는다 — 품질이 단조 증가해야 한다.
"""

from __future__ import annotations

from app.pipeline import _is_improvement

_CHECKS = [
    {"id": "C1", "severity": "critical", "correction": "전액이 대상입니다.",
     "docs": ["doc39"], "verify_any": ["전액"]},
    {"id": "C2", "severity": "critical", "correction": "이연퇴직소득은 제외됩니다.",
     "docs": ["doc39"], "verify_any": ["이연퇴직소득"]},
]


class _Verdict:
    """verify_grounding 반환값 대역 — numeric.passed만 본다."""

    class _Num:
        def __init__(self, passed): self.passed = passed

    def __init__(self, numeric_passed=True):
        self.numeric = self._Num(numeric_passed)


_NONE = "1,500만원 이하로 조절하시는 게 중요합니다."          # C1·C2 둘 다 미해소
_C2_ONLY = "이연퇴직소득은 1,500만원 계산에 포함되지 않습니다."  # C2만 해소
_BOTH = ("이연퇴직소득은 계산에서 제외되며, 초과 시 전액이 과세 "
         "선택 대상입니다.")                                   # 둘 다 해소
_C1_ONLY = "1,500만원을 넘으면 전액이 과세 선택 대상입니다."     # C1만 해소


# ── 개선으로 인정하는 경우 ───────────────────────────────────

def test_미해소_함정이_줄면_개선이다():
    """★ 실측 사고 그대로 — ['C1','C2'] → ['C1']."""
    assert _is_improvement(_NONE, _C2_ONLY, _Verdict(), _CHECKS) is True


def test_전부_해소되면_당연히_개선이다():
    assert _is_improvement(_NONE, _BOTH, _Verdict(), _CHECKS) is True


# ── 개선으로 인정하지 않는 경우 ──────────────────────────────

def test_수치검증에_실패하면_개선이_아니다():
    """★ 근거 없는 수치가 든 답변은 함정을 몇 개 고쳤든 채택하지 않는다.

    이것을 완화하면 재생성이 날조를 들여오는 뒷문이 된다.
    """
    assert _is_improvement(_NONE, _BOTH, _Verdict(numeric_passed=False),
                           _CHECKS) is False


def test_하나_고치고_하나_깨뜨리면_개선이_아니다():
    """★ 진부분집합일 때만 개선 — 품질은 단조 증가해야 한다.

    C2를 고쳤지만 C1을 깨뜨린 답변({C1} → {C2})은 개수는 같고 내용만
    바뀐 것이라 '나아졌다'고 말할 수 없다.
    """
    assert _is_improvement(_C1_ONLY, _C2_ONLY, _Verdict(), _CHECKS) is False


def test_그대로면_개선이_아니다():
    assert _is_improvement(_NONE, _NONE, _Verdict(), _CHECKS) is False


def test_더_나빠지면_개선이_아니다():
    assert _is_improvement(_C2_ONLY, _NONE, _Verdict(), _CHECKS) is False


def test_비교할_함정이_없으면_개선을_주장하지_않는다():
    """기준이 없는데 '나아졌다'고 하면 근거 없는 주장이 된다."""
    assert _is_improvement(_NONE, _BOTH, _Verdict(), []) is False
    assert _is_improvement(_NONE, _BOTH, _Verdict(), None) is False


# ── end-to-end — 파이프라인에서 실제로 채택되는가 ────────────

class _RegenImproves:
    """초안은 C2를 놓치고, 재생성에서 C2를 반영하는 대역(C1은 끝까지 미해소)."""

    is_mock = False

    def __init__(self):
        self.calls: list[str] = []

    def call(self, system, user, purpose="?", **kw):
        self.calls.append(purpose)
        if "감사자" in system:
            return '{"verdict":"APPROVE","findings":[]}'
        if purpose == "l5_supervisor":
            return ("[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
                    "연 1,500만원을 초과하면 종합과세 대상이 될 수 있으니 "
                    "1,500만원 이하로 조절하시는 게 중요합니다.\n\n"
                    "[한계 고지]\n확인 필요")
        if purpose == "l5_regenerate":
            return ("[확인된 조건]\n퇴직금을 재원으로 하는 IRP로 이해했습니다.\n\n"
                    "[조건별 결론]\n퇴직급여를 재원으로 하는 연금소득"
                    "(이연퇴직소득)은 1,500만원 분리과세 한도 계산에 포함되지 "
                    "않습니다. 공적연금도 마찬가지입니다.\n\n"
                    "[한계 고지]\n계좌 구성 확인이 필요합니다.")
        return "진단"

    def call_with_functions(self, s, u, t, purpose="?", **kw):
        self.calls.append(purpose)
        return {"name": None, "arguments": None, "raw": ""}


def test_개선된_재생성이_채택되고_오답이_사라진다():
    """★ 이번 결함의 핵심 — 개선안이 실제 최종 답변에 반영돼야 한다."""
    from app.pipeline import answer_question

    r = answer_question(
        "Q", "IRP에 있는 퇴직금인데 연 1500만원 넘게 받으면 세금이 어떻게 되나요?",
        client=_RegenImproves())

    assert "L6_재생성_부분반영" in r["think_trace"]
    assert "이연퇴직소득" in r["answer"], "개선된 내용이 채택되지 않았다"
    assert "1,500만원 이하로 조절" not in r["answer"], (
        "기각돼 원본 오답이 그대로 남았다 — 이번 결함이 재발했다")


def test_부분_채택해도_고지와_강등은_유지된다():
    """★ 판정을 완화한 것이 아니다 — 남은 지적은 반드시 사용자에게 도달한다."""
    from app.pipeline import answer_question

    r = answer_question(
        "Q", "IRP에 있는 퇴직금인데 연 1500만원 넘게 받으면 세금이 어떻게 되나요?",
        client=_RegenImproves())

    assert "검증_미통과_고지" in r["think_trace"]
    assert "내부 검증을 완전히 통과하지 못했습니다" in r["answer"]
    # 남은 함정(C1)의 시정 지시가 고지문에 실려야 한다
    assert "전액" in r["answer"]


def test_개선되지_않은_재생성은_여전히_기각된다():
    """★ 안전판 — 나아지지 않았는데 채택하면 안 된다."""
    from app.pipeline import answer_question

    class _NoImprove(_RegenImproves):
        def call(self, system, user, purpose="?", **kw):
            self.calls.append(purpose)
            if "감사자" in system:
                return '{"verdict":"APPROVE","findings":[]}'
            if purpose in ("l5_supervisor", "l5_regenerate", "subagent_rewrite"):
                return ("[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
                        "1,500만원 이하로 조절하시는 게 중요합니다.\n\n"
                        "[한계 고지]\n확인 필요")
            return "진단"

    r = answer_question(
        "Q", "IRP에 있는 퇴직금인데 연 1500만원 넘게 받으면 세금이 어떻게 되나요?",
        client=_NoImprove())
    assert "L6_재생성_부분반영" not in r["think_trace"], (
        "나아지지 않은 재생성을 개선으로 오판했다")
