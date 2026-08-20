"""L1 function calling 400(40009 Unsupported function) 원인 이분 탐색.

    python -m scripts.diagnose_function_calling

━━ 왜 필요한가 ━━
평가 42건 전부에서 L1이 `HTTP 400 {"code":"40009","message":"Unsupported
function"}`으로 실패했다. 그래서 지금 점수는 **L1 없이 규칙 폴백만으로** 낸
것이다. 에러 메시지가 "무엇이" 지원되지 않는지 말해 주지 않으므로,
스키마를 단순한 것부터 한 겹씩 쌓아 올리며 **어디서 처음 깨지는지**를 찾는다.

무엇을 알아내는가
  · 이 엔드포인트/모델에서 function calling 자체가 되긴 하는가 (T1)
  · 어떤 스키마 요소가 거부되는가 (T2~T8: 배열·중첩객체·enum·중첩required)
  · 샘플링 파라미터나 toolChoice 형식이 문제인가 (P1~P4)

실제 API를 호출하므로 토큰을 조금 쓴다(테스트당 1회, 20회 남짓).
"""

from __future__ import annotations

import json
import sys

import httpx

from app.config import get_settings
from app.core.coverage_pipeline import CALC_REGISTRY
from app.llm.clova import ClovaClient

SYSTEM = "당신은 질의를 구조화하는 분석기입니다. 반드시 주어진 함수를 호출하십시오."
USER = "1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?"


def _tool(name: str, params: dict) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": "연금 질의를 분석해 구조화한다.",
            "parameters": params,
        },
    }]


# ── 스키마 사다리 ────────────────────────────────────────────
# 아래로 갈수록 한 가지씩 복잡해진다. 처음 실패하는 지점이 원인이다.

T1 = _tool("t1", {
    "type": "object",
    "properties": {"intent": {"type": "string", "description": "질의 의도"}},
    "required": ["intent"],
})

T2 = _tool("t2", {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "age": {"type": "integer"},
        "amount": {"type": "number"},
        "urgent": {"type": "boolean"},
    },
    "required": ["intent"],
})

T3 = _tool("t3", {        # 문자열 배열
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent"],
})

T4 = _tool("t4", {        # 중첩 객체 (깊이 2)
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "user_conditions": {
            "type": "object",
            "properties": {
                "account_value_manwon": {"type": "number"},
                "pension_year": {"type": "integer"},
            },
        },
    },
    "required": ["intent"],
})

T5 = _tool("t5", {        # 작은 enum
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "kind": {"type": "string", "enum": ["fact", "calculation", "comparison"]},
    },
    "required": ["intent"],
})

T6 = _tool("t6", {        # 큰 enum (계산함수 15종)
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "calc_function": {"type": "string", "enum": sorted(CALC_REGISTRY)},
    },
    "required": ["intent"],
})

T7 = _tool("t7", {        # 객체 배열 (items가 object)
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "asked_for": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    },
    "required": ["intent"],
})

T8 = _tool("t8", {        # 객체 배열 + items 안의 required
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "asked_for": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["id", "description"],
            },
        },
    },
    "required": ["intent"],
})

T9 = _tool("t9", {        # 객체 배열 + items 안의 enum (실제 스키마 구조)
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "asked_for": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string",
                             "enum": ["fact", "calculation", "comparison"]},
                    "calc_function": {"type": "string",
                                      "enum": sorted(CALC_REGISTRY)},
                },
                "required": ["id", "type"],
            },
        },
    },
    "required": ["intent", "asked_for"],
})


def _real_tool() -> list[dict]:
    from app.analysis.query_spec import QUERY_SPEC_TOOL
    return QUERY_SPEC_TOOL


SCHEMA_LADDER = [
    ("T1  최소 (문자열 1개)", T1),
    ("T2  스칼라 여러 개", T2),
    ("T3  + 문자열 배열", T3),
    ("T4  + 중첩 객체", T4),
    ("T5  + 작은 enum", T5),
    ("T6  + 큰 enum (15종)", T6),
    ("T7  + 객체 배열", T7),
    ("T8  + 객체배열 안 required", T8),
    ("T9  + 객체배열 안 enum", T9),
    ("T10 실제 QUERY_SPEC_TOOL", None),    # 지연 로드
]


def _client() -> "ClovaClient":
    """진단용 실클라이언트. 키가 없으면 여기서 깔끔하게 끝낸다.

    ClovaClient()는 키가 없으면 생성 시점에 예외를 던진다 — 진단 도구가
    raw traceback으로 죽으면 '무엇을 해야 하는지'가 안 보인다.
    """
    from app.llm.clova import ClovaError
    try:
        return ClovaClient()
    except ClovaError as e:
        print(f"\n❌ CLOVA 클라이언트를 만들 수 없습니다: {e}")
        print("   .env 의 CLOVA_API_KEY / CLOVA_ENDPOINT 를 확인하십시오.")
        raise SystemExit(1)


def _post(body: dict) -> tuple[bool, str]:
    """요청 1회. (성공여부, 요약) 반환."""
    c = _client()
    try:
        with httpx.Client(timeout=c.timeout) as client:
            resp = client.post(c.endpoint, headers=c._headers(), json=body)
    except Exception as e:      # noqa: BLE001
        return False, f"네트워크: {e}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    code = str((data.get("status") or {}).get("code", ""))
    if code and not code.startswith("2"):
        return False, f"status {code}: {(data.get('status') or {}).get('message')}"

    msg = (data.get("result") or {}).get("message") or {}
    calls = msg.get("toolCalls") or msg.get("tool_calls") or []
    if calls:
        fn = (calls[0].get("function") or {})
        return True, f"함수호출 OK — {fn.get('name')} {str(fn.get('arguments'))[:80]}"
    return True, f"200이지만 함수 미호출 (본문 {len(str(msg.get('content') or ''))}자)"


def _body(tools: list[dict], *, sampling: bool = True,
          tool_choice: object = "auto", temperature: float = 0.0) -> dict:
    b: dict = {
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": USER}],
        "tools": tools,
        "maxTokens": 1200,
    }
    if tool_choice is not None:
        b["toolChoice"] = tool_choice
    if sampling:
        b.update({"topP": 0.8, "topK": 0, "temperature": temperature,
                  "repetitionPenalty": 1.1, "stop": []})
    else:
        b["temperature"] = temperature
    return b


def main() -> int:
    print("═" * 66)
    print(" L1 function calling 진단")
    print("═" * 66)
    c = _client()
    print(f" endpoint : {c.endpoint}")
    print(f" 모델     : {get_settings().clova_endpoint.rsplit('/', 1)[-1]}")
    print()

    # ── 1단계: 스키마 사다리 ──────────────────────────────
    print("── 스키마 사다리 (파라미터는 현재 코드와 동일) " + "─" * 18)
    first_fail = None
    for label, tools in SCHEMA_LADDER:
        if tools is None:
            tools = _real_tool()
        ok, note = _post(_body(tools))
        print(f"  {'✅' if ok else '❌'} {label:<28} {note}")
        if not ok and first_fail is None:
            first_fail = label

    # ── 2단계: 요청 파라미터 변형 ─────────────────────────
    # 스키마가 아니라 파라미터 쪽이 원인일 수도 있다. 최소 스키마(T1)로
    # 고정한 뒤 파라미터만 흔들어 본다 — T1조차 실패했다면 여기서 갈린다.
    print()
    print("── 요청 파라미터 변형 (스키마는 T1 고정) " + "─" * 23)
    variants = [
        ("P1 샘플링 파라미터 제거", dict(sampling=False)),
        ("P2 toolChoice 생략", dict(tool_choice=None)),
        ("P3 toolChoice=none 아닌 명시 지정",
         dict(tool_choice={"type": "function",
                           "function": {"name": "t1"}})),
        ("P4 temperature 0.5", dict(temperature=0.5)),
        ("P5 최소 요청(샘플링·toolChoice 모두 제거)",
         dict(sampling=False, tool_choice=None)),
    ]
    for label, kw in variants:
        ok, note = _post(_body(T1, **kw))
        print(f"  {'✅' if ok else '❌'} {label:<28} {note}")

    # ── 2.5단계: enum 경계 정밀 확인 ──────────────────────
    # 1차에서 T6(최상위 큰 enum)은 깨졌는데 T9(배열 안 같은 enum)는 통과했다.
    # 개수 문제인지, 위치 문제인지, 그냥 들쭉날쭉한 건지 갈라 둔다.
    print()
    print("── enum 경계 (3회 반복해 재현성까지 확인) " + "─" * 21)
    ascii15 = [f"func_{i:02d}" for i in range(15)]
    ko = sorted(CALC_REGISTRY)
    enum_cases = [
        ("E1 최상위 ASCII 15개", ascii15),
        ("E2 최상위 한글 5개", ko[:5]),
        ("E3 최상위 한글 10개", ko[:10]),
        ("E4 최상위 한글 15개", ko),
    ]
    for label, values in enum_cases:
        tool = _tool("e", {
            "type": "object",
            "properties": {"intent": {"type": "string"},
                           "calc_function": {"type": "string", "enum": values}},
            "required": ["intent"],
        })
        marks = []
        for _ in range(3):
            ok, _note = _post(_body(tool))
            marks.append("✅" if ok else "❌")
        print(f"  {''.join(marks)} {label}")

    # ── 2.7단계: 수정된 스키마가 실제로 통과하는가 ────────
    print()
    print("── 수정본 검증 " + "─" * 49)
    ok, note = _post(_body(_real_tool()))
    print(f"  {'✅' if ok else '❌'} F1 수정된 QUERY_SPEC_TOOL   {note}")
    from app.analysis.query_spec import MINIMAL_QUERY_SPEC_TOOL
    ok2, note2 = _post(_body(MINIMAL_QUERY_SPEC_TOOL))
    print(f"  {'✅' if ok2 else '❌'} F2 축소 스키마(폴백)        {note2}")

    # ── 3단계: 대조군 — 일반 채팅은 되는가 ────────────────
    print()
    print("── 대조군 " + "─" * 54)
    ok, note = _post({
        "messages": [{"role": "user", "content": "안녕하세요"}],
        "maxTokens": 50, "temperature": 0.5,
    })
    print(f"  {'✅' if ok else '❌'} C1 tools 없는 일반 채팅       {note}")

    print()
    print("═" * 66)
    if first_fail:
        print(f" 처음 실패한 스키마 단계: {first_fail}")
        print(" → 그 단계에서 새로 추가된 요소가 원인입니다.")
    else:
        print(" 스키마 사다리는 전부 통과했습니다.")
        print(" → 스키마가 아니라 요청 파라미터 또는 간헐적 오류를 의심하십시오.")
    print("═" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
