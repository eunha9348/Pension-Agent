"""intent 정확 매칭이 정상 경로에서 죽어 있던 문제 (2026-09-03).

━━ 실측 결함 ━━
L1(HyperCLOVA X)은 tools를 쓰지 않는 일반 채팅 호출이라(CLAUDE.md),
intent가 '세액공제 한도 문의'·'IRP_퇴직소득세'처럼 **자유 서술**로 나온다.
실서버 think_trace에서 그대로 관측됐다. 그런데 아래 네 곳이 이 값을
규칙 추출기(TOPIC_RULES)가 내는 **정규값**과 정확 문자열로 비교했다:

  · pipeline.py `_TAX_INTENTS`            — 세제 질의 구법 문서 배제
  · reconcile_spec의 `misclassified` 판정  — 제도 오분류 교정(Q-001)
  · slot_matching.py / coverage_pipeline.py `== "상품_비교"` — 비교 예외

자유 서술은 이 비교에 전부 걸리지 않아 조용히 False로 떨어진다.
역설적으로 **L1이 실패해 규칙으로 축퇴했을 때만** 안전장치가 작동하고,
L1이 정상 동작하는 평상시(정상 경로)에는 꺼져 있었다. 테스트가 못 잡은
이유도 같다 — 기존 테스트는 전부 정규값을 직접 넣었다("배선을 검사하는
테스트는 배선을 지나가야 한다").

실서버에서 그 결과가 실제로 나타났다 — 세제 질의(E-28, "700만원이 맞나요")
인데 구법 문서(R2_KR514X450008)가 배제되지 않고 근거로 인용됐다.

━━ 수정 ━━
`normalize_intent()`가 L1의 자유 서술을 규칙 정규값으로 정규화한다.
`extract_query_spec`에서 `sanitize_spec` 직후, 즉 모든 소비처보다 먼저
적용한다. `reconcile_spec`은 건드리지 않는다 — 정규화된 값을 받으므로
그 안의 misclassified 판정도 자동으로 함께 살아난다.
"""

from __future__ import annotations

from app.analysis.query_spec import (make_extract_query_spec, normalize_intent,
                                     reconcile_spec, rule_based_spec)
from app.pipeline import _TAX_INTENTS


# ── normalize_intent 단위 테스트 ──────────────────────────────

def test_L1_자유서술을_정규값으로_정규화한다():
    """★ 실측 그대로 — 실서버 think_trace에서 관측된 값들."""
    assert normalize_intent("세액공제 한도 문의") == "세액공제"
    assert normalize_intent("IRP_퇴직소득세") == "퇴직소득세"
    assert normalize_intent("연금저축 세액공제 한도 문의") == "세액공제"


def test_더_구체적인_정규값을_먼저_본다():
    """★ '퇴직소득세_감면'이 '퇴직소득세'보다 길다 — 순서를 틀리면 잘못 잘린다."""
    assert normalize_intent("퇴직소득세_감면율이 궁금해요") == "퇴직소득세_감면"


def test_이미_정규값이면_그대로_둔다():
    assert normalize_intent("세액공제") == "세액공제"
    assert normalize_intent("상품_비교") == "상품_비교"


def test_매칭이_없으면_원문을_그대로_둔다():
    """모르는 의도를 억지로 우겨넣지 않는다."""
    assert normalize_intent("일반") == "일반"
    assert normalize_intent("도메인 이탈 질의") == "도메인 이탈 질의"


def test_빈_값은_빈_문자열로_돌아온다():
    assert normalize_intent("") == ""
    assert normalize_intent(None) == ""


# ── 안전장치 1: 세제 질의 구법 문서 배제 (pipeline._TAX_INTENTS) ──

def test_자유서술_세제_intent가_TAX_INTENTS에_걸린다():
    """★ 이게 이번 결함의 핵심 — 정규화 전에는 전부 False였다."""
    raw_intents = ["세액공제 한도 문의", "IRP_퇴직소득세",
                  "연금저축 세액공제 한도 문의", "과세방식 문의"]
    for raw in raw_intents:
        normalized = normalize_intent(raw)
        assert normalized in _TAX_INTENTS, (
            f"{raw!r} 정규화 결과 {normalized!r}가 여전히 세제 배제 필터를 "
            f"피해간다 — 구법 문서가 다시 새어나갈 수 있다")


def test_정규화_안_한_원문은_TAX_INTENTS에_안_걸린다():
    """대조군 — 정규화가 실제로 하는 일이 있다는 걸 보여준다."""
    assert "세액공제 한도 문의" not in _TAX_INTENTS
    assert "IRP_퇴직소득세" not in _TAX_INTENTS


# ── 안전장치 2: reconcile_spec의 misclassified 교정 (Q-001) ──

Q_MYEONGTOE = ("명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
               "어마어마하다던데, 절세법만 알려주세요.")


def test_자유서술_오분류도_정규화_후에는_교정된다():
    """★ 예전엔 intent가 '세액공제'로 정확히 와야만 교정이 발동했다.

    L1이 실제로 내는 형태('세액공제 한도 문의' 등)로는 misclassified 판정의
    `== "세액공제"` 비교를 통과하지 못해 교정이 불발했다.
    """
    llm_spec = {
        "query": Q_MYEONGTOE,
        "intent": normalize_intent("세액공제 한도 문의로 보입니다"),
        "asked_for": [{"id": "seaek_gongje", "description": "연금저축·IRP 세액공제",
                       "type": "calculation",
                       "calc_function": "사적연금_납입한도_세액공제_계산",
                       "required": True}],
        "planned_calls": [{"function": "사적연금_납입한도_세액공제_계산", "args": {}}],
        "source": "llm",
    }
    assert llm_spec["intent"] == "세액공제", "정규화 자체가 실패하면 아래 검증이 무의미하다"
    out = reconcile_spec(llm_spec, rule_based_spec(Q_MYEONGTOE), Q_MYEONGTOE)
    assert out["intent"] != "세액공제", "퇴직소득 질의가 세액공제로 남아 있다"


# ── 안전장치 3: 상품_비교 예외 처리 ──

def test_자유서술_비교_intent도_정규화된다():
    assert normalize_intent("여러 상품 비교해서 알려주세요 (상품_비교로 판단)") == "상품_비교"


# ── end-to-end — extract_query_spec을 통과하면 정규화가 실제로 적용되는가 ──

class _FreeTextIntentClient:
    """L1이 실제로 내는 것과 같은 자유 서술 intent를 돌려주는 대역."""

    is_mock = False

    def __init__(self, intent: str):
        self.intent = intent
        self.calls = 0

    def call(self, system, user, purpose="", **kw):
        self.calls += 1
        return ('{"intent": "%s", "asked_for": [{"id": "s1", '
                '"description": "세액공제 한도", "type": "fact", '
                '"required": true}]}') % self.intent


def test_extract_query_spec이_자유서술_intent를_정규화해서_돌려준다():
    """★ 배선 테스트 — 공개 진입점(make_extract_query_spec)을 통과해야 한다."""
    c = _FreeTextIntentClient("세액공제 한도 문의")
    spec = make_extract_query_spec(client=c)("연금저축 세액공제 한도가 얼마인가요?")
    assert spec["intent"] == "세액공제"
    assert spec["intent"] in _TAX_INTENTS


def test_extract_query_spec_결과가_바로_TAX_INTENTS_필터를_통과한다():
    """★ 이것이 실서버에서 재현됐던 시나리오 — 세제 질의가 구법 배제 필터에 걸리는가."""
    c = _FreeTextIntentClient("과세방식이 궁금해요")
    spec = make_extract_query_spec(client=c)("연금 받을 때 세금이 얼마나 붙나요?")
    assert spec.get("intent") in _TAX_INTENTS, (
        "정상 경로(L1 성공)에서도 세제 질의가 구법 문서 배제 대상으로 "
        "잡혀야 한다 — 안 잡히면 E-28이 재발한다")


def test_정규화_사실이_think_trace에_남는다():
    """★ 조용히 바뀌면 디버깅이 불가능하다 — 무엇을 왜 바꿨는지 남겨야 한다."""
    logged = []
    c = _FreeTextIntentClient("세액공제 한도 문의")
    make_extract_query_spec(client=c, trace_log=lambda step, msg, **kw: logged.append((step, msg)))(
        "연금저축 세액공제 한도가 얼마인가요?")
    steps = [s for s, _ in logged]
    assert "질의분석_의도표준화" in steps
