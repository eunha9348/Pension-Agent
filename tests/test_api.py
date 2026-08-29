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


# ════════════════════════════════════════════════════════════════
# /ui — 사람이 직접 써 보는 화면
# ════════════════════════════════════════════════════════════════
# 평가와 무관한 부가 경로다. 다만 두 가지를 반드시 지켜야 한다.
#   ① /answer 계약을 건드리지 않는다 (평가 규격은 변경 불가)
#   ② 외부 리소스를 불러오지 않는다 — 배포 환경에 아웃바운드가 없거나
#      CDN이 막혀 있으면 화면이 통째로 깨진다

def test_ui가_HTML을_반환한다():
    r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<title>연금 Agent</title>" in r.text


def test_ui는_외부_리소스를_불러오지_않는다():
    """CDN·외부 폰트에 의존하면 망 없는 환경에서 화면이 깨진다."""
    r = client.get("/ui")
    for marker in ("http://", "https://", "//cdn", "//unpkg", "//fonts."):
        assert marker not in r.text, f"외부 리소스 참조 발견: {marker}"


def test_ui_파일이_패키지에_포함돼_있다():
    """Dockerfile은 app/ 를 통째로 COPY한다 — 파일이 app/ 밖으로 나가면
    이미지에서 사라진다. 실제로 scripts/ 가 빠져 사고가 난 적이 있다."""
    from app.main import _UI_FILE

    assert _UI_FILE.exists(), f"UI 파일 없음: {_UI_FILE}"
    assert "app" in _UI_FILE.parts, "UI 파일이 app/ 밖에 있으면 이미지에 안 들어간다"


def test_ui를_추가해도_answer_계약은_그대로다():
    """부가 경로 때문에 평가 경로가 흔들리면 안 된다."""
    body = _get("연금수령한도가 얼마인가요?", qid="UI-001")
    assert set(body) == set(REQUIRED_FIELDS)
    assert body["question_id"] == "UI-001"


def test_root가_ui_경로를_안내한다():
    assert "/ui" in client.get("/").json()["endpoints"]


# ── 법령 계층 상태 노출 ────────────────────────────────────────
# 법령은 내부 검증 전용이라 답변에도 retrieved_context에도 안 나타난다.
# 그래서 배포 후 "반영이 됐는가"를 눈으로 확인할 방법이 없었다.
# 이 필드가 그 질문에 추측이 아니라 사실로 답한다.

def test_root가_법령_계층_상태를_보고한다():
    law = client.get("/").json()["law"]
    assert {"articles", "laws", "anchored_traps", "anchor_refs",
            "active"} <= set(law)
    assert isinstance(law["articles"], int)
    assert isinstance(law["active"], bool)


def test_등재된_앵커가_상태에_그대로_드러난다():
    """서버에서 이 값으로 배포 반영 여부를 판정한다."""
    from app.law.anchors import ANCHORS

    law = client.get("/").json()["law"]
    assert law["anchored_traps"] == sorted(ANCHORS)
    assert law["anchor_refs"] == sum(len(v) for v in ANCHORS.values())


def test_수집본이_없으면_active가_거짓이다():
    """수집 전에는 법령 판정이 꺼져 있어야 하고, 그 사실이 보여야 한다."""
    law = client.get("/").json()["law"]
    if law["articles"] == 0:
        assert law["active"] is False
    else:
        assert law["active"] is True


def test_법령_저장소가_터져도_상태조회는_살아남는다(monkeypatch):
    """법령 쪽 사고가 서비스 소개 엔드포인트를 죽이면 안 된다.

    손상된 저장소는 LawStore.load()가 RuntimeError를 던지도록 돼 있다
    (조용히 빈 것으로 넘기면 기능 실종을 눈치채지 못하므로). 그 예외가
    여기까지 올라와 / 를 500으로 만들면 안 된다.
    """
    import app.law.store as ls
    import app.main as m

    def boom(*a, **k):
        raise RuntimeError("법령 저장소 손상")

    monkeypatch.setattr(ls, "get_store", boom)
    status = m._law_status()
    assert status["active"] is False
    assert "error" in status
