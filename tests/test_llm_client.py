"""CLOVA 클라이언트 계약 테스트.

실제 호출은 검증할 수 없으므로(네트워크 차단), 검증 대상은
"실연동이 안 되는 상황에서도 파이프라인이 규약대로 굴러가는가"이다.
"""

from __future__ import annotations

import json

from app.config import Settings
from app.llm.clova import MockClovaClient, _loads_lenient, llm_call_adapter


def test_mock_은_감사_호출에_APPROVE_JSON을_준다():
    c = MockClovaClient(Settings())
    raw = c.call("당신은 연금 상담 답변을 심사하는 감사자입니다.", "payload")
    data = json.loads(raw)
    assert data["verdict"] == "APPROVE"
    # 감사가 실제로 수행되지 않았다는 사실이 반드시 남아야 한다
    assert any("MOCK" in f["detail"] for f in data["findings"])


def test_mock_은_생성_호출에_빈문자열을_준다():
    """mock이 답변을 지어내면 실연동 시 문제가 드러나지 않는다.
    빈 문자열을 주고 호출 측이 결정론적 템플릿으로 축퇴하게 만든다."""
    c = MockClovaClient(Settings())
    assert c.call("당신은 연금 상담원입니다", "질문") == ""


def test_mock_function_calling은_None_arguments를_준다():
    c = MockClovaClient(Settings())
    out = c.call_with_functions("sys", "user", tools=[])
    assert out["arguments"] is None       # → 결정론적 규칙 추출기로 폴백


def test_llm_call_adapter_시그니처():
    call = llm_call_adapter(MockClovaClient(Settings()))
    assert isinstance(call("감사자입니다", "payload"), str)


def test_settings_llm_is_mock_판정():
    assert Settings(llm_mode="auto", clova_api_key="").llm_is_mock is True
    assert Settings(llm_mode="auto", clova_api_key="k").llm_is_mock is False
    assert Settings(llm_mode="mock", clova_api_key="k").llm_is_mock is True
    assert Settings(llm_mode="real", clova_api_key="").llm_is_mock is False


def test_json_회수_파서():
    assert _loads_lenient('```json\n{"a":1}\n```') == {"a": 1}
    assert _loads_lenient('설명입니다 {"a": 2} 끝') == {"a": 2}
    assert _loads_lenient("완전 텍스트") is None
