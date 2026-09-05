"""L4-sub · 불특정 질의 상담 답변 (HyperCLOVA X 위임).

━━ 무엇을 위한 계층인가 ━━
"나 몇 살인데 연금 계획 좀", "24살이고 현금 3,500만원 있는데 노후 대비를
어떻게 해야 할까요" 같은 질의는 계좌유형도 판매클래스도 수치도 없다.
예전에는 이런 질의가 조기 거절되거나, 통과해도 빈 계산 카드를 답으로
받았다. 계산할 값이 없으니 계산함수로는 답할 수 없는 것이 당연하다.

그런데 답할 수 없는 질의는 아니다. **지금 있는 근거로 답할 수 있는
만큼 답하고, 무엇이 더 필요한지 정리해 주는 것**이 옳다. 그 역할을
HCX에 위임하는 것이 이 계층이다.

━━ 반드시 지키는 것 ━━
① 근거자료를 검토한 뒤 **합리적 답변만** 추출한다. 근거에 없는 수치를
   지어내면 이후 검증 계층에서 걸린다 — 이 계층은 검증을 면제받지 않는다.
② 규칙과 조금이라도 충돌하면 **한계점을 명확히 밝히고**, 어떤 정보가
   더 필요한지 정확히 알려 준다.
③ 답변 형식은 L5'와 **동일하다.** 사용자에게는 같은 상담원이어야 한다.

━━ 검증은 면제되지 않는다 ━━
여기서 만든 답변도 수치 대조·인용 무결성·L6 감독을 똑같이 통과한다.
HCX가 지어낸 수치가 검증 없이 나가는 일은 없다.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.coverage_pipeline import EvidenceChunk, RequirementSlot

ADVISORY_SYSTEM_PROMPT = """당신은 연금 상담원입니다.

지금 받은 질문은 계좌 유형·판매 클래스·구체적 금액이 확인되지 않은
상태입니다. 그래도 **되돌려보내지 마십시오.** 지금 있는 근거로 답할 수
있는 만큼 답하고, 무엇을 더 알려주시면 더 정확해지는지 알려 드리는 것이
당신의 일입니다.

━━ 절대 규칙 ━━
1. 숫자를 새로 만들지 마십시오. [근거 문서]에 있는 수치만 쓸 수 있습니다.
   근거에 없는 금액·세율·한도를 쓰면 답변 전체가 폐기됩니다.
   확실하지 않으면 숫자를 쓰지 말고 "확인이 필요하다"고 쓰십시오.
2. 단정적으로 추천하지 마십시오. "가장 유리합니다", "추천드립니다",
   "무조건", "확실히"는 금지입니다. 조건을 나눠 설명하십시오.
3. 확인되지 않은 조건을 아는 것처럼 쓰지 마십시오.
   나이·소득·계좌 유형을 모르면 모른다고 전제하고 경우를 나누십시오.
4. 사용자가 밝힌 사정은 반드시 반영하십시오. 무시하고 일반론만
   늘어놓으면 안 됩니다. 다만 밝히지 않은 것을 지어내서도 안 됩니다.
5. 제공된 근거로 도저히 다룰 수 없는 주제라면, 억지로 답하지 말고
   그 사실을 밝히고 무엇을 확인해야 하는지 안내하십시오.
6. 마크다운 표기를 쓰지 마십시오. 답변은 상담 화면에 그대로 표시되는
   일반 텍스트입니다. "**굵게**"처럼 별표를 붙이지 말고, "#"으로 시작하는
   제목이나 "1." 번호 목록도 쓰지 마십시오.
7. 조건에 따라 값이 갈리면 **조건을 먼저** 밝히고 그다음에 값을 쓰십시오.
   값을 먼저 확정적으로 말해 놓고 뒤에서 다른 조건의 값을 덧붙이면,
   마치 두 값이 함께 적용되거나 하나가 예외인 것처럼 읽힙니다. 실제로는
   조건에 따라 **둘 중 하나만** 적용됩니다.
   틀린 예: "600만원 이내 13.2%가 적용됩니다. 다만 소득이 낮으면
            16.5%도 가능합니다."
   맞는 예: "총급여 5,500만원을 넘으면 13.2%, 그 이하면 16.5%가
            적용됩니다."

━━ 답변 구성 ━━
아래 세 가지를 **자연스럽게 이어지는 문장**으로 쓰십시오.
제목이나 대괄호로 구획을 나누지 마십시오.

  · 질문을 어떤 상황으로 이해했는지
  · 그 상황에서 지금 근거로 말할 수 있는 것 (조건이 갈리면 경우를 나눠서)
  · 무엇이 확인되지 않아 여기까지만 답했는지, 어떤 정보를 주시면 더
    구체적으로 답변드릴 수 있는지

예시 어조:
  "말씀해 주신 상황이라면 ○○부터 보시는 게 순서입니다. 자료 기준으로는
   ○○이고, ○○인 경우에는 ○○로 갈립니다. 다만 ○○를 알 수 없어
   여기까지만 말씀드릴 수 있고, ○○를 알려주시면 ○○까지 계산해
   드릴 수 있습니다."

한국어로, 상담원의 어조로 작성하십시오. 확인이 필요한 항목은 최대 2건만
꼽으십시오 — 많이 물으면 사용자가 답할 수 없습니다."""


def build_advisory_payload(query_spec: dict,
                           evidence: list[EvidenceChunk],
                           extra_conditions: Optional[dict] = None,
                           route_reason: str = "",
                           trap_context: Optional[dict] = None) -> str:
    """L4-sub 프롬프트 페이로드.

    계산 결과가 없는 것이 정상이므로 넣지 않는다. 대신 사용자가 밝힌
    사정(정규 조건 + 자유 조건)을 온전히 전달한다 — 그것이 이 계층이
    답할 수 있는 유일한 재료다.

    ⚠️ trap_context — 2026-09-03 추가. 예전에는 이 함수에 함정 교정
    (correction_notes)이 전혀 전달되지 않았다. L2 함정 감지는 경로와
    무관하게 돌므로 ADVISORY로 분류된 질의도 얼마든지 함정에 걸리는데,
    L4-sub는 그걸 모른 채 초안을 썼다. L6 감사가 결국 TRAP_UNADDRESSED로
    잡긴 하지만, 애초에 알려주지 않은 걸 잡는 것보다 처음부터 알려주는
    게 낫다 — L5'(build_supervisor_payload)와 같은 블록을 그대로 쓴다.
    """
    from app.analysis.conditions import describe_conditions
    from app.analysis.product_facts import render_facts_block

    parts = [f"[질문]\n{query_spec.get('query', '')}"]

    conditions = query_spec.get("user_conditions") or {}
    if desc := describe_conditions(conditions):
        parts.append(f"\n[확인된 조건]\n{desc}")

    extra = extra_conditions or query_spec.get("extra_conditions") or {}
    if extra:
        # 사용자가 분명히 말했는데 정규 스키마에 자리가 없던 것들이다.
        # 계산에는 못 쓰지만 무엇을 안내할지 정하는 데는 결정적이다.
        lines = "\n".join(f"· {k}: {v}" for k, v in extra.items())
        parts.append(f"\n[사용자가 밝힌 그 밖의 사정]\n{lines}")

    # L5'(build_supervisor_payload)와 **같은 블록을 같은 자리에** 싣는다.
    # ADVISORY 경로에만 팩트가 빠지면 "상품 하나 추천해 주세요" 같은
    # 상담형 질의에서 정작 위험등급·보수를 말하지 못한다 — F3에서 함정
    # 교정이 이 경로에만 빠져 있던 것과 정확히 같은 계열의 사고다.
    if facts_block := render_facts_block(
            query_spec.get("_product_facts") or []):
        parts.append(facts_block)

    if evidence:
        parts.append("\n[근거 문서 — 이 내용만 사실로 인용 가능]")
        for c in evidence[:6]:
            parts.append(f"---\n{c.text[:700]}")
    else:
        parts.append("\n[근거 문서]\n확보된 근거가 없습니다. "
                     "일반적인 제도 설명도 하지 마십시오 — 무엇을 확인해야 "
                     "답변드릴 수 있는지만 알려 드리십시오.")

    if trap_context and trap_context.get("correction_notes"):
        # L5'의 build_supervisor_payload와 같은 형식 — 판정 기준(verify_any)
        # 까지 함께 준다. 다른 말로 바꿔 쓰면 L6의 해소 판정을 못 만나
        # 계속 REVISE가 뜨는 것을 방지한다.
        parts.append("\n[주의할 혼동 — 항목마다 반드시 답변에 반영할 것]")
        for c in (trap_context.get("checks") or [])[:4]:
            note = c.get("correction") or c.get("title") or ""
            if not note:
                continue
            line = f"· {note}"
            if terms := c.get("verify_any"):
                line += f"\n  (다음 중 하나는 반드시 답변에 등장해야 함: {', '.join(terms[:4])})"
            parts.append(line)
        if not trap_context.get("checks"):      # checks가 없는 예전 호출 경로
            for note in trap_context["correction_notes"][:4]:
                parts.append(f"· {note}")

    if route_reason:
        parts.append(f"\n[이 경로로 온 이유]\n{route_reason}")

    return "\n".join(parts)


def make_generate_advisory(client=None,
                           extra_conditions: Optional[dict] = None,
                           route_reason: str = "",
                           trace_log: Optional[Callable[..., Any]] = None,
                           trap_context: Optional[dict] = None):
    """(query_spec, evidence, slots) -> str.

    ⚠️ 시그니처를 L5'의 generate_answer와 **일부러 똑같이** 맞췄다.
       파이프라인이 두 경로를 같은 자리에서 갈아 끼울 수 있어야
       이후 검증·인용·감독 계층이 한 벌로 유지된다. 경로마다 다른
       처리를 만들면 검증이 두 벌이 되고 반드시 어긋난다.
    """
    from app.llm.clova import USAGE, get_client
    c = client or get_client()

    def generate_advisory(query_spec: dict,
                          evidence: list[EvidenceChunk],
                          slots: list[RequirementSlot]) -> str:
        payload = build_advisory_payload(
            query_spec, evidence, extra_conditions, route_reason, trap_context)

        try:
            draft = c.call(ADVISORY_SYSTEM_PROMPT, payload,
                           purpose="l4sub_advisory", max_tokens=1400)
        except Exception as e:                                # noqa: BLE001
            if trace_log:
                trace_log("L4sub_호출_실패",
                          f"상담 답변 생성 실패({e}) → 결정론적 안내로 축퇴")
            draft = ""

        if not draft.strip():
            if trace_log:
                reason = ("mock 클라이언트" if getattr(c, "is_mock", False)
                          else "빈 응답")
                trace_log("L4sub_템플릿_축퇴",
                          f"상담 답변을 생성하지 못함({reason}) → "
                          f"확인 항목만 담은 결정론적 안내 사용")
            USAGE.record_degradation("l4sub_템플릿축퇴")
            return render_advisory_fallback(query_spec, evidence,
                                            extra_conditions, trap_context)
        return draft.strip()

    return generate_advisory


def render_advisory_fallback(query_spec: dict,
                            evidence: list[EvidenceChunk],
                            extra_conditions: Optional[dict] = None,
                            trap_context: Optional[dict] = None) -> str:
    """LLM 없이 만드는 상담 안내.

    예산 초과·호출 실패·mock에서 쓰인다. 지어낼 수 있는 것이 없으므로
    **무엇을 확인해야 하는지**만 정확히 말한다. 이것도 유효한 답변이다 —
    사용자는 다음에 무엇을 말해야 할지 알게 된다.

    trap_context가 있으면 L5'의 render_template_answer와 같은 방식으로
    "주의할 점" 문구를 덧붙인다 — LLM이 못 만든 답변이라도 함정 교정이
    아예 안 실리는 것보다는 낫다.
    """
    from app.analysis.conditions import describe_conditions

    conditions = query_spec.get("user_conditions") or {}
    extra = extra_conditions or query_spec.get("extra_conditions") or {}

    lines: list[str] = []
    known = describe_conditions(conditions)
    if known or extra:
        said = [known] if known else []
        said += [f"{k} {v}" for k, v in list(extra.items())[:4]]
        lines.append(f"말씀해 주신 내용은 {', '.join(said)}로 이해했습니다.")
    else:
        lines.append("문의하신 내용을 어떤 상황으로 볼지 확인이 필요합니다.")

    if evidence:
        lines.append(
            "제공 자료에서 관련 내용을 찾았지만, 연금 제도는 계좌 유형과 "
            "나이·소득에 따라 적용 기준이 갈려 지금 정보만으로는 금액을 "
            "확정해 드리기 어렵습니다.")
    else:
        lines.append(
            "다만 지금 주신 내용만으로는 제공 자료에서 뒷받침할 근거를 "
            "찾지 못했습니다.")

    lines.append(
        "다음 두 가지를 알려주시면 구체적인 금액까지 계산해 드릴 수 "
        "있습니다: 연금계좌 유형(연금저축 / IRP / DC), "
        "그리고 연간 납입 예정 금액 또는 총급여.")

    if trap_context and trap_context.get("correction_notes"):
        # "주의할 점"이라는 표현을 유지할 것 — L6 적합성 감사가 답변에
        # 교정 취지가 담겼는지를 이런 표지어로 확인한다(render_template_answer
        # 와 동일한 규약).
        for note in trap_context["correction_notes"][:2]:
            lines.append(f"주의할 점: {note}")

    return "\n\n".join(lines)
