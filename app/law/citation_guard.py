"""인용 검증 — 이 시스템의 할루시네이션 차단선.

설계 의도
--------
HCX에게 "이 함정이 이 질의에 적용되는가"를 조문을 보고 판정하게 하되,
**판정마다 조문ID와 원문 인용을 함께 내도록 강제**한다. 그리고 코드가
그 인용이 실제 조문에 글자 그대로 있는지 대조한다.

    통과 → 판정 채택
    실패 → 판정 폐기 (기존 결정론적 판정만 남는다)

이 구조의 성질:
  · LLM이 조문을 지어내면 → 저장소 조회 실패 → 폐기
  · 조문은 맞는데 내용을 지어내면 → 문자열 대조 실패 → 폐기
  · 의역하면 → 대조 실패 → 폐기 (공백 외 정규화를 하지 않는 이유)

즉 **지어낸 근거는 구조적으로 답변에 도달할 수 없다.** 검증을 통과한
판정만이 남으므로, 남은 것은 전부 실재하는 조문에 뒷받침된다.

⚠️ 이 대조를 느슨하게 만들지 말 것. 인용을 어간 비교나 유사도로 바꾸면
   의역이 통과하고, 그 순간 이 모듈은 아무것도 막지 못한다.
   검증이 엄격해서 정탐을 놓치는 것은 안전한 실패(결정론적 판정으로
   돌아갈 뿐)지만, 느슨해서 오탐을 통과시키는 것은 안전하지 않다.
"""

from __future__ import annotations

import logging

from app.law.schema import (MIN_QUOTE_CHARS, CitationCheck, LawArticle,
                            LawJudgement, normalize_for_match)
from app.law.store import LawStore

log = logging.getLogger(__name__)


def verify_citation(store: LawStore, law_ref: str, quote: str) -> CitationCheck:
    """(조문참조, 인용문)이 실재하는지 대조한다."""
    ref = (law_ref or "").strip()
    q = (quote or "").strip()

    if not ref:
        return CitationCheck(False, "조문 참조가 비어 있음")
    if not q:
        return CitationCheck(False, "인용문이 비어 있음")

    norm_q = normalize_for_match(q)
    if len(norm_q) < MIN_QUOTE_CHARS:
        # 짧은 인용은 어느 조문에나 있어 대조가 통과해 버린다.
        return CitationCheck(
            False,
            f"인용문이 너무 짧아 대조 불가 ({len(norm_q)}자 < {MIN_QUOTE_CHARS}자)")

    candidates = store.get_candidates(ref)
    if not candidates:
        return CitationCheck(False, f"저장소에 없는 조문: {ref}")

    # 후보 중 **어느 하나**에 원문이 그대로 있으면 통과. 항 표기를 흘린
    # 인용을 살리기 위한 것이지 대조를 느슨하게 하는 게 아니다 —
    # 각 후보에 대한 대조는 여전히 글자 그대로다.
    for article in candidates:
        if article.contains_verbatim(q):
            return CitationCheck(True, f"조문 원문과 일치 ({len(norm_q)}자)",
                                 article=article, matched_quote=norm_q)

    return CitationCheck(
        False,
        "인용문이 조문 원문에 그대로 존재하지 않음 (의역·창작 의심)",
        article=candidates[0])


def verify_judgements(store: LawStore,
                      judgements: list[LawJudgement]
                      ) -> tuple[list[LawJudgement], list[str]]:
    """판정 목록을 검증해 (통과분, 감사기록)을 돌려준다.

    폐기된 판정도 전부 기록에 남긴다. 조용히 버리면 "왜 LLM 판정이
    반영되지 않았는가"를 나중에 추적할 수 없다 — 감사·검증 실패는
    반드시 think_trace에 남긴다는 원칙 그대로다.
    """
    if store.is_empty:
        return [], ["법령 저장소가 비어 있어 법령 근거 판정을 수행하지 않음"]

    kept: list[LawJudgement] = []
    trace: list[str] = []

    for j in judgements:
        check = verify_citation(store, j.law_ref, j.quote)
        j.check = check
        j.verified = check.ok
        if check.ok:
            kept.append(j)
            trace.append(
                f"[{j.trap_id}] 법령 판정 채택 (적용={j.applies}) "
                f"— {check.article.ref}: {check.reason}")
        else:
            trace.append(
                f"[{j.trap_id}] 법령 판정 폐기 — 근거 '{j.law_ref}' {check.reason}")
            log.warning("법령 판정 폐기: trap=%s ref=%r 사유=%s",
                        j.trap_id, j.law_ref, check.reason)

    return kept, trace


def parse_law_judgements(data: object) -> list[LawJudgement]:
    """HCX 응답의 law_judgements 배열을 구조체로. 형식 오류는 조용히 버린다.

    여기서 버려진 항목은 애초에 검증도 통과 못 한다 — 형식이 깨졌다는 건
    조문ID나 인용문이 없다는 뜻이기 때문이다.
    """
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("trap_id", "")).strip()
        if not tid:
            continue
        out.append(LawJudgement(
            trap_id=tid,
            applies=bool(item.get("applies", False)),
            law_ref=str(item.get("law_ref", "")).strip(),
            quote=str(item.get("quote", "")).strip(),
            rationale=str(item.get("rationale", "")).strip(),
        ))
    return out


def apply_to_traps(deterministic_ids: list[str],
                   kept: list[LawJudgement],
                   authority: str = "citation_verified"
                   ) -> tuple[list[str], list[str]]:
    """검증을 통과한 판정을 결정론적 함정 목록에 반영한다.

    authority="citation_verified" (기본):
        인용이 검증된 판정에 한해 **추가와 제거를 모두** 허용한다.
        이 권한은 "LLM을 믿는다"가 아니라 "실재하는 조문 원문을 믿는다"에
        가깝다 — 검증을 통과했다는 것은 조문에 그 근거가 실재한다는 뜻이다.

    authority="escalate_only":
        추가만 허용. 결정론적 판정을 무르지 못한다(단조성 유지).
        법령 수집이 불완전한 동안 쓰는 보수적 모드다.

    ⚠️ 두 모드 모두, 검증에 실패한 판정은 이 함수에 도달하지 않는다.
    """
    ids = list(deterministic_ids)
    trace: list[str] = []

    for j in kept:
        ref = j.check.article.ref if j.check and j.check.article else j.law_ref
        if j.applies and j.trap_id not in ids:
            ids.append(j.trap_id)
            trace.append(f"{j.trap_id} 추가 — 조문 근거 확인 ({ref})")
        elif not j.applies and j.trap_id in ids:
            if authority == "escalate_only":
                trace.append(
                    f"{j.trap_id} 제거 요청 기각 — 상향 전용 모드 (근거 {ref})")
                continue
            ids.remove(j.trap_id)
            trace.append(f"{j.trap_id} 제거 — 조문상 적용 대상 아님 ({ref})")

    return ids, trace
