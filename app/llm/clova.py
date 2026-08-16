"""CLOVA Studio (HyperCLOVA X) 클라이언트.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ⚠️  실연동 미검증 — 개발 컨테이너에서 CLOVA Studio 도메인이
     네트워크 정책으로 차단돼(CONNECT 403) 실제 호출을 한 번도
     성공시키지 못했습니다. 아래 요청/응답 형식은 공개 규격에 맞춰
     작성한 것이며, **API 키를 넣고 스모크 테스트로 반드시 검증**해야
     합니다:  python -m app.llm.smoke_test
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

설계 메모
---------
· 동기 호출. 평가 API가 단일 GET 요청 안에서 완결돼야 하므로 async를
  쓰더라도 이득이 없고, 타임아웃 관리만 복잡해진다.
· 재시도는 1회. 마감이 있는 대회 환경에서 지연 누적이 더 위험하다.
· 토큰 사용량은 호출마다 누적한다(크레딧 200,000원 한도 모니터링).
· Function calling / Structured Outputs / Thinking은 **동시 사용 불가**.
  이 클라이언트는 요청마다 하나만 지정한다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import SETTINGS, Settings

log = logging.getLogger("clova")


# ════════════════════════════════════════════════════════════════
# 사용량 집계
# ════════════════════════════════════════════════════════════════

@dataclass
class UsageTracker:
    """토큰 사용량 누적. 크레딧 한도 모니터링용."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0
    by_purpose: dict[str, int] = field(default_factory=dict)

    def record(self, purpose: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "failures": self.failures,
            "by_purpose": dict(self.by_purpose),
        }


USAGE = UsageTracker()


class ClovaError(RuntimeError):
    """CLOVA 호출 실패. 예외를 삼키지 않고 호출 측에 전달한다 —
    감사·검증 실패는 반드시 think_trace에 남아야 하기 때문."""


# ════════════════════════════════════════════════════════════════
# 실제 클라이언트
# ════════════════════════════════════════════════════════════════

class ClovaClient:
    """HyperCLOVA X 동기 클라이언트."""

    is_mock = False

    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or SETTINGS
        if not self.s.clova_api_key:
            raise ClovaError(
                "CLOVA_API_KEY가 비어 있습니다. .env 파일에 키를 넣으십시오."
            )
        self.endpoint = self.s.clova_endpoint
        self.timeout = self.s.clova_timeout_sec
        self.max_retry = self.s.clova_max_retry

    # ── 내부 ────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        h = {
            # CLOVA Studio 신규 인증(Bearer). 구형 콘솔 키를 쓰는 경우
            # X-NCP-CLOVASTUDIO-API-KEY 헤더로 바꿔야 할 수 있다 —
            # 스모크 테스트에서 401이 나오면 이 부분을 먼저 확인할 것.
            "Authorization": f"Bearer {self.s.clova_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.s.clova_request_id:
            h["X-NCP-CLOVASTUDIO-REQUEST-ID"] = self.s.clova_request_id
        return h

    def _post(self, body: dict, purpose: str) -> dict:
        import httpx

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retry + 1):
            t0 = time.time()
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(self.endpoint, headers=self._headers(),
                                       json=body)
                elapsed = round((time.time() - t0) * 1000)
                if resp.status_code != 200:
                    raise ClovaError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                status_code = str((data.get("status") or {}).get("code", ""))
                if status_code and not status_code.startswith("2"):
                    raise ClovaError(f"CLOVA status {status_code}: "
                                     f"{(data.get('status') or {}).get('message')}")
                result = data.get("result") or {}
                USAGE.record(purpose,
                             result.get("inputLength", 0),
                             result.get("outputLength", 0))
                log.info("[clova] %s ok %dms in=%s out=%s", purpose, elapsed,
                         result.get("inputLength"), result.get("outputLength"))
                return result
            except Exception as e:  # noqa: BLE001 — 재시도 후 그대로 올린다
                last_err = e
                USAGE.failures += 1
                log.warning("[clova] %s 시도 %d 실패: %s", purpose, attempt + 1, e)
        raise ClovaError(f"{purpose} 호출 실패: {last_err}")

    # ── 공개 인터페이스 ──────────────────────────────────────
    def call(self, system: str, user: str, purpose: str = "generic",
             max_tokens: int = 1200, temperature: float = 0.1,
             **kw) -> str:
        """기본 텍스트 호출. 반환값은 assistant 메시지 본문 문자열."""
        body = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "topP": kw.get("top_p", 0.8),
            "topK": kw.get("top_k", 0),
            "maxTokens": max_tokens,
            "temperature": temperature,
            "repeatPenalty": kw.get("repeat_penalty", 1.1),
            "stopBefore": kw.get("stop_before", []),
            "includeAiFilters": True,
            "seed": kw.get("seed", 0),   # 재현성 — 채점 리허설 시 유용
        }
        result = self._post(body, purpose)
        message = result.get("message") or {}
        return str(message.get("content") or "")

    def call_with_functions(self, system: str, user: str, tools: list[dict],
                            purpose: str = "function_call",
                            max_tokens: int = 1200) -> dict:
        """Function calling 호출.

        ⚠️ Function calling / Structured Outputs / Thinking은 동시 사용 불가.
           이 메서드는 tools만 지정한다.

        반환: {"arguments": dict | None, "name": str | None, "raw": str}
              모델이 함수를 부르지 않고 텍스트만 반환한 경우 arguments=None.
        """
        body = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "toolChoice": "auto",
            "maxTokens": max_tokens,
            "temperature": 0.0,      # 분석 단계는 흔들리면 안 된다
            "seed": 0,
        }
        result = self._post(body, purpose)
        message = result.get("message") or {}
        raw = str(message.get("content") or "")

        tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
        if tool_calls:
            first = tool_calls[0]
            fn = first.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            return {"name": fn.get("name"), "arguments": args, "raw": raw}

        # 함수 호출 없이 JSON 텍스트만 온 경우도 흡수
        parsed = _loads_lenient(raw)
        return {"name": None, "arguments": parsed, "raw": raw}


def _loads_lenient(text: str) -> Optional[dict]:
    """```json 펜스나 앞뒤 설명이 섞인 응답에서 JSON 객체를 회수."""
    import re
    t = (text or "").strip()
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', t).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', t, re.S)
        if not m:
            return None
        try:
            v = json.loads(m.group())
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None


# ════════════════════════════════════════════════════════════════
# Mock 클라이언트 — 실연동 전까지 파이프라인을 굴리기 위한 대역
# ════════════════════════════════════════════════════════════════

MOCK_BANNER = "[MOCK LLM] HyperCLOVA X 실연동 없이 결정론적 대역으로 동작 중"


class MockClovaClient:
    """네트워크·키 없이 인터페이스만 맞춘 대역.

    ━━ 설계 원칙 ━━
    **mock은 내용을 지어내지 않는다.**
    지어내면 파이프라인이 '동작하는 것처럼' 보여서 실연동 시 문제가
    드러나지 않는다. 대신 각 호출 지점에서 다음을 반환한다:

      · L1 질의 분석  → None (=분석 실패) → 결정론적 규칙 추출기가 대신 처리
      · L5' 답변 생성 → 빈 문자열 → 결정론적 템플릿 답변으로 축퇴
      · L6 의미 감사  → APPROVE + "감사 미수행" finding
                        (권한 계층상 APPROVE는 결정론적 판정을 완화하지 못하므로
                         안전하다. 감사가 실제로는 돌지 않았다는 사실만 남긴다.)
    """

    is_mock = True

    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or SETTINGS
        log.warning(MOCK_BANNER)

    def call(self, system: str, user: str, purpose: str = "generic", **kw) -> str:
        USAGE.record(f"mock:{purpose}", 0, 0)
        if "감사자" in system or "심사" in system:
            return json.dumps({
                "verdict": "APPROVE",
                "findings": [{
                    "code": "의미 정합성",
                    "detail": "MOCK 모드 — HyperCLOVA X 의미 감사가 실제로 수행되지 않음",
                    "directive": "",
                }],
                "most_critical_questions": [],
            }, ensure_ascii=False)
        # 생성 계열 호출은 빈 문자열 → 호출 측이 결정론적 템플릿으로 축퇴
        return ""

    def call_with_functions(self, system: str, user: str, tools: list[dict],
                            purpose: str = "function_call", **kw) -> dict:
        USAGE.record(f"mock:{purpose}", 0, 0)
        return {"name": None, "arguments": None, "raw": "",
                "mock": True}


# ════════════════════════════════════════════════════════════════
# 팩토리
# ════════════════════════════════════════════════════════════════

_CLIENT: Optional[Any] = None


def get_client(force_reload: bool = False):
    """설정에 따라 실제/mock 클라이언트를 반환 (프로세스 내 싱글턴).

    LLM_MODE=auto 에서 키가 없으면 mock으로 떨어진다 —
    키를 넣는 즉시 코드 수정 없이 실제 호출로 전환된다.
    """
    global _CLIENT
    if _CLIENT is not None and not force_reload:
        return _CLIENT

    from app.config import get_settings
    s = get_settings()

    if s.llm_is_mock:
        _CLIENT = MockClovaClient(s)
    else:
        _CLIENT = ClovaClient(s)
    return _CLIENT


def llm_call_adapter(client=None, audit_context: str = ""):
    """supervise_hybrid(llm_call=...)가 기대하는 (system, user) -> str 어댑터.

    audit_context : 감사자에게 함께 줄 도메인 유의사항(감지된 함정 사실 등).

    ━━ 왜 이 자리에서 붙이는가 ━━
    L6의 의미 감사는 "수치는 맞는데 설명이 틀린 경우"(연금수령연차 ↔
    연금실제수령연차 혼동 등)를 잡으라고 있는 계층이다. 그런데 감사자가
    받는 페이로드에는 **어떤 혼동을 조심해야 하는지가 들어 있지 않았다.**
    그러면 감사자는 자기 내부 지식에 의존하게 되는데, 이 도메인은 법 개정이
    잦아 모델 내부 지식을 신뢰할 수 없다는 것이 이 설계의 출발점이다.
    (그래서 L0 접지 계층을 따로 둔 것이다.)

    감사자에게 주는 것은 **판정 기준**이지 근거 문서가 아니므로,
    근거(evidence_texts)와 섞지 않고 별도 블록으로 붙인다.
    """
    c = client or get_client()

    def _call(system: str, user: str) -> str:
        payload = user
        if audit_context:
            payload = f"{user}\n\n[도메인 유의사항 — 이 관점에서도 점검할 것]\n{audit_context}"
        return c.call(system, payload, purpose="l6_semantic_audit", max_tokens=800)

    return _call
