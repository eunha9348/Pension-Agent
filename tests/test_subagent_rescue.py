"""Sub-Agent 구제 재생성 — 배선 테스트.

L6가 REVISE를 내고 L5' 재생성마저 지적을 해소하지 못했을 때,
Sub-Agent가 **직접 답변을 다시 쓰고** 그 결과가 사용자에게 도달하는가.

━━ 왜 파이프라인을 실제로 지나가는가 ━━
부품(rescue_answer)만 직접 불러 보면 호출자가 결과를 안 받아도 통과한다.
실제로 이 저장소에서 그 형태의 결함이 두 번 있었다(법령 계층 미배선,
supervise_plan 판정 폐기). 그래서 여기서는 answer_question 진입점으로
들어가 최종 answer 필드까지 확인한다.

━━ 절대 깨지면 안 되는 것 ━━
구제 답변도 **반드시 재검증을 거친다.** 검증을 통과하지 못하면 채택하지
않는다. 이걸 건너뛰면 Sub-Agent가 감사를 우회하는 뒷문이 된다.
"""

from __future__ import annotations

import json

from app.pipeline import answer_question

# L6 감사가 REVISE를 내도록 만드는 응답 (단정 표현을 지적)
_AUDIT_REVISE = json.dumps({
    "verdict": "REVISE",
    "findings": [{"code": "의미 정합성", "severity": "REVISE",
                  "detail": "설명이 조건과 어긋납니다",
                  "directive": "조건별로 나눠 서술할 것"}],
}, ensure_ascii=False)

_AUDIT_APPROVE = json.dumps({"verdict": "APPROVE", "findings": []},
                            ensure_ascii=False)

_RESCUED_TEXT = ("말씀하신 조건이라면 연금저축 단독으로는 600만원까지 "
                 "세액공제 대상입니다. 다만 총급여 구간에 따라 공제율이 "
                 "달라질 수 있어 소득 구간을 확인해 주시면 더 정확히 "
                 "안내드릴 수 있습니다.")


class _FakeClient:
    """purpose로 단계를 갈라 응답하는 가짜 HCX.

    is_mock=False 여야 재생성·구제 경로가 열린다(mock이면 건너뛴다).
    """

    is_mock = False

    def __init__(self, rescue_text=_RESCUED_TEXT, audit_after_rescue=_AUDIT_APPROVE):
        self.purposes: list[str] = []
        self.rescue_text = rescue_text
        self.audit_after_rescue = audit_after_rescue
        self._audit_calls = 0

    def call(self, system, user, purpose="generic", **kw):
        self.purposes.append(purpose)

        if purpose == "l1_query_spec":
            return json.dumps({
                "intent": "세액공제",
                "asked_for": [{"id": "a", "description": "세액공제 한도",
                               "type": "fact"}],
                "search_terms": ["세액공제"],
            }, ensure_ascii=False)

        if purpose == "l5_supervisor":
            return "세액공제는 이 상품이 가장 유리합니다."

        if purpose == "l5_regenerate":
            # 재생성도 같은 문제를 반복한다 → 지적이 해소되지 않는다
            return "역시 이 상품이 가장 유리합니다."

        if purpose == "subagent_rewrite":
            return self.rescue_text

        if purpose == "l6_semantic_audit":
            self._audit_calls += 1
            # 1) 초안 감사 → REVISE, 2) 재생성 감사 → REVISE,
            # 3) 구제 답변 감사 → 시나리오에 따라
            if self._audit_calls >= 3:
                return self.audit_after_rescue
            return _AUDIT_REVISE

        return ""


# ⚠️ 계산함수가 붙는 질의를 쓰면 안 된다. 계산값 누락(CALC_NOT_SHOWN,
#    DOWNGRADE)이 REVISE보다 상위 판정이라 REVISE 분기 자체가 열리지
#    않는다. 여기서 보려는 것은 REVISE 경로이므로 사실형 질의를 쓴다.
_Q = "IRP와 연금저축의 차이가 무엇인가요?"


def test_구제_재생성_답변이_사용자에게_도달한다():
    """★ 배선 — Sub-Agent가 쓴 답변이 최종 answer가 되는가."""
    c = _FakeClient()
    body = answer_question("RESCUE-1", _Q, client=c)

    assert "subagent_rewrite" in c.purposes, (
        "Sub-Agent 구제 재생성이 호출되지 않았다 — 배선이 끊겼다")
    assert "세액공제 대상입니다" in body["answer"], (
        "Sub-Agent가 다시 쓴 답변이 최종 답변에 반영되지 않았다")
    assert "가장 유리합니다" not in body["answer"], (
        "반려된 원본이 그대로 남아 있다")


def test_구제_재생성은_L5_재생성_뒤에만_돈다():
    """순서 — 감사(REVISE) → L5' 재생성 → 그래도 안 되면 Sub-Agent."""
    c = _FakeClient()
    answer_question("RESCUE-2", _Q, client=c)

    assert "l5_regenerate" in c.purposes
    assert c.purposes.index("l5_regenerate") < c.purposes.index("subagent_rewrite"), (
        "Sub-Agent가 L5' 재생성보다 먼저 돌았다 — 순서가 뒤집혔다")


def test_구제_답변도_검증을_통과해야_채택된다():
    """★ 감사 우회 금지 — 검증에 실패하면 구제 답변을 쓰지 않는다.

    이걸 건너뛰면 Sub-Agent가 감사를 빠져나가는 뒷문이 되고,
    "LLM 감사는 심각도를 올릴 수만 있다"는 단조성이 무너진다.
    """
    # 구제 답변에 대한 감사도 REVISE를 유지한다
    c = _FakeClient(audit_after_rescue=_AUDIT_REVISE)
    body = answer_question("RESCUE-3", _Q, client=c)

    assert "subagent_rewrite" in c.purposes, "구제 시도 자체는 있어야 한다"
    assert "내부 검증을 완전히 통과하지 못했습니다" in body["answer"], (
        "검증을 통과하지 못한 구제 답변이 고지 없이 채택됐다")


def test_구제_재생성이_실패해도_5필드는_지켜진다():
    """호출이 빈 문자열을 돌려줘도 응답 계약은 흔들리지 않는다."""
    c = _FakeClient(rescue_text="")
    body = answer_question("RESCUE-4", _Q, client=c)

    for k in ("question_id", "question", "retrieved_context",
              "think_trace", "answer"):
        assert body.get(k), f"{k} 가 비었다"


def test_mock_클라이언트에서는_구제를_시도하지_않는다():
    """mock은 결정론적 대역이라 재생성이 의미가 없다 — 지연만 늘린다."""
    from app.config import Settings
    from app.llm.clova import MockClovaClient

    body = answer_question("RESCUE-5", _Q, client=MockClovaClient(Settings()))
    assert "SubAgent_구제재생성" not in body["think_trace"]


# ── 프롬프트 역할 분리 ────────────────────────────────────────

def test_진단_프롬프트와_생성_프롬프트가_분리돼_있다():
    """★ 진단 역할에 생성 권한을 섞으면 진단 역할이 망가진다.

    SUB_AGENT_SYSTEM_PROMPT의 핵심 규칙은 "답변을 작성하지 마십시오"다.
    정상 흐름에서 진단만 해야 할 계층이 답변을 쓰기 시작하면,
    결정론적 계층이 확보한 재현성을 LLM 재량이 갉아먹는다.
    """
    from app.core.sub_agent import (SUB_AGENT_REWRITE_PROMPT,
                                    SUB_AGENT_SYSTEM_PROMPT)

    assert SUB_AGENT_SYSTEM_PROMPT != SUB_AGENT_REWRITE_PROMPT
    assert "답변을 작성하지 마십시오" in SUB_AGENT_SYSTEM_PROMPT
    # 생성 프롬프트는 숫자를 만들지 못하게 막아야 한다 (계산은 함수)
    assert "수치를 만들지 마십시오" in SUB_AGENT_REWRITE_PROMPT


def test_구제_프롬프트에_계산결과_화이트리스트가_실린다():
    """LLM이 숫자를 만들지 않게 하는 장치 — 쓸 수 있는 값을 열거해 준다."""
    from app.core.sub_agent import build_rewrite_payload

    payload = build_rewrite_payload(
        "질문", "반려된 답변",
        calc_results=[{"limit": 1200.0, "source": "doc39"}])
    assert "limit = 1200.0" in payload
    assert "이 값만 그대로 쓸 수 있습니다" in payload


def test_계산결과가_없으면_수치금지를_명시한다():
    from app.core.sub_agent import build_rewrite_payload

    payload = build_rewrite_payload("질문", "반려된 답변", calc_results=[])
    assert "어떤 수치도 새로 쓰지 마십시오" in payload


# ── 구제 성공은 '진전'이다 (불필요한 진단 호출 방지) ──────────
#
# detect_anomalies는 "재생성_기각이 있는데 재생성_반영이 없으면 루프"로 본다.
# 구제 재생성을 붙이면서 이 조건이 어긋났다 — L5' 재생성이 기각돼도
# Sub-Agent 구제가 채택됐으면 진전이 있는 것인데, 이상으로 잡혀 진단
# LLM 호출이 매번 한 번씩 더 붙었다(실측으로 발견).

def test_구제가_성공하면_진단을_부르지_않는다():
    """구제로 해소됐는데 '진전 없음'으로 잡으면 쓸데없이 호출이 는다."""
    c = _FakeClient()
    answer_question("RESCUE-6", _Q, client=c)

    assert "subagent_rewrite" in c.purposes
    assert "subagent_diagnosis" not in c.purposes, (
        "구제가 성공했는데도 진단이 호출됐다 — REGEN_NO_PROGRESS 오탐")


def test_구제가_실패하면_진단을_부른다():
    """반대편 — 정말로 진전이 없으면 진단은 돌아야 한다."""
    c = _FakeClient(audit_after_rescue=_AUDIT_REVISE)
    answer_question("RESCUE-7", _Q, client=c)

    assert "subagent_diagnosis" in c.purposes, (
        "끝내 해소되지 않았는데 진단이 돌지 않았다")


def test_구제_반영도_진전으로_인정한다():
    """detect_anomalies 단위 — trace 문구가 바뀌어도 이 불변식은 유지된다."""
    from app.core.sub_agent import detect_anomalies

    trace = ["[1ms] L6_재생성_기각 — 재생성 답변도 검증에 실패",
             "[2ms] SubAgent_구제_반영 — 다시 쓴 답변이 검증을 통과해 채택"]
    codes = [a.code for a in detect_anomalies(trace, "충분히 긴 정상 답변입니다." * 3)]
    assert "REGEN_NO_PROGRESS" not in codes


def test_구제도_기각되면_진전이_아니다():
    from app.core.sub_agent import detect_anomalies

    trace = ["[1ms] L6_재생성_기각 — 재생성 답변도 검증에 실패",
             "[2ms] SubAgent_구제_기각 — Sub-Agent 답변도 검증에 실패"]
    codes = [a.code for a in detect_anomalies(trace, "충분히 긴 정상 답변입니다." * 3)]
    assert "REGEN_NO_PROGRESS" in codes


def test_구제_생성은_감사보다_큰_토큰예산을_쓴다():
    """llm_call_adapter 기본값(800)은 감사용이다.

    그대로 답변 생성에 쓰면 문장이 중간에 잘린다.
    """
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    assert 'purpose="subagent_rewrite"' in src
    assert "max_tokens=1500" in src
