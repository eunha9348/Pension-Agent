"""함정 규칙 테스트 — 감지 정확도 + 오탐 회귀.

━━ 오탐 회귀가 중요한 이유 ━━
과거 "정해지는"의 '해지'가 규칙에 걸리는 오탐이 있었다.
부분 문자열 매칭은 한국어에서 이런 사고를 반드시 낸다.
아래 참고 질의 5개는 **걸리면 안 되는** 질의다.
"""

from __future__ import annotations

import pytest

from app.core.trap_rules import TRAPS, build_trap_context, detect_traps

# 함정에 걸리면 안 되는 평범한 질의 5개 (오탐 회귀 세트)
CLEAN_QUERIES = [
    "기준가격은 어떻게 정해지는 건가요?",
    "연금 개시 시점은 언제로 정해지나요?",
    "펀드 수익률은 어떻게 산정되나요?",
    "환매는 며칠이나 걸리나요?",
    "가입자 교육은 언제 받아야 하나요?",
]


def test_규칙이_26종이다():
    assert len(TRAPS) == 26


def test_규칙_스키마가_온전하다():
    for r in TRAPS:
        assert r.id and r.title and r.trigger_keywords
        assert r.severity in ("critical", "high", "medium")
        assert r.fact, f"{r.id}: fact가 비어 있으면 교정 근거가 없다"


@pytest.mark.parametrize("q", CLEAN_QUERIES)
def test_오탐_회귀_평범한_질의는_걸리지_않는다(q):
    """'정해지는' → '해지' 오탐 재발 방지."""
    assert detect_traps(q) == [], f"오탐: {q} → {[r.id for r in detect_traps(q)]}"


@pytest.mark.parametrize("q,expected_id", [
    ("주택 사려고 중도인출하면 세금이 어떻게 되나요", "A1"),
    ("연금 11년째인데 퇴직소득세 40% 감면 맞나요", "B1"),
])
def test_대표_함정을_감지한다(q, expected_id):
    assert expected_id in [r.id for r in detect_traps(q)]


def test_감지되면_교정_문구가_제공된다():
    ctx = build_trap_context("주택 사려고 중도인출하면 세금이 어떻게 되나요")
    assert ctx["detected"]
    assert ctx["facts"]
    assert ctx["trace"]


def test_감지되지_않으면_빈_컨텍스트():
    ctx = build_trap_context("펀드 수익률은 어떻게 산정되나요?")
    assert ctx["detected"] == []
    assert ctx["critical_count"] == 0
    assert "없음" in ctx["trace"]


def test_trigger_all은_모든_단어가_있어야_확정된다():
    rules = [r for r in TRAPS if r.trigger_all]
    for r in rules:
        # trigger_keywords만 있고 trigger_all이 빠진 질의는 걸리지 않아야 한다
        partial = r.trigger_keywords[0]
        missing = [k for k in r.trigger_all if k not in partial]
        if missing:
            assert r.id not in [x.id for x in detect_traps(partial)]


def test_심각도_필터():
    critical = detect_traps("주택 사려고 중도인출하면 세금이 어떻게 되나요",
                            severity_filter="critical")
    assert all(r.severity == "critical" for r in critical)
