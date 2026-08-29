"""Sub-Agent · 전 구간 로직 건전성 감독 (HyperCLOVA X).

━━ 무엇을 하는 계층인가 ━━
L0부터 L6까지의 실행 흐름을 지켜보다가, **로직이 정상 궤도를 벗어났을 때만**
개입해 원인을 짚고 정상 tracing으로 되돌린다.

━━ 무엇을 하지 않는가 (더 중요하다) ━━
① **기본 로직이 우선이다.** 파이프라인이 정상적으로 돌고 있으면 이 계층은
   호출되지 않는다. 잘 도는 것을 굳이 들여다보고 고치려 들면, 결정론적
   계층이 애써 확보한 재현성을 LLM 재량이 갉아먹는다.
② **주어진 DB 자료에 과하게 개입하지 않는다.** 근거 문서를 재해석하거나
   다른 문서를 고르라고 지시하지 않는다. 그것은 L3·L4의 일이고, 이 계층이
   손대면 검색 결정이 실행마다 달라져 디버깅이 불가능해진다.
③ **답변 문장을 쓰지 않는다.** 진단과 시정 방향만 낸다.

━━ 언제 개입하는가 ━━
개입 조건은 **결정론적 코드가 판정한다**(detect_anomalies). LLM이
"내가 보기엔 이상한데"라고 나서는 구조가 아니다. 아래 셋 중 하나라도
잡혔을 때만 HCX를 부른다:

  · 중대 오류  — 계층이 예외로 죽었거나, 판정이 서로 모순되거나,
                 답변이 비어 있는 등 결과가 성립하지 않는 상태
  · 무의미 루프 — 재생성이 같은 지적으로 되돌아오는 등 진전 없는 반복
  · 축퇴 연쇄  — 여러 계층이 연달아 폴백으로 떨어져 실질 처리가 사라진 상태

━━ 예산 ━━
남은 시간이 없으면 호출하지 않고 진단만 기록한다. 이 계층 때문에
평가 응답이 늦어지는 일은 없어야 한다 — 보조 장치가 본체를 지연시키면
그 자체가 결함이다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# 개입할 만한 심각도. 이 미만은 기록만 하고 넘어간다.
SEVERITY_CRITICAL = "critical"
SEVERITY_LOOP = "loop"
SEVERITY_DEGRADE = "degrade"

# 축퇴가 이만큼 연달아 일어나면 실질 처리가 사라졌다고 본다.
_DEGRADE_THRESHOLD = 3

# trace 키에서 축퇴·실패를 나타내는 표지
_DEGRADE_MARKERS = ("축퇴", "예산초과", "실패", "폴백")


@dataclass
class Anomaly:
    """감지된 이상. 무엇이·왜 문제인지를 함께 담는다."""

    severity: str
    code: str
    detail: str

    def as_trace(self) -> str:
        return f"[{self.severity}/{self.code}] {self.detail}"


@dataclass
class SubAgentResult:
    """Sub-Agent 판정. 개입하지 않았으면 anomalies가 비어 있다."""

    anomalies: list[Anomaly] = field(default_factory=list)
    intervened: bool = False
    diagnosis: str = ""
    directive: str = ""
    trace: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.anomalies

    def as_trace(self) -> str:
        if self.healthy:
            return "로직 건전성 정상 — 개입 없음"
        lines = [a.as_trace() for a in self.anomalies]
        if self.intervened:
            lines.append(f"진단: {self.diagnosis}")
            if self.directive:
                lines.append(f"시정 방향: {self.directive}")
        else:
            lines.append("이상은 감지했으나 개입하지 않음 "
                         "(예산 부족 또는 개입 대상 아님)")
        lines.extend(self.trace)
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 이상 감지 — 결정론적
# ════════════════════════════════════════════════════════════════

def detect_anomalies(trace_entries: list[str],
                     answer: str,
                     supervision=None,
                     regeneration_count: int = 0,
                     answerability: str = "") -> list[Anomaly]:
    """실행 기록에서 이상을 찾는다. **LLM을 쓰지 않는다.**

    trace_entries : TraceLogger가 쌓은 줄들
    answer        : 최종 답변 초안
    supervision   : L6 SupervisionResult (있으면)
    regeneration_count : 재생성 시도 횟수

    ⚠️ 여기서 판정한 것만 개입 대상이 된다. LLM이 스스로 "이상한 것
       같다"고 나서는 경로는 없다 — 그러면 개입이 실행마다 달라진다.
    """
    found: list[Anomaly] = []
    joined = "\n".join(trace_entries)

    # ── 중대 오류: 답변이 성립하지 않는다 ──
    if not (answer or "").strip():
        found.append(Anomaly(SEVERITY_CRITICAL, "EMPTY_ANSWER",
                             "최종 답변이 비어 있다"))
    elif len((answer or "").strip()) < 20:
        found.append(Anomaly(SEVERITY_CRITICAL, "TRUNCATED_ANSWER",
                             f"최종 답변이 지나치게 짧다({len(answer.strip())}자)"))

    # ── 중대 오류: 계층이 예외로 죽었다 ──
    for m in re.finditer(r'^\[[\d.]+ms\]\s*(\S*(?:예외|크래시|CRASH)\S*)',
                         joined, re.M):
        found.append(Anomaly(SEVERITY_CRITICAL, "STAGE_EXCEPTION",
                             f"계층이 예외로 종료됨: {m.group(1)}"))

    # ── 무의미 루프: 재생성이 진전 없이 반복됐다 ──
    if regeneration_count >= 2:
        found.append(Anomaly(SEVERITY_LOOP, "REGEN_REPEAT",
                             f"재생성이 {regeneration_count}회 반복됨 "
                             f"(1회로 제한돼 있어야 한다)"))
    if "재생성_기각" in joined and "재생성_반영" not in joined:
        found.append(Anomaly(SEVERITY_LOOP, "REGEN_NO_PROGRESS",
                             "재생성 결과가 검증을 통과하지 못해 진전이 없다"))

    # ── 축퇴 연쇄: 실질 처리가 사라졌다 ──
    degraded = [ln for ln in trace_entries
                if any(mk in ln for mk in _DEGRADE_MARKERS)]
    if len(degraded) >= _DEGRADE_THRESHOLD:
        found.append(Anomaly(
            SEVERITY_DEGRADE, "DEGRADE_CHAIN",
            f"축퇴·실패가 {len(degraded)}건 연달아 발생해 실질 처리가 "
            f"사라졌을 수 있다"))

    # ── 판정 모순 ──
    if supervision is not None:
        verdict = getattr(getattr(supervision, "verdict", None), "value", "")
        if verdict == "BLOCK" and answerability == "ANSWER":
            found.append(Anomaly(
                SEVERITY_CRITICAL, "VERDICT_CONFLICT",
                "감독이 BLOCK인데 답변 등급이 ANSWER로 남아 있다"))

    return found


# ════════════════════════════════════════════════════════════════
# 개입 — HyperCLOVA X
# ════════════════════════════════════════════════════════════════

SUB_AGENT_SYSTEM_PROMPT = """당신은 연금 상담 시스템의 실행 로직을 점검하는 감독자입니다.

답변을 작성하지 마십시오. 근거 문서를 재해석하지도 마십시오.
**실행 흐름이 왜 정상 궤도를 벗어났는지**만 진단하십시오.

━━ 반드시 지킬 것 ━━
1. 기본 로직이 우선입니다. 정상 동작을 바꾸라고 지시하지 마십시오.
2. 검색된 근거 문서를 다시 고르라거나 다르게 해석하라고 하지 마십시오.
   그것은 다른 계층의 일이며, 당신이 손대면 실행마다 결과가 달라집니다.
3. 수치를 만들지 마십시오.
4. 진단이 서면 시정 방향은 **한 문장**으로만 쓰십시오.

━━ 무엇을 보는가 ━━
· 어느 계층에서 흐름이 끊겼는가
· 반복이 진전을 만들고 있는가, 아니면 같은 자리를 돌고 있는가
· 축퇴가 연달아 일어나 실질 처리가 사라지지 않았는가

반드시 아래 JSON 형식으로만 답하십시오.

{
  "diagnosis": "무엇이 어디서 어긋났는지 한두 문장",
  "directive": "시정 방향 한 문장 (없으면 빈 문자열)",
  "recoverable": true | false
}"""


def build_sub_agent_payload(anomalies: list[Anomaly],
                            trace_entries: list[str],
                            question: str) -> str:
    """진단용 입력. **근거 문서는 넣지 않는다.**

    자료를 주면 그것을 재해석하려 들기 때문이다. 이 계층이 볼 것은
    '무엇이 실행됐는가'이지 '무엇이 사실인가'가 아니다.
    """
    parts = [f"[질문]\n{question}"]
    parts.append("\n[감지된 이상]")
    parts.extend(f"· {a.as_trace()}" for a in anomalies)
    parts.append("\n[실행 기록]")
    # 뒤쪽이 사고 지점에 가깝다
    parts.extend(trace_entries[-25:])
    return "\n".join(parts)


def parse_sub_agent_response(raw: str) -> tuple[str, str, bool]:
    """(진단, 시정방향, 복구가능) — 파싱 실패는 빈 진단으로 처리."""
    from app.core.supervisory_board import _load_audit_json

    data = _load_audit_json(raw)
    if not data:
        return "", "", False
    return (str(data.get("diagnosis") or "").strip(),
            str(data.get("directive") or "").strip(),
            bool(data.get("recoverable", False)))


def supervise_logic(trace_entries: list[str],
                    answer: str,
                    question: str = "",
                    supervision=None,
                    regeneration_count: int = 0,
                    answerability: str = "",
                    llm_call: Optional[Callable[[str, str], str]] = None,
                    ) -> SubAgentResult:
    """전 구간 건전성 감독의 진입점.

    llm_call 이 None이면(예산 부족·mock) **감지만 하고 개입하지 않는다.**
    보조 장치가 본체를 지연시키면 그 자체가 결함이다.
    """
    anomalies = detect_anomalies(trace_entries, answer, supervision,
                                 regeneration_count, answerability)
    result = SubAgentResult(anomalies=anomalies)

    if not anomalies:
        return result                     # 정상 — 개입하지 않는다

    if llm_call is None:
        result.trace.append("개입 생략 — LLM 예산 없음 (감지 결과만 기록)")
        return result

    payload = build_sub_agent_payload(anomalies, trace_entries, question)
    try:
        raw = llm_call(SUB_AGENT_SYSTEM_PROMPT, payload)
    except Exception as e:                                    # noqa: BLE001
        log.warning("Sub-Agent 호출 실패: %s", e)
        result.trace.append(f"개입 실패({e}) — 감지 결과만 기록")
        return result

    diagnosis, directive, _ = parse_sub_agent_response(raw)
    if not diagnosis:
        result.trace.append("진단 응답을 해석하지 못함 — 감지 결과만 기록")
        return result

    result.intervened = True
    result.diagnosis = diagnosis
    result.directive = directive
    return result
