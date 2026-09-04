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


def _law_relevance(answer: str, question: str,
                   exclude_refs: set) -> tuple[list, str]:
    """답변과 관련된 조문을 **함정과 무관하게** 고른다. (조문, 사유)

    ━━ 왜 _law_context와 따로 두는가 ━━
    두 함수가 답하는 질문이 다르다.

      _law_context   : "감지된 함정의 근거 조문은 무엇인가"
                       → 함정이 없으면 대상도 없다. 게이팅이 옳다.
      _law_relevance : "이 답변이 어긋날 수 있는 조문은 무엇인가"
                       → 함정이 없어도 답변은 존재한다. 게이팅하면 안 된다.

    예전에는 앞의 것만 있었고, 그래서 함정이 안 잡힌 질의에서는 조문이
    한 줄도 실리지 않았다. 298건 실측으로 그 비율이 **55%**였다
    (함정 0건 49.0% + 앵커 미등재 6.0%). 그 상태에서는 답변이 법령에
    어긋나는 말을 해도 대조할 대상 자체가 없다.

    ⚠️ 여기서 고른 조문은 사람이 확인한 앵커가 아니라 용어 겹침으로 고른
       후보다. 신뢰도가 다르므로 앵커를 먼저 싣고 남는 자리만 채운다
       (exclude_refs로 중복을 막는다).
    """
    try:
        from app.law.relevance import select_relevant_articles
        return select_relevant_articles(answer, question,
                                        exclude_refs=frozenset(exclude_refs))
    except Exception as e:                                   # noqa: BLE001
        # 법령 계층이 없거나 깨져도 감사는 계속돼야 한다. 다만 조용히
        # 넘어가면 "고를 조문이 없었다"와 "고르지 못했다"가 구별되지 않는다.
        log.warning("관련 조문 선택 실패 — 앵커 조문만 사용: %s", e)
        return [], f"관련 조문 선택 중 오류: {e}"


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
                          skip_semantic: bool = False,
                          fact_texts: Optional[list[str]] = None):
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
        #
        # ⚠️ 검증이 쓰는 근거는 **사용자에게 제시된 근거와 같아야 한다.**
        #    (2026-09-04 실서버 확인) 예전에는 검색된 evidence 전체로 허용
        #    집합을 만들었는데, retrieved_context는 인용된 것(used_evidence)
        #    만 담는다. 두 집합이 어긋나 있어서 화면에는
        #    "근거 문서: 해당 없음"이 뜨는데 동시에 "감독 검증을 통과했습니다"
        #    가 떴다. 실물 코퍼스는 투자설명서라 숫자 밀도가 높아, LLM이
        #    지어낸 400만원·300만원·7년이 검색된 청크 어딘가에 우연히
        #    존재하기만 하면 통과했다.
        #    인용이 하나도 없다는 것은 **뒷받침할 근거를 제시하지 못했다**는
        #    뜻이므로, 그 상태에서 허용할 수 있는 수치는 계산 결과와 질의가
        #    준 값뿐이다. mock 코퍼스(7문서)는 우연 일치가 없어 이 결함이
        #    재현되지 않는다 — 실물에서만 보이는 계열이다.
        numeric_texts = evidence_texts if citations else []
        # ⚠️ 상품 팩트 스니펫은 citations 게이트 **밖**에서 더한다.
        #    F8이 막은 것은 "검색된 근거 뭉치 전체를 허용 집합으로 쓰는 것"
        #    이었다 — 투자설명서는 숫자 밀도가 높아 지어낸 값이 우연히
        #    걸리기 때문이다. 팩트 스니펫은 성격이 다르다:
        #      · 색인 시점에 **결정론적으로** 잘라 온 코퍼스 원문이고
        #      · 근거로 채택된 문서의 것만 들어오며(collect_facts)
        #      · 답변에 실제로 제시하는 바로 그 값이다.
        #    이걸 빼면, 검색이 마침 표 청크를 못 건졌다는 이유로 **우리가
        #    문서에서 확정한 위험등급·수익률이 '근거 없는 수치'로 잡혀**
        #    답변이 통째로 템플릿으로 축퇴한다.
        numeric_texts = numeric_texts + list(fact_texts or [])
        numeric = verify_numeric_grounding(answer, calc_results, numeric_texts,
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
            # 앵커 조문(사람이 확인) 뒤에 관련 조문(용어 겹침)을 덧붙인다.
            # 순서가 신뢰도 순이라 페이로드 상한에 걸려도 앵커가 먼저 남는다.
            rel_articles, rel_status = _law_relevance(
                answer, question, {a.ref for a in law_articles})
            law_articles = law_articles + rel_articles
            focus_terms: list[str] = []
            if rel_articles:
                from app.law.relevance import query_terms
                focus_terms = sorted(query_terms(answer, question))
            supervision = supervise_hybrid(
                answer=answer, question=question, llm_call=llm_call,
                evidence_texts=evidence_texts,
                law_articles=law_articles, candidate_traps=candidates,
                law_focus_terms=focus_terms,
                **det_kwargs)
            # ⚠️ 법령 판정을 **하지 못한 경우에도** 그 사실을 남긴다.
            #    바로 위 의미감사 NOT_RUN과 같은 이유다 — "판정 대상이
            #    없었다"와 "판정을 못 했다"는 다른 사건인데, 예전에는 둘 다
            #    아무 흔적 없이 지나가 구별할 방법이 없었다.
            #    심각도는 올리지 않는다(정보 기록 전용).
            if not candidates:
                supervision.findings.append(Finding(
                    "법령근거", "NOT_RUN", supervision.verdict, law_status, ""))
            # 저촉 검사 쪽도 같은 이유로 남긴다 — 조문이 하나도 안 실리면
            # 검사를 **할 수 없다**. 그 사실이 기록되지 않으면 "저촉 없음"과
            # 구별되지 않는다 (CLAUDE.md: 못 한 것과 대상이 없던 것을 구별할 것).
            if not law_articles:
                supervision.findings.append(Finding(
                    "법령저촉", "NOT_RUN", supervision.verdict,
                    f"대조할 조문이 없어 답변–조문 저촉 검사를 수행하지 못함 "
                    f"({rel_status})", ""))
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
