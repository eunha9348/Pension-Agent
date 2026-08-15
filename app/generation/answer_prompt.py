"""L5' Supervisor — 답변 생성.

━━ 역할 경계 ━━
LLM은 **문장만 만든다.** 숫자는 계산 결과와 근거 문서에서 온 것만 쓴다.
새 수치를 만들면 numeric_verifier가 잡아내고, 그 답변은 재생성되거나 축퇴된다.

━━ mock/실패 시 ━━
결정론적 템플릿(render_template_answer)이 같은 형식으로 답변을 만든다.
템플릿 답변은 계산 결과와 근거 스니펫만 조합하므로 환각이 원천적으로 없다.
문장이 딱딱한 대신 틀리지 않는다 — 이 대회 평가지표에서는 그쪽이 낫다.

━━ 출력 형식 (고정) ━━
    [확인된 조건] ...
    [조건별 결론] A 상황이면 ~, B 상황이면 ~
    [한계 고지] ...

근거 각주는 LLM이 아니라 citation_system이 붙인다.
문서 ID를 LLM에게 만들게 하면 없는 문서를 인용할 수 있기 때문이다.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.analysis.conditions import describe_conditions
from app.core.coverage_pipeline import EvidenceChunk, RequirementSlot, SlotStatus
from app.generation.render import render_calc_result

# 금지 표현 — 단정적 추천은 대회 요건 위반이자 부당권유 리스크
FORBIDDEN_EXPRESSIONS = [
    "가장 유리합니다", "가장 좋습니다", "추천드립니다", "추천합니다",
    "하시면 됩니다", "무조건", "반드시 이득", "확실히 유리",
]

SUPERVISOR_SYSTEM_PROMPT = """당신은 연금 상담 답변을 작성하는 상담원입니다.

━━ 절대 규칙 ━━
1. 숫자를 새로 만들지 마십시오. 제공된 [계산 결과]와 [근거 문서]에 있는
   수치만 쓸 수 있습니다. 두 곳에 없는 숫자를 쓰면 답변 전체가 폐기됩니다.
2. 단정적으로 추천하지 마십시오. 다음 표현은 금지입니다:
   "가장 유리합니다", "추천드립니다", "무조건", "확실히 유리합니다"
   대신 조건을 나눠 설명하십시오: "A 상황이면 ~, B 상황이면 ~"
3. 확인되지 않은 조건을 아는 것처럼 쓰지 마십시오.
   [확인이 필요한 항목]에 있는 것은 되물어야 할 사항입니다.
4. [주의할 혼동]에 적힌 내용이 있으면 반드시 답변에서 바로잡으십시오.
5. 문서 ID(doc39 등)를 본문에 쓰지 마십시오. 근거 각주는 시스템이 붙입니다.

━━ 출력 형식 (이 세 블록만, 이 순서로) ━━
[확인된 조건]
문의를 어떤 조건으로 이해했는지 한두 문장.

[조건별 결론]
계산 결과와 근거를 바탕으로 한 설명. 조건이 갈리면 경우를 나눠 서술.

[한계 고지]
확인이 필요한 항목, 제공 자료 밖 기준을 쓴 부분, 개인 사정에 따라
달라지는 부분. 없으면 "추가 확인이 필요한 사항은 없습니다."

한국어로, 상담원의 어조로 작성하십시오."""


# ════════════════════════════════════════════════════════════════
# 프롬프트 페이로드
# ════════════════════════════════════════════════════════════════

def build_supervisor_payload(query_spec: dict,
                             evidence: list[EvidenceChunk],
                             slots: list[RequirementSlot],
                             trap_context: Optional[dict] = None,
                             assumptions: Optional[list[str]] = None,
                             ask_back_items: Optional[list[str]] = None) -> str:
    parts = [f"[질문]\n{query_spec.get('query', '')}"]

    conditions = query_spec.get("user_conditions") or {}
    if desc := describe_conditions(conditions):
        parts.append(f"\n[확인된 사용자 조건]\n{desc}")

    calc_slots = [s for s in slots if s.status == SlotStatus.CALC_DONE]
    if calc_slots:
        parts.append("\n[계산 결과 — 이 수치만 사용 가능]")
        for s in calc_slots:
            parts.append(f"· {s.description}")
            parts.append(render_calc_result(s.calc_result))

    if evidence:
        parts.append("\n[근거 문서 — 이 내용만 사실로 인용 가능]")
        for c in evidence[:6]:
            parts.append(f"---\n{c.text[:700]}")

    if trap_context and trap_context.get("correction_notes"):
        parts.append("\n[주의할 혼동 — 답변에서 바로잡을 것]")
        for note in trap_context["correction_notes"][:4]:
            parts.append(f"· {note}")

    if ask_back_items:
        parts.append("\n[확인이 필요한 항목 — 단정하지 말고 되물을 것]")
        for item in ask_back_items[:2]:
            parts.append(f"· {item}")

    if assumptions:
        parts.append("\n[계산에 사용한 가정 — 한계 고지에 반드시 포함]")
        for a in assumptions[:4]:
            parts.append(f"· {a}")

    missing = [s.description for s in slots
               if s.required and s.status == SlotStatus.MISSING]
    if missing:
        parts.append("\n[근거를 찾지 못한 항목 — 답변하지 말고 한계로 고지]\n"
                     + ", ".join(missing))

    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════
# 결정론적 템플릿 (mock / LLM 실패 / 재생성 실패 시)
# ════════════════════════════════════════════════════════════════

def render_template_answer(query_spec: dict,
                           evidence: list[EvidenceChunk],
                           slots: list[RequirementSlot],
                           trap_context: Optional[dict] = None,
                           assumptions: Optional[list[str]] = None,
                           ask_back_items: Optional[list[str]] = None) -> str:
    """LLM 없이 같은 형식의 답변을 조립한다.

    계산 결과와 근거 문서 문장만 쓰므로 환각이 발생할 수 없다.
    """
    conditions = query_spec.get("user_conditions") or {}
    lines: list[str] = ["[확인된 조건]"]
    desc = describe_conditions(conditions)
    lines.append(f"문의를 다음 조건으로 이해했습니다: {desc}." if desc
                 else "질의에서 확인된 개인 조건이 없어 일반 기준으로 안내드립니다.")

    lines.append("\n[조건별 결론]")
    body_written = False

    for s in slots:
        if s.status == SlotStatus.CALC_DONE and s.calc_result is not None:
            lines.append(f"· {s.description}")
            lines.append(render_calc_result(s.calc_result, indent="    "))
            body_written = True

    for s in slots:
        if s.status == SlotStatus.COVERED and s.evidence_ids:
            from app.analysis.vocab import key_terms
            snippet = _evidence_snippet(evidence, s.evidence_ids,
                                        keywords=key_terms(s.description))
            if snippet:
                lines.append(f"· {s.description}: {snippet}")
                body_written = True

    if trap_context and trap_context.get("correction_notes"):
        for note in trap_context["correction_notes"][:2]:
            # "주의할 점"이라는 표현을 유지할 것 — L6 적합성 감사가 답변에
            # 교정 취지가 담겼는지를 이런 표지어로 확인한다(TRAP_UNADDRESSED).
            lines.append(f"· 주의할 점: {note}")
            body_written = True

    if not body_written:
        lines.append("제공 자료에서 이 질의를 뒷받침할 근거를 확인하지 못했습니다.")

    lines.append("\n[한계 고지]")
    limits: list[str] = []
    if ask_back_items:
        limits.append("다음을 확인해 주시면 더 정확히 안내드릴 수 있습니다 — "
                      + ", ".join(ask_back_items[:2]))
    if assumptions:
        limits.extend(assumptions[:3])
    missing = [s.description for s in slots
               if s.required and s.status == SlotStatus.MISSING]
    if missing:
        limits.append(", ".join(missing) + "은(는) 제공 자료로 확정하기 어렵습니다")
    if not limits:
        limits.append("추가 확인이 필요한 사항은 없습니다.")
    lines.extend(f"· {l}" for l in limits)

    return "\n".join(lines)


def _evidence_snippet(evidence: list[EvidenceChunk], doc_ids: list[str],
                      keywords: Optional[set[str]] = None,
                      width: int = 200) -> str:
    """근거 문서에서 짧게 인용. 원문을 변형하지 않는다.

    ⚠️ 앞에서부터 자르면 안 된다. 청크 앞부분이 다른 조항이면 정작 필요한
       내용(세율표, 한도 수치)이 잘려 나가, 근거를 인용하고도 답이 비는
       상황이 생긴다. 슬롯 핵심어가 처음 등장하는 지점을 중심으로 자른다.
    """
    for c in evidence:
        if c.doc_id not in doc_ids:
            continue
        text = " ".join(c.text.split())
        start = 0
        for kw in sorted(keywords or (), key=len, reverse=True):
            idx = text.find(kw)
            if idx > 0:
                start = max(0, idx - width // 4)
                break
        snippet = text[start:start + width]
        return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")
    return ""


# ════════════════════════════════════════════════════════════════
# 진입점
# ════════════════════════════════════════════════════════════════

def make_generate_answer(client=None,
                         trap_context: Optional[dict] = None,
                         assumptions: Optional[list[str]] = None,
                         ask_back_items: Optional[list[str]] = None,
                         trace_log: Optional[Callable[..., Any]] = None):
    """(query_spec, evidence, slots) -> str 시그니처의 함수를 만든다."""
    from app.llm.clova import get_client
    c = client or get_client()

    def generate_answer(query_spec: dict,
                        evidence: list[EvidenceChunk],
                        slots: list[RequirementSlot]) -> str:
        payload = build_supervisor_payload(
            query_spec, evidence, slots, trap_context, assumptions, ask_back_items)

        try:
            draft = c.call(SUPERVISOR_SYSTEM_PROMPT, payload,
                           purpose="l5_supervisor", max_tokens=1500)
        except Exception as e:
            if trace_log:
                trace_log("답변생성_LLM_실패",
                          f"L5' 호출 실패({e}) → 결정론적 템플릿으로 축퇴")
            draft = ""

        if not draft.strip():
            if trace_log:
                reason = ("mock 클라이언트" if getattr(c, "is_mock", False)
                          else "빈 응답")
                trace_log("답변생성_템플릿_축퇴",
                          f"L5'가 문장을 생성하지 못함({reason}) → "
                          f"계산 결과·근거만 조합한 템플릿 답변 사용")
            return render_template_answer(query_spec, evidence, slots,
                                          trap_context, assumptions, ask_back_items)
        return draft.strip()

    return generate_answer


def strip_forbidden(answer: str) -> tuple[str, list[str]]:
    """금지 표현을 조건부 표현으로 치환. 반환: (수정된 답변, 발견된 표현)

    L6 준법 감사가 잡아내지만, 생성 직후에 한 번 걸러두면
    재생성 횟수를 줄일 수 있다.
    """
    found = [p for p in FORBIDDEN_EXPRESSIONS if p in answer]
    out = answer
    for p in found:
        out = out.replace(p, "조건에 따라 달라집니다")
    return out, found
