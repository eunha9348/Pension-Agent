"""Sub-Agent · 전 구간 로직 건전성 감독 테스트.

이 계층에서 가장 중요한 것은 **개입하지 않는 것**이다.
잘 도는 파이프라인을 들여다보고 고치려 들면, 결정론적 계층이 애써
확보한 재현성을 LLM 재량이 갉아먹는다.

그래서 이 파일은 '언제 개입하는가'보다 '언제 개입하지 않는가'를 더
촘촘히 못박는다.
"""

from __future__ import annotations

import json

import pytest

from app.core.sub_agent import (SEVERITY_CRITICAL, SEVERITY_DEGRADE,
                                SEVERITY_LOOP, SUB_AGENT_SYSTEM_PROMPT,
                                build_sub_agent_payload, detect_anomalies,
                                parse_sub_agent_response, supervise_logic)

_OK_TRACE = [
    "[1.0ms] L0_분류 — 영역 ['세제']",
    "[10.0ms] L1_질의분석 — 의도 '세액공제'",
    "[20.0ms] L3_정밀검색 — 후보 근거 5건 확보",
    "[30.0ms] L5'_답변생성 — 계산·근거 기반 경로로 초안 생성",
]
_OK_ANSWER = "연금저축 단독 한도는 600만원이고 IRP를 합치면 900만원까지 공제됩니다."


# ════════════════════════════════════════════════════════════════
# 개입하지 않는다 ★ — 이 계층의 첫째 규율
# ════════════════════════════════════════════════════════════════

def test_정상_실행에는_이상이_없다():
    assert detect_anomalies(_OK_TRACE, _OK_ANSWER) == []


def test_정상이면_LLM을_아예_호출하지_않는다():
    """잘 도는 것을 들여다보면 재현성이 깎인다."""
    calls = []

    def spy(system, user):
        calls.append(1)
        return "{}"

    r = supervise_logic(_OK_TRACE, _OK_ANSWER, llm_call=spy)
    assert r.healthy
    assert not r.intervened
    assert calls == [], "정상인데 LLM을 호출했다"


def test_예산이_없으면_감지만_하고_넘어간다():
    """보조 장치가 본체를 지연시키면 그 자체가 결함이다."""
    r = supervise_logic(["[1ms] x — y"], "", llm_call=None)
    assert r.anomalies                      # 감지는 한다
    assert not r.intervened                 # 개입은 안 한다
    assert any("예산" in t for t in r.trace)


def test_축퇴가_한두건이면_이상으로_보지_않는다():
    """폴백은 정상 동작이다. 연쇄일 때만 문제다."""
    trace = _OK_TRACE + ["[40ms] L1_예산초과 — 규칙 기반으로 진행"]
    codes = [a.code for a in detect_anomalies(trace, _OK_ANSWER)]
    assert "DEGRADE_CHAIN" not in codes


# ════════════════════════════════════════════════════════════════
# 개입한다 — 결정론적으로 판정된 경우만
# ════════════════════════════════════════════════════════════════

def test_빈_답변은_중대_오류다():
    a = detect_anomalies(_OK_TRACE, "")
    assert a and a[0].severity == SEVERITY_CRITICAL
    assert a[0].code == "EMPTY_ANSWER"


def test_지나치게_짧은_답변도_잡는다():
    a = detect_anomalies(_OK_TRACE, "네.")
    assert any(x.code == "TRUNCATED_ANSWER" for x in a)


def test_계층_예외를_잡는다():
    trace = _OK_TRACE + ["[50.0ms] L5_예외 — ValueError 발생"]
    a = detect_anomalies(trace, _OK_ANSWER)
    assert any(x.code == "STAGE_EXCEPTION" and x.severity == SEVERITY_CRITICAL
               for x in a)


def test_재생성_반복을_루프로_잡는다():
    a = detect_anomalies(_OK_TRACE, _OK_ANSWER, regeneration_count=2)
    assert any(x.code == "REGEN_REPEAT" and x.severity == SEVERITY_LOOP
               for x in a)


def test_진전없는_재생성을_잡는다():
    trace = _OK_TRACE + ["[60ms] L6_재생성_기각 — 재생성 답변도 검증 실패"]
    a = detect_anomalies(trace, _OK_ANSWER)
    assert any(x.code == "REGEN_NO_PROGRESS" for x in a)


def test_축퇴_연쇄를_잡는다():
    trace = _OK_TRACE + [
        "[40ms] L1_예산초과 — 규칙 기반으로 진행",
        "[50ms] L5'_템플릿_축퇴 — 빈 응답",
        "[60ms] 답변생성_LLM_실패 — 타임아웃",
    ]
    a = detect_anomalies(trace, _OK_ANSWER)
    assert any(x.code == "DEGRADE_CHAIN" and x.severity == SEVERITY_DEGRADE
               for x in a)


def test_판정_모순을_잡는다():
    class _S:
        class verdict:
            value = "BLOCK"

    a = detect_anomalies(_OK_TRACE, _OK_ANSWER, supervision=_S(),
                         answerability="ANSWER")
    assert any(x.code == "VERDICT_CONFLICT" for x in a)


# ════════════════════════════════════════════════════════════════
# DB 자료에 과잉 개입하지 않는다 ★
# ════════════════════════════════════════════════════════════════

def test_페이로드에_근거문서를_넣지_않는다():
    """자료를 주면 재해석하려 든다. 이 계층이 볼 것은 '무엇이 실행됐는가'다."""
    p = build_sub_agent_payload(detect_anomalies(_OK_TRACE, ""),
                                _OK_TRACE, "연금 질문")
    assert "실행 기록" in p
    assert "근거 문서" not in p


def test_프롬프트가_근거_재해석을_금지한다():
    assert "근거 문서를 재해석하지도 마십시오" in SUB_AGENT_SYSTEM_PROMPT
    assert "다시 고르라거나 다르게 해석하라고 하지 마십시오" in SUB_AGENT_SYSTEM_PROMPT


def test_프롬프트가_기본_로직_우선을_못박는다():
    assert "기본 로직이 우선입니다" in SUB_AGENT_SYSTEM_PROMPT
    assert "정상 동작을 바꾸라고 지시하지 마십시오" in SUB_AGENT_SYSTEM_PROMPT


def test_프롬프트가_답변_작성과_수치_생성을_금지한다():
    assert "답변을 작성하지 마십시오" in SUB_AGENT_SYSTEM_PROMPT
    assert "수치를 만들지 마십시오" in SUB_AGENT_SYSTEM_PROMPT


# ════════════════════════════════════════════════════════════════
# 개입 경로
# ════════════════════════════════════════════════════════════════

def test_진단을_받으면_기록한다():
    raw = json.dumps({"diagnosis": "L5'가 빈 응답을 냈다",
                      "directive": "템플릿 경로를 확인할 것",
                      "recoverable": True}, ensure_ascii=False)
    r = supervise_logic(_OK_TRACE, "", llm_call=lambda s, u: raw)
    assert r.intervened
    assert "빈 응답" in r.diagnosis
    assert "템플릿" in r.directive


def test_호출_실패는_조용히_넘어가지_않는다():
    def boom(s, u):
        raise RuntimeError("타임아웃")

    r = supervise_logic(_OK_TRACE, "", llm_call=boom)
    assert not r.intervened
    assert any("실패" in t for t in r.trace)


def test_해석_불가_응답은_감지결과만_남긴다():
    r = supervise_logic(_OK_TRACE, "", llm_call=lambda s, u: "그냥 텍스트")
    assert not r.intervened
    assert r.anomalies


def test_파싱():
    d, di, ok = parse_sub_agent_response(
        '{"diagnosis":"a","directive":"b","recoverable":true}')
    assert (d, di, ok) == ("a", "b", True)
    assert parse_sub_agent_response("깨진 응답") == ("", "", False)


# ════════════════════════════════════════════════════════════════
# 파이프라인 통합
# ════════════════════════════════════════════════════════════════

def test_trace_entries가_as_text와_같은_내용이다():
    """감지 대상과 사용자에게 보이는 기록이 어긋나면 안 된다."""
    from app.core.coverage_pipeline import TraceLogger

    t = TraceLogger()
    t.log("A", "첫째")
    t.log("B", "둘째")
    assert t.as_text() == "\n".join(t.entries())


@pytest.mark.parametrize("q", [
    "연금저축 세액공제 한도가 얼마인가요?",
    "나 몇살인데 연금 계획 좀",
])
def test_파이프라인이_건전성_기록을_남긴다(q):
    from app.pipeline import answer_question

    r = answer_question("SA", q)
    assert "SubAgent_건전성" in r["think_trace"]
