"""CLOVA 클라이언트 계약 테스트.

실제 호출은 검증할 수 없으므로(네트워크 차단), 검증 대상은
"실연동이 안 되는 상황에서도 파이프라인이 규약대로 굴러가는가"이다.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.llm.clova import ClovaClient, ClovaError, MockClovaClient, _loads_lenient, llm_call_adapter

V3_ENDPOINT = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005"
V1_ENDPOINT = "https://clovastudio.stream.ntruss.com/testapp/v1/chat-completions/abc123"


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


# ── 인증 방식 자동 판별 (nv-* Bearer vs 구형 3-헤더) ─────────────

def test_nv_접두_키는_v3에서_Bearer_헤더를_쓴다():
    c = ClovaClient(Settings(clova_api_key="nv-abc123", clova_endpoint=V3_ENDPOINT))
    h = c._headers()
    assert h["Authorization"] == "Bearer nv-abc123"
    assert "X-NCP-CLOVASTUDIO-API-KEY" not in h


def test_구형_키는_v1_엔드포인트에서_전용_헤더를_쓴다():
    c = ClovaClient(Settings(clova_api_key="ncpXYZ", clova_endpoint=V1_ENDPOINT))
    h = c._headers()
    assert h["X-NCP-CLOVASTUDIO-API-KEY"] == "ncpXYZ"
    assert "Authorization" not in h


def test_구형_키에_APIGW_키가_있으면_함께_실린다():
    c = ClovaClient(Settings(clova_api_key="ncpXYZ", clova_apigw_key="gw-999",
                              clova_endpoint=V1_ENDPOINT))
    h = c._headers()
    assert h["X-NCP-APIGW-API-KEY"] == "gw-999"


def test_구형_키에_APIGW_키가_없으면_헤더도_없다():
    c = ClovaClient(Settings(clova_api_key="ncpXYZ", clova_endpoint=V1_ENDPOINT))
    h = c._headers()
    assert "X-NCP-APIGW-API-KEY" not in h


def test_구형_키로_v3_엔드포인트를_쓰면_기동_시점에_거절한다():
    """v3는 Bearer(nv-*) 전용이라, 구형 키로는 헤더를 어떻게 맞춰도 401이 난다.
    401을 받고 나서야 알아채지 않도록 생성 시점에 바로 막아야 한다."""
    with pytest.raises(ClovaError, match="nv-"):
        ClovaClient(Settings(clova_api_key="ncpXYZ", clova_endpoint=V3_ENDPOINT))


def test_nv_키는_v1_엔드포인트에서도_문제없이_생성된다():
    c = ClovaClient(Settings(clova_api_key="nv-abc123", clova_endpoint=V1_ENDPOINT))
    assert c._headers()["Authorization"] == "Bearer nv-abc123"


# ════════════════════════════════════════════════════════════════
# 429는 즉시 포기한다 — 대기 없는 재시도는 낭비다
# ════════════════════════════════════════════════════════════════
#
# 실사고: L6 예산은 3초뿐인데 _post()의 재시도는 대기 없이 즉시 재요청한다.
# 그러면 같은 429 윈도우에 또 걸릴 뿐이라 재시도가 시간만 태운다.
# (build_embeddings.py의 offline 배치용 429 백오프와는 성격이 다르다 —
#  거긴 몇 초씩 쉬어도 되고, 여긴 그럴 예산이 없다.)

def test_429는_재시도하지_않는다(monkeypatch):
    from app.llm.clova import ClovaClient

    calls = {"n": 0}

    class _Resp:
        status_code = 429
        text = '{"status":{"code":"42901","message":"Too many requests"}}'

    class _FakeHttpx:
        class Client:
            def __init__(self, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k):
                calls["n"] += 1
                return _Resp()

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    from app.config import Settings
    c = ClovaClient.__new__(ClovaClient)
    c.s = Settings(clova_api_key="nv-test")
    c.endpoint = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005"
    c.timeout = 5.0
    c.max_retry = 1

    import pytest
    from app.llm.clova import ClovaError
    with pytest.raises(ClovaError):
        c._post({}, "l6_semantic_audit")
    assert calls["n"] == 1, "429는 첫 실패에서 바로 포기해야 한다"


# ════════════════════════════════════════════════════════════════
# rate_limit_seen — 평가 루프가 429 페이싱을 조절하는 신호
# ════════════════════════════════════════════════════════════════
#
# 실사고: 평가 42문항을 텀 없이 쏘다가 E-15부터 끝까지 전부 429가 나서,
# 나머지 절반 이상이 LLM 없이 결정론적 폴백으로만 돌았다.
# embedding.py의 같은 이름 신호와 동일한 패턴을 clova.py에도 둔다.

def test_429를_겪으면_신호가_선다(monkeypatch):
    from app.llm.clova import ClovaClient, rate_limit_seen

    class _Resp:
        status_code = 429
        text = '{"status":{"code":"42901"}}'

    class _FakeHttpx:
        class Client:
            def __init__(self, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return _Resp()

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    from app.config import Settings
    c = ClovaClient.__new__(ClovaClient)
    c.s = Settings(clova_api_key="nv-test")
    c.endpoint = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005"
    c.timeout = 5.0
    c.max_retry = 1

    rate_limit_seen()      # 이전 상태 비우기
    import pytest
    from app.llm.clova import ClovaError
    with pytest.raises(ClovaError):
        c._post({}, "l1_query_spec")
    assert rate_limit_seen() is True


def test_429가_아니면_신호가_안_선다():
    from app.llm.clova import rate_limit_seen
    rate_limit_seen()   # 비우기
    assert rate_limit_seen() is False
