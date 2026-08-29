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
    # ⚠️ 구제 재생성이 해소한 경우는 진전이 **있는** 것이다.
    #    L5' 재생성이 기각돼도 Sub-Agent 구제가 채택됐으면 루프가 아니다.
    #    이 조건을 빼면 구제가 성공한 정상 흐름마다 이상으로 잡혀,
    #    쓸데없는 진단 LLM 호출이 한 번씩 더 붙는다(실측으로 확인).
    progressed = ("재생성_반영" in joined) or ("구제_반영" in joined)
    if "재생성_기각" in joined and not progressed:
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


# ════════════════════════════════════════════════════════════════
# 구제 재생성 — Sub-Agent가 직접 답변을 쓴다 (HyperCLOVA X)
# ════════════════════════════════════════════════════════════════
"""
━━ 왜 이 역할이 따로 있는가 ━━
L6가 REVISE를 내면 L5'가 1회 재생성한다. 그런데 그 재생성마저 검증에
걸리면 예전에는 **원본을 그대로 내보내고 고지문만 붙였다.** 감독이 두 번
반려한 문장이 그대로 나가는 셈이라, 고지를 붙여도 답변 자체는 나아지지
않았다.

그래서 마지막 한 번을 Sub-Agent에게 준다. L5'와 같은 자리에서 같은
프롬프트로 또 시도하면 같은 실패를 반복하기 쉬우므로, **지적사항을
정면으로 놓고 다시 쓰는** 다른 역할로 접근한다.

━━ 진단 역할과 반드시 분리한다 ━━
SUB_AGENT_SYSTEM_PROMPT는 "답변을 작성하지 마십시오"가 핵심 규칙이다.
거기에 생성 권한을 섞으면 진단 역할이 통째로 망가진다 — 정상 흐름에서
진단만 해야 할 계층이 답변을 쓰기 시작한다. 그래서 프롬프트도 함수도
따로 둔다.

━━ 감사를 우회하지 않는다 (핵심) ━━
이 답변도 **반드시 다시 검증을 거친다.** 통과하지 못하면 채택하지 않는다.
여기서 검증을 건너뛰면 Sub-Agent가 감사를 빠져나가는 뒷문이 되고,
"LLM 감사는 심각도를 올릴 수만 있다"는 단조성이 무너진다.
"""

SUB_AGENT_REWRITE_PROMPT = """당신은 연금 상담 답변을 **마지막으로 다시 쓰는** 작성자입니다.

앞서 작성된 답변이 내부 감사에서 반려됐고, 한 번의 수정 시도도 실패했습니다.
지적사항을 정면으로 반영해 답변을 처음부터 다시 쓰십시오.

━━ 반드시 지킬 것 ━━
1. **수치를 만들지 마십시오.** [계산 결과]에 주어진 값만 그대로 쓸 수 있습니다.
   주어지지 않은 금액·비율·연차를 새로 쓰면 그 답변은 폐기됩니다.
2. **근거 문서에 없는 사실을 쓰지 마십시오.** 사용자가 말하지 않은 조건을
   말한 것처럼 옮기지 마십시오.
3. **지적사항을 회피하지 말고 해소하십시오.** 문제가 된 문장을 지우기만 하면
   같은 지적이 다시 나옵니다.
4. **단정하지 마십시오.** 조건이 갈리면 "~인 경우에는 ~입니다" 형태로 나눠
   서술하고, 확인이 필요한 전제를 함께 밝히십시오.
5. 특정 상품을 추천하거나 "가장 유리하다"고 쓰지 마십시오.
6. 확인을 요청할 항목은 **최대 2건**입니다.

━━ 어떻게 쓰는가 ━━
· 사람에게 말하듯 자연스럽게 이어지는 문장으로 쓰십시오.
  대괄호 구획이나 번호 매긴 목차를 만들지 마십시오.
· 이해한 조건 → 결론 → 한계 순으로 자연스럽게 흐르게 하십시오.
· 정보가 모자라면 되돌려보내지 말고, 무엇까지 말할 수 있는지 밝힌 뒤
  무엇을 확인하면 되는지 정리해 주십시오.

답변 본문만 출력하십시오. 설명이나 머리말을 덧붙이지 마십시오."""


def build_rewrite_payload(question: str,
                          rejected_draft: str,
                          supervision=None,
                          calc_results: Optional[list[dict]] = None,
                          evidence_texts: Optional[list[str]] = None,
                          diagnosis: str = "") -> str:
    """구제 재생성 입력.

    진단 역할(build_sub_agent_payload)과 달리 **근거 문서와 계산 결과를 준다.**
    답변을 써야 하므로 재료가 필요하다. 다만 계산 결과는 '이 값만 쓸 수
    있다'는 화이트리스트로 제시한다 — 숫자를 만들지 못하게 하는 장치다.
    """
    parts = [f"[질문]\n{question}"]

    findings = list(getattr(supervision, "findings", []) or [])
    if findings:
        parts.append("\n[감사가 지적한 것 — 이것을 해소해야 합니다]")
        for f in findings:
            detail = (getattr(f, "directive", "") or
                      getattr(f, "detail", "") or "").strip()
            if detail:
                parts.append(f"· {detail}")

    if diagnosis:
        parts.append(f"\n[실행 로직 진단]\n{diagnosis}")

    if calc_results:
        parts.append("\n[계산 결과 — 이 값만 그대로 쓸 수 있습니다]")
        for r in calc_results:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    parts.append(f"· {k} = {v}")
    else:
        parts.append("\n[계산 결과]\n없습니다. 어떤 수치도 새로 쓰지 마십시오.")

    if evidence_texts:
        parts.append("\n[근거 문서 — 이 내용만 사실로 인용 가능]")
        for t in evidence_texts[:6]:
            parts.append(f"---\n{t[:700]}")
    else:
        parts.append("\n[근거 문서]\n확보된 근거가 없습니다. "
                     "제도 수치를 단정하지 말고, 무엇을 확인해야 답변드릴 수 "
                     "있는지 정리해 주십시오.")

    parts += ["\n[반려된 답변 — 참고만 하고 그대로 쓰지 마십시오]", rejected_draft]
    return "\n".join(parts)


def rescue_answer(question: str,
                  rejected_draft: str,
                  supervision=None,
                  calc_results: Optional[list[dict]] = None,
                  evidence_texts: Optional[list[str]] = None,
                  diagnosis: str = "",
                  llm_call: Optional[Callable[[str, str], str]] = None,
                  ) -> str:
    """Sub-Agent가 직접 다시 쓴 답변. 실패하면 빈 문자열.

    ⚠️ 호출자는 이 결과를 **반드시 다시 검증**해야 한다. 검증 없이 채택하면
       감사를 우회하는 경로가 생긴다.
    """
    if llm_call is None or not (rejected_draft or "").strip():
        return ""

    payload = build_rewrite_payload(question, rejected_draft, supervision,
                                    calc_results, evidence_texts, diagnosis)
    try:
        raw = llm_call(SUB_AGENT_REWRITE_PROMPT, payload)
    except Exception as e:                                    # noqa: BLE001
        log.warning("Sub-Agent 구제 재생성 실패: %s", e)
        return ""
    return (raw or "").strip()
