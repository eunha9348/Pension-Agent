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

import logging
from typing import Any, Callable, Optional

from app.core.coverage_pipeline import EvidenceChunk, RequirementSlot, SlotStatus
from app.core.numeric_verifier import (verify_calc_presence,
                                       verify_numeric_grounding,
                                       verify_source_disclosure)
from app.core.supervisory_board import (Finding, SupervisionResult, Verdict,
                                        supervise, supervise_hybrid)

log = logging.getLogger(__name__)


class GroundingVerdict(int):
    """bool처럼 쓰이면서 상세 결과를 함께 실어 나르는 반환값."""

    def __new__(cls, ok: bool, numeric=None, supervision=None, disclosure=None,
                presence=None):
        obj = super().__new__(cls, 1 if ok else 0)
        obj.numeric = numeric
        obj.supervision = supervision
        obj.disclosure = disclosure
        obj.presence = presence
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
        if self.presence is not None:
            lines.append(self.presence.as_trace())
        if self.disclosure and not self.disclosure.get("ok"):
            lines.append(f"출처 고지 미흡 — {self.disclosure.get('action')}")
        if self.supervision is not None:
            lines.append(self.supervision.as_trace())
        return "\n".join(lines)

    def revise_instruction(self) -> str:
        """재생성 시 L5'에 붙일 시정 지시. 없으면 빈 문자열."""
        if self.presence is not None and not self.presence.passed:
            return self.presence.instruction()
        return ""


def _law_context(trap_ids: Optional[list[str]],
                 trap_checks: Optional[list[dict]]
                 ) -> tuple[list, list[dict]]:
    """법령 근거가 등재된 함정만 골라 (조문 목록, 판정 대상) 을 만든다.

    법령 저장소가 비어 있거나(수집 전) 등재된 앵커가 없으면 빈 값을 돌려주고,
    감사는 지금까지와 똑같이 결정론적 경로로만 돈다. 즉 이 계층은
    **있으면 더 정확해지고 없으면 아무것도 망가뜨리지 않는다.**
    """
    if not trap_ids:
        return [], [], "감지된 함정이 없어 법령 판정 대상 없음"
    try:
        from app.law.anchors import anchors_for, law_backed
        from app.law.store import get_store

        store = get_store()
        if store.is_empty:
            return [], [], ("법령 수집본이 비어 있음 — 조문 판정을 수행하지 "
                            "않았습니다 (data/law 수집 필요)")

        by_id = {c.get("id"): c for c in (trap_checks or [])}
        articles, seen, candidates = [], set(), []
        for tid in trap_ids:
            if not law_backed(tid):
                continue
            arts = anchors_for(tid, store)
            if not arts:
                continue
            candidates.append({
                "id": tid,
                "title": by_id.get(tid, {}).get("title", ""),
                "verify_any": by_id.get(tid, {}).get("verify_any") or [],
            })
            for a in arts:
                if a.ref not in seen:
                    seen.add(a.ref)
                    articles.append(a)
        if candidates:
            return (articles, candidates,
                    f"함정 {[c['id'] for c in candidates]}에 대해 조문 "
                    f"{len(articles)}건으로 판정 수행")
        return [], [], (f"감지된 함정 {trap_ids} 중 법령 앵커가 등재된 것이 "
                        f"없어 조문 판정 대상 없음")
    except Exception as e:                                   # noqa: BLE001
        # 법령 계층이 없거나 깨져도 감사는 계속돼야 한다.
        # ⚠️ 다만 **조용히** 넘어가면 안 된다. 예전에는 log.warning만 남겨
        #    서버 로그에만 찍혔고, think_trace에는 아무 흔적도 없었다.
        #    그래서 "법령 판정이 안 된 것"과 "판정 대상이 없던 것"을
        #    사용자도 우리도 구별할 수 없었다 — 법령 계층이 통째로 죽은 채
        #    배포됐던 이력이 있는데도 겉으로는 정상으로 보였다.
        log.warning("법령 컨텍스트 구성 실패 — 결정론적 경로로 진행: %s", e)
        return [], [], f"법령 계층 오류로 조문 판정을 수행하지 못함: {e}"


def make_verify_grounding(question: str,
                          slots: list[RequirementSlot],
                          llm_call: Optional[Callable[[str, str], str]] = None,
                          citations: Optional[list] = None,
                          user_conditions: Optional[dict] = None,
                          ask_back_items: Optional[list[str]] = None,
                          answerability: str = "ANSWER",
                          trap_ids: Optional[list[str]] = None,
                          trap_checks: Optional[list[dict]] = None,
                          mentioned_products: Optional[list[dict]] = None,
                          partial_answer_possible: bool = False,
                          skip_semantic: bool = False):
    """(answer, evidence) -> GroundingVerdict 시그니처의 함수를 만든다."""

    def verify_grounding(answer: str, evidence: list[EvidenceChunk]) -> GroundingVerdict:
        calc_results = [s.calc_result for s in slots
                        if s.status == SlotStatus.CALC_DONE and s.calc_result is not None]
        evidence_texts = [c.text for c in evidence]

        # ── 1. 수치 대조 (LLM 없음) ──────────────────────────
        # 두 방향을 **모두** 본다. 한 방향만 보면 한쪽 사고를 놓친다:
        #   grounding : 답변의 수치 → 근거   (없는 숫자를 지어내는 것을 막음)
        #   presence  : 계산 결과 → 답변     (계산해 놓고 안 쓰는 것을 막음)
        # 후자가 없으면 "계산은 함수, 설명은 LLM" 원칙이 절반만 지켜진다.
        numeric = verify_numeric_grounding(answer, calc_results, evidence_texts,
                                           question=question)
        presence = verify_calc_presence(answer, calc_results)
        disclosure = verify_source_disclosure(answer, calc_results)

        # ── 2. 감독 (결정론적 4대 감사 + HyperCLOVA X 의미 감사) ──
        #
        # ⚠️ **결정론적 4대 감사는 LLM 가용성과 무관하게 언제나 돈다.**
        #    예전에는 llm_call이 없으면 supervise_hybrid 자체를 건너뛰어
        #    준법·이상치·적합성·부담 감사까지 통째로 사라졌다. 즉 예산이
        #    빠듯하거나 LLM이 죽었을 때 — 감독이 가장 필요한 순간에 —
        #    감독이 0이 됐다. 설계상 결정론적 계층이 1차 방어선이고
        #    LLM 감사는 '보완'이므로, 둘의 의존 방향이 정반대였다.
        det_kwargs = dict(
            calc_results=calc_results,
            citations=citations or [],
            user_conditions=user_conditions or {},
            mentioned_products=mentioned_products or [],
            ask_back_items=ask_back_items or [],
            answerability=answerability,
            trap_ids=trap_ids or [],
            trap_checks=trap_checks or [],
            partial_answer_possible=partial_answer_possible,
        )

        if llm_call is not None and not skip_semantic:
            # 감지된 함정 중 **법령 근거가 등재된 것**만 판정 대상으로 올린다.
            # 근거 조문이 페이로드에 없으면 어차피 인용 검증을 통과할 수
            # 없으므로, 등재되지 않은 규칙을 올려봐야 판정이 폐기될 뿐이다.
            law_articles, candidates, law_status = _law_context(
                trap_ids, trap_checks)
            supervision = supervise_hybrid(
                answer=answer, question=question, llm_call=llm_call,
                evidence_texts=evidence_texts,
                law_articles=law_articles, candidate_traps=candidates,
                **det_kwargs)
            # ⚠️ 법령 판정을 **하지 못한 경우에도** 그 사실을 남긴다.
            #    바로 위 의미감사 NOT_RUN과 같은 이유다 — "판정 대상이
            #    없었다"와 "판정을 못 했다"는 다른 사건인데, 예전에는 둘 다
            #    아무 흔적 없이 지나가 구별할 방법이 없었다.
            #    심각도는 올리지 않는다(정보 기록 전용).
            if not candidates:
                supervision.findings.append(Finding(
                    "법령근거", "NOT_RUN", supervision.verdict, law_status, ""))
        else:
            supervision = supervise(answer, **det_kwargs)
            # 감사자가 응답을 못 준 것과 '문제없음'은 다르다 —
            # 의미 감사가 수행되지 않았다는 사실을 반드시 남긴다.
            supervision.findings.append(Finding(
                "의미감사", "NOT_RUN", supervision.verdict,
                ("의미 감사를 수행하지 않음 — "
                 + ("호출자가 생략을 지정" if skip_semantic
                    else "LLM 호출 예산 없음")
                 + " (결정론적 4대 감사만 적용됨)"),
                ""))

        # ── 3. 계산값 누락은 REVISE로 올린다 ────────────────────
        #
        # 결정론적 검사가 찾은 결함이므로 심각도를 **올리기만** 한다
        # (감사 권한 계층의 단조성 — LLM이든 코드든 완화는 못 한다).
        # 여기서 REVISE로 올려야 파이프라인의 기존 재생성 경로가 그대로
        # 동작한다. 별도 분기를 만들면 재생성 로직이 두 벌이 된다.
        if supervision is not None and not presence.passed:
            supervision.findings.append(Finding(
                "수치표기", "CALC_NOT_SHOWN", Verdict.REVISE,
                presence.as_trace(), presence.instruction()))
            supervision.directives.append(presence.instruction())
            if supervision.verdict == Verdict.APPROVE:
                supervision.verdict = Verdict.REVISE

        ok = bool(numeric.passed) and bool(presence.passed)
        if supervision is not None and supervision.verdict in (Verdict.REVISE,
                                                               Verdict.BLOCK):
            ok = False

        return GroundingVerdict(ok, numeric=numeric, supervision=supervision,
                                disclosure=disclosure, presence=presence)

    return verify_grounding
