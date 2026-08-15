"""verify_grounding — **새 검증 로직이 아니라 얇은 래퍼다.**

`build_answer()`의 원래 인터페이스는 이 자리에 신규 검증 함수를 기대하지만,
이미 그 역할을 하는 모듈이 두 개 있다:

  1. numeric_verifier.verify_numeric_grounding()  — 수치 대조 (결정론적)
  2. supervisory_board.supervise_hybrid()         — 의미 감사 (결정론적 + HCX)

그래서 여기서는 **두 함수를 순서대로 호출하고 결과를 bool로 감싸기만** 한다.
검증 로직을 여기에 새로 쓰면 같은 판단이 두 곳에 생겨 반드시 어긋난다.

━━ 순서가 중요하다 ━━
수치 대조를 먼저 한다. 수치가 근거에 없으면 의미 감사를 할 이유가 없고,
LLM 호출도 아낀다.

━━ 반환값 ━━
build_answer는 bool만 보지만, 파이프라인은 상세 결과가 필요하다.
그래서 bool 서브클래스에 detail을 달아 양쪽을 모두 만족시킨다.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.coverage_pipeline import EvidenceChunk, RequirementSlot, SlotStatus
from app.core.numeric_verifier import (verify_numeric_grounding,
                                       verify_source_disclosure)
from app.core.supervisory_board import SupervisionResult, Verdict, supervise_hybrid


class GroundingVerdict(int):
    """bool처럼 쓰이면서 상세 결과를 함께 실어 나르는 반환값."""

    def __new__(cls, ok: bool, numeric=None, supervision=None, disclosure=None):
        obj = super().__new__(cls, 1 if ok else 0)
        obj.numeric = numeric
        obj.supervision = supervision
        obj.disclosure = disclosure
        return obj

    def __bool__(self) -> bool:
        return int(self) == 1

    @property
    def verdict(self) -> Verdict:
        if self.supervision is not None:
            return self.supervision.verdict
        return Verdict.APPROVE if bool(self) else Verdict.REVISE

    def as_trace(self) -> str:
        lines = []
        if self.numeric is not None:
            lines.append(self.numeric.as_trace())
        if self.disclosure and not self.disclosure.get("ok"):
            lines.append(f"출처 고지 미흡 — {self.disclosure.get('action')}")
        if self.supervision is not None:
            lines.append(self.supervision.as_trace())
        return "\n".join(lines)


def make_verify_grounding(question: str,
                          slots: list[RequirementSlot],
                          llm_call: Optional[Callable[[str, str], str]] = None,
                          citations: Optional[list] = None,
                          user_conditions: Optional[dict] = None,
                          ask_back_items: Optional[list[str]] = None,
                          answerability: str = "ANSWER",
                          trap_ids: Optional[list[str]] = None,
                          mentioned_products: Optional[list[dict]] = None,
                          skip_semantic: bool = False):
    """(answer, evidence) -> GroundingVerdict 시그니처의 함수를 만든다."""

    def verify_grounding(answer: str, evidence: list[EvidenceChunk]) -> GroundingVerdict:
        calc_results = [s.calc_result for s in slots
                        if s.status == SlotStatus.CALC_DONE and s.calc_result is not None]
        evidence_texts = [c.text for c in evidence]

        # ── 1. 수치 대조 (LLM 없음) ──────────────────────────
        numeric = verify_numeric_grounding(answer, calc_results, evidence_texts)
        disclosure = verify_source_disclosure(answer, calc_results)

        # ── 2. 의미 감사 (결정론적 4대 감사 + HyperCLOVA X) ──
        supervision: Optional[SupervisionResult] = None
        if not skip_semantic and llm_call is not None:
            supervision = supervise_hybrid(
                answer=answer,
                question=question,
                llm_call=llm_call,
                evidence_texts=evidence_texts,
                calc_results=calc_results,
                citations=citations or [],
                user_conditions=user_conditions or {},
                mentioned_products=mentioned_products or [],
                ask_back_items=ask_back_items or [],
                answerability=answerability,
                trap_ids=trap_ids or [],
            )

        ok = bool(numeric.passed)
        if supervision is not None and supervision.verdict in (Verdict.REVISE,
                                                               Verdict.BLOCK):
            ok = False

        return GroundingVerdict(ok, numeric=numeric, supervision=supervision,
                                disclosure=disclosure)

    return verify_grounding
