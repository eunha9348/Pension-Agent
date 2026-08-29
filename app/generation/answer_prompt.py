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
4. [주의할 혼동]은 참고 사항이 아니라 **반드시 반영해야 할 항목**입니다.
   적힌 항목마다, 그 내용에 등장하는 **구체적인 용어를 그대로 써서**
   답변에 한 문장 이상 포함하십시오.
   예: "법정 외 퇴직급여"라고 적혀 있으면 답변에도 "법정 외"라는 말이
       나와야 합니다. "주의가 필요합니다" 같은 일반적인 문장은 반영으로
       인정되지 않습니다.
   반영 여부는 항목별로 따로 검사하며, 하나라도 빠지면 답변이 반려됩니다.
5. 수치를 쓸 때는 **숫자 사이의 관계**도 정확해야 합니다.
   근거에 "A와 B 중 적은 금액"이라고 되어 있으면 "A 또는 B 중 선택"이라고
   쓰면 안 됩니다. 숫자가 맞아도 관계를 틀리면 잘못된 답변입니다.
6. 문서 ID(doc39 등)를 본문에 쓰지 마십시오. 근거 각주는 시스템이 붙입니다.
7. 이미 발생한 **퇴직소득**(명예퇴직수당·퇴직금 등)과 **새로 납입하는 금액**은
   적용 제도가 다릅니다. 전자는 이연퇴직소득세 감면, 후자는 연금계좌 세액공제
   입니다. 질문이 어느 쪽인지 확인하고, 다른 쪽 제도를 설명하지 마십시오.

━━ 답변 구성 — 이 세 가지를 이 순서로, 이어지는 문장으로 ━━
**대괄호 제목이나 번호로 구획을 나누지 마십시오.** 사람이 상담해 주듯
자연스럽게 이어 쓰되, 아래 세 가지가 빠지면 안 됩니다.

① 문의를 어떤 상황으로 이해했는지
② 그 상황에서 계산 결과와 근거로 말할 수 있는 것
   (조건이 갈리면 "A라면 ~, B라면 ~"으로 경우를 나눠서)
③ 무엇이 확인되지 않아 여기까지만 답했는지, 어떤 정보를 주시면 더
   구체적으로 답변드릴 수 있는지

이런 흐름으로 쓰십시오:

  "말씀하신 조건이라면 ○○입니다. 그렇게 판단한 근거는 ○○이고,
   ○○인 경우에는 ○○로 갈립니다. 다만 ○○를 알 수 없어 여기까지만
   말씀드릴 수 있고, ○○를 알려주시면 ○○까지 계산해 드릴 수 있습니다."

⚠️ ③을 "추가 확인이 필요한 사항은 없습니다" 같은 **완결 선언으로
   대신하지 마십시오.** 검색이 빗나갔는지 여부를 이 단계에서는 알 수
   없으므로, 근거가 엉뚱해도 확신에 찬 문장이 그대로 나갑니다. 실제로
   무관한 회사 연혁을 근거로 답하면서 이 문구를 붙인 사례가 있습니다.
   적을 것이 정말 없으면, 이 답변이 무엇을 근거로 한 일반 기준인지
   한 문장으로 밝히십시오.

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
        # 항목별로 어떤 용어가 답변에 나와야 하는지까지 알려 준다.
        # 예전에는 교정 문구만 줬더니 "주의가 필요합니다" 같은 일반 문장으로
        # 때우고 넘어갔고, 감사는 그걸 반영으로 인정했다(Q-002).
        checks = {c["id"]: c for c in (trap_context.get("checks") or [])}
        parts.append("\n[주의할 혼동 — 항목마다 반드시 답변에 반영할 것]")
        for c in (trap_context.get("checks") or [])[:4]:
            note = c.get("correction") or c.get("title") or ""
            if not note:
                continue
            line = f"· {note}"
            if terms := c.get("verify_any"):
                line += f"\n  (다음 중 하나는 반드시 답변에 등장해야 함: {', '.join(terms[:4])})"
            parts.append(line)
        if not checks:      # checks가 없는 예전 호출 경로
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
    # ⚠️ 대괄호 제목을 쓰지 않는다(2026-08-29). L5'가 사람처럼 이어지는
    #    문장으로 답하도록 바꿨는데, 축퇴 경로만 딱딱한 구획을 유지하면
    #    같은 시스템이 상황에 따라 다른 상담원처럼 보인다. 담기는 **항목은
    #    그대로**이고 구획 표시만 없앤다.
    conditions = query_spec.get("user_conditions") or {}
    lines: list[str] = []
    desc = describe_conditions(conditions)
    lines.append(f"문의를 다음 조건으로 이해했습니다: {desc}." if desc
                 else "질의에서 확인된 개인 조건이 없어 일반 기준으로 안내드립니다.")

    lines.append("")
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

    lines.append("")
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
        # ⚠️ "추가 확인이 필요한 사항은 없습니다"를 쓰면 안 된다.
        #    이 시점에서는 검색이 빗나갔는지 알 수 없다. 실제로 무관한 회사
        #    연혁을 근거로 답하면서 이 문구를 붙인 사례가 있다(300건 감사).
        #    완결을 선언하는 대신 무엇을 근거로 한 기준인지 밝힌다.
        limits.append("제공 자료 범위에서 확인한 일반 기준입니다. "
                      "개별 계좌·상품·가입 시점에 따라 달라질 수 있습니다")
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
    kws = set(keywords or ())

    def _relevance(c: EvidenceChunk) -> int:
        """슬롯 핵심어가 몇 개나 실제로 등장하는가.

        검색 순위 1위 청크가 그 슬롯의 최적 근거인 것은 아니다. BM25는 짧은
        청크를 선호하므로, 인용 문장은 핵심어가 실제로 많이 등장하는 쪽을 고른다.
        """
        return sum(1 for kw in kws if kw in c.text)

    # 순위(안정 정렬)를 유지하면서 관련도가 높은 청크를 앞으로
    ordered = sorted((c for c in evidence if c.doc_id in doc_ids),
                     key=_relevance, reverse=True)

    # ⚠️ 슬롯 핵심어가 **하나도** 없는 청크는 인용하지 않는다.
    #    이 인용문은 [조건별 결론] 자리에 그대로 들어간다. 검색이 빗나갔을 때
    #    무관한 원문이 답변 본문이 되어 나가는 경로가 여기다 — 실측 감사에서
    #    ESG 회사 연혁이 '국민연금 제도'의 근거로, 펀드 클래스 표가
    #    '주택연금 가입연령'의 근거로 실렸다.
    #    근거를 못 찾았다고 밝히는 편이 엉뚱한 원문을 내미는 것보다 낫다.
    if kws and ordered and _relevance(ordered[0]) == 0:
        return ""

    for c in ordered:
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
