"""CLOVA Studio 스모크 테스트.

    python -m app.llm.smoke_test

API 키를 넣은 직후 **가장 먼저** 이걸 돌리십시오. 하는 일:

  1. 실제 호출이 되는지 (인증 헤더 형식이 맞는지)
  2. 응답 JSON 구조가 파서와 맞는지 (원문 그대로 출력)
  3. 지연시간 측정 — 이 값이 있어야 단계별 타임아웃을 배분할 수 있다
  4. Function calling 응답 형태 확인

지연시간을 측정하기 전까지 app/pipeline.py의 타임아웃 예산은 **추정치**입니다.
"""

from __future__ import annotations

import json
import statistics
import sys
import time

from app.config import get_settings
from app.llm.clova import USAGE, ClovaClient, ClovaError, MockClovaClient

PROBE_SYSTEM = "당신은 연금 제도를 설명하는 상담원입니다. 한국어로 간결히 답하십시오."
PROBE_USER = "연금저축계좌의 연간 세액공제 대상 납입한도는 얼마입니까? 한 문장으로만 답하십시오."

PROBE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "extract_query_spec",
        "description": "연금 질의에서 요구사항과 조건을 추출한다",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "account_type": {"type": "string"},
            },
            "required": ["intent"],
        },
    },
}]


def main() -> int:
    s = get_settings()
    print("═" * 62)
    print(" CLOVA Studio 스모크 테스트")
    print("═" * 62)
    print(f" endpoint : {s.clova_endpoint}")
    print(f" LLM_MODE : {s.llm_mode}")
    print(f" API KEY  : {'설정됨 (' + s.clova_api_key[:6] + '…)' if s.clova_api_key else '★ 비어 있음 ★'}")
    print()

    if s.llm_is_mock:
        print("⚠️  현재 mock 모드입니다 — 실제 호출을 하지 않습니다.")
        print("   .env 의 CLOVA_API_KEY 를 채운 뒤 다시 실행하십시오.")
        print()
        client = MockClovaClient(s)
        print("mock 응답 확인:", repr(client.call(PROBE_SYSTEM, PROBE_USER))[:80])
        return 2

    try:
        client = ClovaClient(s)
    except ClovaError as e:
        print("❌ 클라이언트 생성 실패:", e)
        return 1

    # ── 1. 기본 호출 3회 (지연 분포 확인) ──────────────────
    latencies: list[float] = []
    for i in range(3):
        t0 = time.time()
        try:
            out = client.call(PROBE_SYSTEM, PROBE_USER, purpose="smoke", max_tokens=200)
        except ClovaError as e:
            print(f"❌ {i+1}회차 호출 실패: {e}")
            print("\n확인 순서:")
            print("  · 401/403 → 인증 헤더 형식. app/llm/clova.py _headers() 참고")
            print("    (Bearer 방식이 아니라 X-NCP-CLOVASTUDIO-API-KEY 일 수 있음)")
            print("  · 404     → CLOVA_ENDPOINT 의 모델 경로 확인")
            print("  · timeout → CLOVA_TIMEOUT_SEC 상향")
            return 1
        dt = time.time() - t0
        latencies.append(dt)
        print(f"  {i+1}회차 {dt*1000:7.0f}ms  →  {out[:60]!r}")

    p_med = statistics.median(latencies)
    p_max = max(latencies)
    print()
    print(f" 지연 중앙값 {p_med*1000:.0f}ms / 최대 {p_max*1000:.0f}ms")

    # ── 2. Function calling ────────────────────────────────
    print("\n[Function calling 응답 형태]")
    try:
        fc = client.call_with_functions(
            "질의를 분석해 함수를 호출하십시오.",
            "55세인데 IRP에서 연금 받으면 세금 얼마나 떼나요?",
            PROBE_TOOLS, purpose="smoke_fc")
        print(json.dumps(fc, ensure_ascii=False, indent=2)[:800])
        if fc.get("arguments") is None:
            print("⚠️ toolCalls 파싱 실패 — 응답 원문을 보고 clova.py의 "
                  "call_with_functions() 파싱부를 실제 스키마에 맞추십시오.")
    except ClovaError as e:
        print("❌ function calling 실패:", e)
        print("   → app/analysis/query_spec.py 는 결정론적 폴백이 있으므로")
        print("     이 기능이 안 되더라도 파이프라인은 계속 동작합니다.")

    # ── 3. 타임아웃 예산 제안 ───────────────────────────────
    print("\n" + "─" * 62)
    print(" 단계별 타임아웃 재배분 제안 (측정 지연 기준)")
    print("─" * 62)
    budget = p_max * 1.5
    print(f"  L1 질의분석   : {budget:4.1f}초")
    print(f"  L5' 답변생성  : {budget*1.6:4.1f}초   (출력 길이가 길다)")
    print(f"  L6 의미감사   : {budget:4.1f}초")
    print(f"  전체 상한     : {budget*4:4.1f}초")
    print("  → app/pipeline.py 의 TIMEOUT_BUDGET 상수를 이 값으로 갱신하십시오.")

    print("\n[사용량]", json.dumps(USAGE.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
