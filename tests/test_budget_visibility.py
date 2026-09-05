"""예산 때문에 재생성이 생략됐다는 사실이 밖에서 보이는가 (2026-09-02).

━━ 실측 결함 ━━
REVISE가 떴는데 `L6_재생성_생략`으로 빠지는 경우가 나왔다. 그런데
그 줄의 문구가 "재생성 예산이 없거나 mock 모드"였다 — **둘 중 무엇이었는지,
남은 시간이 얼마였는지 알 수 없었다.** 그래서 예산 상수를 근거 있게 조정할
방법이 없었다. "법령 판정을 '못 한 것'과 '대상이 없던 것'을 구별할 것"과
같은 계열의 결함이다 — 사유가 없으면 고칠 수 없다.

구제 재생성 쪽은 더 나빴다. 예산이 모자라 열리지 않으면 **아무 기록도
남지 않아서**, 밖에서 보면 "구제를 시도했다가 실패한 것"과 구별되지 않았다.

━━ 함께 넣은 것 ━━
전체 예산을 환경변수(PIPELINE_BUDGET_SEC)로 뺐다. 이 값은 평가 측
타임아웃과의 트레이드오프라 코드가 혼자 정할 수 없다 — 배포 설정에서
재고 조정할 수 있어야 한다. 다만 **너무 작은 값은 거부한다**: 0이나 음수가
들어오면 모든 LLM 단계가 생략돼 "200 OK인데 HCX가 만들지 않은 답변"이
나가고, 그건 절대 제약 #1 위반이다.
"""

from __future__ import annotations

import app.pipeline as P


# ── 예산 환경변수 ────────────────────────────────────────────

def test_지정하지_않으면_기본값을_쓴다(monkeypatch):
    monkeypatch.delenv("PIPELINE_BUDGET_SEC", raising=False)
    assert P._budget_sec("PIPELINE_BUDGET_SEC", 45.0) == 45.0


def test_지정하면_그_값을_쓴다(monkeypatch):
    monkeypatch.setenv("PIPELINE_BUDGET_SEC", "60")
    assert P._budget_sec("PIPELINE_BUDGET_SEC", 45.0) == 60.0


def test_숫자가_아니면_기본값으로_떨어진다(monkeypatch):
    """기동을 막지 않는다 — restart 루프가 오타보다 나쁘다."""
    monkeypatch.setenv("PIPELINE_BUDGET_SEC", "육십초")
    assert P._budget_sec("PIPELINE_BUDGET_SEC", 45.0) == 45.0


def test_너무_작은_값은_거부한다(monkeypatch):
    """★ 0이나 음수를 그대로 받으면 모든 LLM 단계가 통째로 생략된다.

    그 상태로도 200 OK에 그럴듯한 답변이 나가는데, 그 답변은 HyperCLOVA X
    생성물이 아니다 — 절대 제약 #1 위반이고 평가는 단일 GET이라 되돌릴 수
    없다. 오타 하나로 그 상태를 만들 수 있는 자리이므로 하한을 둔다.
    """
    for bad in ("0", "-5", "3"):
        monkeypatch.setenv("PIPELINE_BUDGET_SEC", bad)
        assert P._budget_sec("PIPELINE_BUDGET_SEC", 45.0) == 45.0, bad


# ── 생략 사유가 think_trace에 남는가 ─────────────────────────

def test_예산_부족으로_재생성을_생략하면_남은_시간이_찍힌다(monkeypatch):
    """★ 사유와 숫자가 없으면 예산을 근거 있게 조정할 수 없다.

    예산을 0에 가깝게 만들어(Deadline.total을 낮춰) 재생성 게이트가 반드시
    닫히게 한 뒤, 그 사유가 숫자와 함께 기록되는지 본다.
    """
    import re

    class _Client:
        """항상 REVISE를 내는 감사자 — mock이 아니라고 주장한다."""

        is_mock = False

        def call(self, system, user, purpose="?", **kw):
            if "감사자" in system:
                return ('{"verdict":"REVISE","findings":[{"code":"X",'
                        '"detail":"고칠 것","directive":"고칠 것"}]}')
            return ("[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
                    "설명입니다.\n\n[한계 고지]\n확인 필요")

        def call_with_functions(self, s, u, t, purpose="?", **kw):
            return {"name": None, "arguments": None, "raw": ""}

    # 재생성 게이트만 확실히 닫는다 — L1/L5/L6는 통과시키고 재생성 예산만
    # 도달 불가능하게 만든다(총 예산보다 큰 값이면 allows()가 항상 False).
    monkeypatch.setattr(P, "BUDGET_REGEN", P.TOTAL_BUDGET_SEC + 100.0)

    r = P.answer_question("Q", "연금저축 세액공제 한도가 얼마인가요?",
                          client=_Client())
    lines = [ln for ln in r["think_trace"].splitlines() if "재생성_생략" in ln]
    assert lines, "재생성을 생략했는데 기록이 남지 않았다"
    for line in lines:
        assert re.search(r"남은 시간 -?\d+\.\d+초", line), line
        assert "총 예산" in line


def test_구제재생성_생략도_기록으로_남는다(monkeypatch):
    """★ 예전에는 이 경우가 아무 기록도 남기지 않았다.

    밖에서 보면 "구제를 시도했다가 실패한 것"과 구별되지 않는다.
    """

    class _Client:
        is_mock = False

        def call(self, system, user, purpose="?", **kw):
            if "감사자" in system:
                return ('{"verdict":"REVISE","findings":[{"code":"X",'
                        '"detail":"고칠 것","directive":"고칠 것"}]}')
            return ("[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
                    "설명입니다.\n\n[한계 고지]\n확인 필요")

        def call_with_functions(self, s, u, t, purpose="?", **kw):
            return {"name": None, "arguments": None, "raw": ""}

    monkeypatch.setattr(P, "BUDGET_REGEN", P.TOTAL_BUDGET_SEC + 100.0)
    monkeypatch.setattr(P, "BUDGET_SUBAGENT_REWRITE", P.TOTAL_BUDGET_SEC + 100.0)

    r = P.answer_question("Q", "연금저축 세액공제 한도가 얼마인가요?",
                          client=_Client())
    assert "SubAgent_구제_생략" in r["think_trace"], (
        "구제 재생성이 열리지 않은 사유가 기록되지 않았다")


# ── 예산표가 실제 비용을 반영하는가 ──────────────────────────
#
# ⚠️ 게이트 값이 실제 소요보다 작으면 **게이트가 통과시켜 놓고 총 예산을
#    넘긴다.** 그러면 예산표가 거짓이 되고, 그 상태로는 어떤 값을 조정해도
#    근거가 없다. 예전 BUDGET_REGEN=10.0이 정확히 그랬다 — 재생성은
#    생성 + 재검증 두 번을 호출하는데 생성 한 번 값만 잡혀 있었다.

def test_재생성_예산이_재검증_비용을_포함한다():
    """★ 재생성 = 생성 + 재검증(의미 감사). 재검증은 게이트 밖에서 무조건 돈다.

    검증 없이 채택하면 감사를 우회하는 뒷문이 되므로 재검증은 뺄 수 없다.
    따라서 게이트도 그 비용을 포함해야 한다.
    """
    assert P.BUDGET_REGEN > P.BUDGET_L6, (
        "재생성 예산이 재검증(의미 감사) 비용조차 담지 못한다")


def test_구제재생성_예산도_재검증_비용을_포함한다():
    assert P.BUDGET_SUBAGENT_REWRITE > P.BUDGET_L6


def test_마지막_단계가_총_예산_안에서_닫힌다():
    """★ 게이트는 중단 장치가 아니다 — 단계 예산이 총 예산을 넘으면 안 된다.

    넘으면 그 단계는 영영 열리지 않거나(remaining이 그만큼 될 수 없다),
    열리는 순간 총 예산 밖에서 끝난다. 어느 쪽이든 예산표가 거짓이 된다.
    """
    for name in ("BUDGET_L1", "BUDGET_L5", "BUDGET_L6", "BUDGET_REGEN",
                 "BUDGET_SUBAGENT", "BUDGET_SUBAGENT_REWRITE"):
        assert getattr(P, name) < P.TOTAL_BUDGET_SEC, name


def test_총_예산은_허용_상한보다_여유를_둔다():
    """★ 상한과 같은 값으로 두면 마지막 단계가 상한 밖에서 끝난다.

    ━━ 상한이 60 → 300으로 정정됨 (2026-09-05) ━━
    예전 이 테스트는 CEILING = 60.0 이었다. 그 60초는 **확인된 값이 아니라
    추측**이었고, 디스코드 공지 원문이 실제 값을 밝혔다:
      "문항 당 응답 대기 시간(타임아웃)은 300초입니다.
       타임아웃 또는 5xx 오류 발생 시, 최대 2회를 재시도합니다."

    불변식 자체는 그대로다 — **총 예산 < 허용 상한**, 그리고 여유를 둘 것.
    바뀐 것은 상한 상수뿐이다(약화가 아니라 사실 정정).

    여유 폭도 함께 고정한다: 최악 경로는 HCX 7회 × (타임아웃 15초 +
    재시도 1회) ≈ 210초라, 마지막 게이트가 t≤(TOTAL - 그 단계 예산)에
    열려도 상한 안에서 닫혀야 한다.
    """
    CEILING = 300.0                      # 주최측 공지 확인값
    assert P.TOTAL_BUDGET_SEC < CEILING, (
        "총 예산이 허용 상한 이상이다 — 마지막 단계가 상한 밖에서 끝난다")

    # 마지막 게이트가 가장 늦게 열린 뒤 최악(타임아웃+재시도 ≈ 30초)으로
    # 끝나도 상한 안이어야 한다.
    worst_last_stage = 30.0
    assert P.TOTAL_BUDGET_SEC + worst_last_stage < CEILING, (
        f"총 예산 {P.TOTAL_BUDGET_SEC}초 + 마지막 단계 최악 {worst_last_stage}초가 "
        f"상한 {CEILING}초를 넘는다")


def test_예산이_재생성_구제재생성을_모두_열_수_있다():
    """★ 55초 시절에는 이게 성립하지 않았다 — 그래서 고쳤다.

    예산이 모자라면 REVISE 재생성(`L6_재생성_생략`)과 구제 재생성
    (`SubAgent_구제_생략`)이 통째로 건너뛰어진다. 그 둘은 감독이 반려한
    답변을 고칠 유일한 기회이므로, 예산 부족으로 잘려 나가면 "재생성으로
    품질을 올린다"는 설계가 이름만 남는다.

    전 구간(L1 → L5' → L6 → 재생성 → 구제 재생성)이 순차로 전부 열릴 수
    있는지를 예산 합으로 확인한다.
    """
    full_path = (P.BUDGET_L1 + P.BUDGET_L5 + P.BUDGET_L6
                 + P.BUDGET_REGEN + P.BUDGET_SUBAGENT_REWRITE)
    assert full_path <= P.TOTAL_BUDGET_SEC, (
        f"전 구간 예산 합 {full_path}초 > 총 예산 {P.TOTAL_BUDGET_SEC}초 — "
        f"구제 재생성이 앞 단계가 빨리 끝났을 때만 열린다")
