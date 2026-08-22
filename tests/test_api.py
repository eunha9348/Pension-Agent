"""평가 API 계약 테스트.

평가는 세션 없는 단일 GET 요청이다. 스키마가 깨지거나 500이 나가면
그 문항은 그대로 0점이므로, **어떤 경우에도 5필드 200**을 검증한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import REQUIRED_FIELDS, app

client = TestClient(app)


def _get(question: str, qid: str = "Q-001"):
    r = client.get("/answer", params={"question_id": qid, "question": question})
    assert r.status_code == 200
    return r.json()


def test_5필드_스키마_준수():
    body = _get("연금저축 세액공제 한도가 얼마인가요?")
    assert set(body) == set(REQUIRED_FIELDS)
    assert all(isinstance(body[k], str) and body[k] for k in REQUIRED_FIELDS)


def test_question_id와_원문이_그대로_반환된다():
    q = "IRP에서 중도인출하면 세금이 어떻게 되나요?"
    body = _get(q, qid="Q-042")
    assert body["question_id"] == "Q-042"
    assert body["question"] == q


def test_거절_응답도_5필드를_지킨다():
    body = _get("비트코인 지금 사도 되나요?")
    assert set(body) == set(REQUIRED_FIELDS)
    # 근거가 없어도 retrieved_context를 비우지 않는다
    assert "근거 문서 없음" in body["retrieved_context"]
    assert body["think_trace"]


def test_빈_질의도_500을_내지_않는다():
    r = client.get("/answer", params={"question_id": "Q-000", "question": ""})
    assert r.status_code == 200
    assert set(r.json()) == set(REQUIRED_FIELDS)


def test_파이프라인이_터져도_스키마를_지킨다(monkeypatch):
    """예외가 새어 나가도 5필드 200이어야 한다."""
    import app.main as main_module

    def boom(*a, **kw):
        raise RuntimeError("의도적 실패")

    monkeypatch.setattr(main_module, "answer_question", boom)
    body = _get("연금 질문")
    assert set(body) == set(REQUIRED_FIELDS)
    assert "오류" in body["answer"]
    assert "의도적 실패" in body["think_trace"]      # 사유는 trace에 남는다


def test_think_trace에_실행계획과_판단과정이_들어간다():
    body = _get("1억이고 연금수령 1년차인데 얼마까지 인출할 수 있나요?")
    assert "[실행 계획]" in body["think_trace"]
    assert "[판단 과정]" in body["think_trace"]
    assert "L0_사전검색" in body["think_trace"]


def test_계산결과가_답변에_실제로_반영된다():
    """doc39 원문 예시: 1억 · 1년차 → 1,200만원"""
    body = _get("계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?")
    assert "1,200만원" in body["answer"]


def test_모든_답변에_근거_문서가_표시된다():
    for q in ["연금저축 세액공제 한도", "80세면 연금소득세율이 얼마인가요",
              "퇴직금 2억에 근속 25년이면 퇴직소득세는?"]:
        body = _get(q)
        assert ("근거 문서" in body["answer"]
                or "근거 문서 없음" in body["retrieved_context"]), q


def test_단정적_추천_표현이_답변에_없다():
    from app.generation.answer_prompt import FORBIDDEN_EXPRESSIONS
    for q in ["총보수가 가장 낮은 클래스로 추천해주세요",
              "연금저축이랑 IRP 중에 뭐가 나은가요?"]:
        body = _get(q)
        assert not [p for p in FORBIDDEN_EXPRESSIONS if p in body["answer"]], q


def test_health가_mock_여부를_드러낸다():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "is_mock" in body["llm"]
    assert body["calc_functions"] == 15
    assert "kind" in body["corpus"]


@pytest.mark.parametrize("q", [
    "연금저축이랑 IRP 합쳐서 세액공제 얼마나 되나요",
    "2013년 이전에 가입했고 4년 지났으면 연금수령연차가 몇 년차인가요",
    "연금 11년차인데 퇴직소득세 40% 감면 맞나요",
    "주택 사려고 중도인출하면 세금이 어떻게 되나요",
    "C-P 클래스는 IRP로 가입할 수 있나요",
    "매달 200만원씩 받으면 분리과세가 유리한가요",
])
def test_대표_질의가_전부_5필드로_응답된다(q):
    body = _get(q)
    assert set(body) == set(REQUIRED_FIELDS)
    assert len(body["answer"]) > 30


# ════════════════════════════════════════════════════════════════
# answer_question은 어떤 예외에도 5필드를 낸다
# ════════════════════════════════════════════════════════════════
#
# 실사고: 평가 API 5필드를 만든다는 약속이 문서에만 있고 코드로 강제되지
# 않았다. 예상 못 한 예외 하나가 요청 전체를 중단시켰다 — 단일 GET 요청
# 규격에서는 그게 곧 그 문항의 전체 실패다.

def test_예상치_못한_예외도_5필드로_축퇴한다(monkeypatch):
    import app.pipeline as p

    def _boom(*a, **k):
        raise ValueError("could not convert string to float: '**'")

    monkeypatch.setattr(p, "_answer_question_impl", _boom)
    out = p.answer_question("Q-1", "질문")
    for k in ("question_id", "question", "retrieved_context",
              "think_trace", "answer"):
        assert out.get(k), f"{k}가 비었다"
    assert out["question_id"] == "Q-1"
