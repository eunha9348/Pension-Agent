"""LLM 축퇴가 밖에서 보이는가 (2026-09-01).

━━ 왜 필요한가 ━━
이 시스템에는 **예외 없이 조용히** 결정론적 경로로 내려앉는 길이 셋 있다:

    L1이 JSON을 못 주면          → 규칙 기반 추출
    L5'가 빈 문장을 주면          → 템플릿 답변
    L4-sub가 빈 문장을 주면       → 결정론적 안내

그 상태로도 200 OK에 그럴듯한 답변이 나간다. 그런데 그 답변은
**HyperCLOVA X가 만든 것이 아니다** — 절대 제약 #1(답변 생성 LLM은
HyperCLOVA X만) 위반이고, 평가는 단일 GET이라 되돌릴 수 없다.

`UsageTracker.failures`는 **예외만** 센다. 위 셋은 예외가 아니므로
아무 데도 집계되지 않았고, 밖에서 확인할 방법이 없었다.
"""

from __future__ import annotations

import pytest

from app.llm.clova import UsageTracker


def test_축퇴_집계가_as_dict에_실린다():
    u = UsageTracker()
    assert u.as_dict()["degradation_total"] == 0
    u.record_degradation("l5_템플릿축퇴")
    u.record_degradation("l5_템플릿축퇴")
    u.record_degradation("l1_규칙축퇴")
    d = u.as_dict()
    assert d["degradation_total"] == 3
    assert d["degradations"]["l5_템플릿축퇴"] == 2


def test_예외_집계와_축퇴_집계는_별개다():
    """failures는 예외만 센다 — 빈 응답은 예외가 아니다."""
    u = UsageTracker()
    u.failures += 1
    u.record_degradation("l5_템플릿축퇴")
    assert u.as_dict()["failures"] == 1
    assert u.as_dict()["degradation_total"] == 1


@pytest.mark.parametrize("path,kind", [
    ("app/analysis/query_spec.py", "l1_규칙축퇴"),
    ("app/generation/answer_prompt.py", "l5_템플릿축퇴"),
    ("app/generation/advisory.py", "l4sub_템플릿축퇴"),
])
def test_세_축퇴_경로가_모두_집계된다(path, kind):
    """★ 배선 — 한 곳이라도 빠지면 그만큼이 보이지 않는다."""
    from pathlib import Path

    src = Path(path).read_text(encoding="utf-8")
    assert f'record_degradation("{kind}")' in src, \
        f"{path}의 축퇴가 집계되지 않는다"


def test_health가_축퇴를_노출한다():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        body = c.get("/health").json()
    assert "llm_usage" in body
    assert "degradation_total" in body["llm_usage"], \
        "/health에서 축퇴를 확인할 수 없다"


def test_compose가_운영에서_mock을_막는다():
    """★ LLM_MODE 기본값(auto)은 키가 없으면 조용히 mock으로 떨어진다.

    운영 서비스에서는 real로 못박아 그 경로 자체를 없앤다.
    """
    from pathlib import Path

    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert 'LLM_MODE: "real"' in compose, \
        "운영 서비스가 mock LLM으로 떨어질 수 있는 상태다"
    assert 'ALLOW_MOCK_CORPUS: "false"' in compose
