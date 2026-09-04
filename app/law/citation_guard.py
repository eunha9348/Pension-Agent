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
from typing import Optional

from app.law.schema import (MIN_QUOTE_CHARS, MIN_SPAN_CHARS, CitationCheck,
                            LawArticle, LawConflict, LawJudgement,
                            normalize_for_match)
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


# ════════════════════════════════════════════════════════════════
# 답변–조문 저촉 검증 — 차단선이 두 겹이다
# ════════════════════════════════════════════════════════════════

def parse_law_conflicts(data: object) -> list[LawConflict]:
    """HCX 응답의 law_conflicts 배열을 구조체로. 형식 오류는 조용히 버린다."""
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("law_ref", "")).strip()
        quote = str(item.get("quote", "")).strip()
        span = str(item.get("answer_span", "")).strip()
        # 셋 중 하나라도 없으면 어차피 검증을 통과할 수 없다.
        if not (ref and quote and span):
            continue
        out.append(LawConflict(
            law_ref=ref, quote=quote, answer_span=span,
            conflict=str(item.get("conflict", "")).strip(),
        ))
    return out


def corpus_supports_span(span: str,
                         evidence_texts: list[str]) -> tuple[bool, str]:
    """이 답변 문장이 **제공 문서**로 뒷받침되는가. (뒷받침 여부, 사유)

    ━━ 왜 필요한가 (과제 안내 6페이지) ━━
    "기본 연금제도 자료에 한해 외부데이터 수집이 가능합니다. 단, **제공자료가
    최종근거이며, 외부정보는 보조로만 쓰고 상충 시 제공자료 우선**"

    법령은 법제처에서 수집한 **외부** 자료다. 따라서 조문과 제공 문서가
    어긋날 때 조문을 이유로 답변을 뜯어고치면 이 규칙을 정면으로 위반한다.
    제공 문서가 더 구체적이거나 다른 시점 기준일 수 있고, 무엇보다
    **평가의 최종 근거는 제공 자료**다.

    ━━ 판정을 코드가 한다 ━━
    프롬프트로 "제공 문서를 우선하라"고 적는 것만으로는 부족하다. 그건
    LLM 재량에 판정을 맡기는 것이고, 이 프로젝트의 "판단은 코드, 문장은
    LLM" 원칙에 어긋난다. 그래서 여기서 결정론적으로 거른다.

    ━━ 세 단계로 본다 (느슨한 쪽이 안전하다) ━━
    ① 정규화 후 근거에 그대로 있으면          → 뒷받침 (가장 강한 신호)
    ② 문장의 수치가 **전부** 근거에 있고
       도메인 용어도 겹치면                    → 뒷받침
    ③ 수치 주장이 없고 같은 주제의 근거가 있으면 → 뒷받침
       (수치가 없으면 조문과 '어긋난다'를 결정론적으로 말할 수 없다.
        그 판단은 의미 감사와 함정 규칙의 몫이다.)

    판정이 애매하면 **뒷받침으로 본다.** 저촉 오탐은 강제 재생성 + 강등을
    부르는데 결정론 계층의 판정은 되돌릴 수 없기 때문이다(단조성).
    """
    from app.analysis.vocab import domain_hits, key_terms
    from app.core.numeric_verifier import _matches, extract_numbers

    if not evidence_texts:
        # 근거가 하나도 없으면 뒷받침할 제공 문서 자체가 없다.
        # 이 경우는 "제공 문서와도 안 맞고 법령과도 안 맞는" 상태이므로
        # 저촉 판정을 통과시킨다 — 사용자가 지시한 바로 그 경우다.
        return False, "제공 근거가 0건이라 뒷받침할 문서가 없음"

    norm_span = normalize_for_match(span)
    norm_ev = [normalize_for_match(t) for t in evidence_texts]

    # ① 그대로 존재하는가
    for i, ev in enumerate(norm_ev):
        if norm_span and norm_span in ev:
            return True, "지목된 문장이 근거 문서에 그대로 존재"

    span_terms = domain_hits(key_terms(span))
    ev_terms = [domain_hits(key_terms(t)) for t in evidence_texts]

    # ② 수치 주장이 근거로 뒷받침되는가
    span_nums = extract_numbers(span)
    if span_nums:
        allowed: set[float] = set()
        for t in evidence_texts:
            allowed |= extract_numbers(t, include_trivial=True)
        ungrounded = [n for n in span_nums if not _matches(n, allowed)]
        if ungrounded:
            return False, (f"지목된 문장의 수치 {ungrounded} 가 제공 문서에 없음 "
                           f"— 제공 문서와도 어긋남")
        if any(span_terms & et for et in ev_terms):
            return True, "지목된 문장의 수치가 전부 근거 문서에 존재"
        return False, "수치는 근거에 있으나 같은 주제의 근거가 아님"

    # ③ 수치 주장이 없는 경우
    if any(len(span_terms & et) >= 2 for et in ev_terms):
        return True, ("수치 주장이 없고 같은 주제의 근거가 존재 — "
                      "조문과의 어긋남을 결정론적으로 판정할 수 없음")
    return False, "같은 주제의 근거 문서를 찾지 못함"


def verify_conflicts(store: LawStore,
                     answer: str,
                     conflicts: list[LawConflict],
                     evidence_texts: Optional[list[str]] = None,
                     ) -> tuple[list[LawConflict], list[str]]:
    """저촉 주장을 검증해 (통과분, 감사기록)을 돌려준다.

    ━━ 두 겹을 모두 통과해야 한다 ━━
      ① 조문 인용(quote)이 실재 조문에 **글자 그대로** 있는가
         — 지어낸 조문·의역한 조문을 막는다 (verify_citation 재사용)
      ② 지목한 답변 문장(answer_span)이 답변에 **글자 그대로** 있는가
         — 답변이 하지도 않은 말을 저촉이라고 지어내는 것을 막는다

    ②가 없으면 어떻게 되는가: 모델이 실재 조문을 정확히 인용해 놓고
    답변에 없는 주장을 지목할 수 있다. 그러면 멀쩡한 답변이 강제로
    재생성되고 강등 고지까지 붙는다. 결정론 계층의 판정은 LLM이 완화하지
    못하므로(단조성), 여기서의 오탐은 되돌릴 수 없다 — CLAUDE.md가 말하는
    "오탐이 미탐보다 나쁘다"가 정확히 이 자리다.

    폐기된 주장도 전부 기록에 남긴다. 조용히 버리면 "왜 저촉 판정이
    반영되지 않았는가"를 나중에 추적할 수 없다.
    """
    if store.is_empty:
        return [], ["법령 저장소가 비어 있어 답변–조문 저촉 검사를 수행하지 않음"]

    norm_answer = normalize_for_match(answer)
    kept: list[LawConflict] = []
    trace: list[str] = []

    for c in conflicts:
        # ① 조문 쪽
        check = verify_citation(store, c.law_ref, c.quote)
        c.check = check
        if not check.ok:
            c.reason = f"조문 인용 실패 — {check.reason}"
            trace.append(f"[저촉] 주장 폐기 — 근거 '{c.law_ref}' {check.reason}")
            log.warning("저촉 주장 폐기(조문): ref=%r 사유=%s",
                        c.law_ref, check.reason)
            continue

        # ② 답변 쪽
        span = normalize_for_match(c.answer_span)
        if len(span) < MIN_SPAN_CHARS:
            c.reason = (f"지목한 답변 문장이 너무 짧아 대조 불가 "
                        f"({len(span)}자 < {MIN_SPAN_CHARS}자)")
            trace.append(f"[저촉] 주장 폐기 — {c.reason}")
            continue
        if span not in norm_answer:
            c.reason = "지목한 문장이 답변에 그대로 존재하지 않음 (창작 의심)"
            trace.append(f"[저촉] 주장 폐기 — {c.reason}: {c.answer_span[:40]!r}")
            log.warning("저촉 주장 폐기(답변): span=%r", c.answer_span[:60])
            continue

        c.span_ok = True

        # ③ **제공 문서 우선** (과제 안내 6페이지)
        #
        # 법령은 외부 수집 자료이고 제공 문서가 최종 근거다. 지목된 답변
        # 문장이 제공 문서로 뒷받침된다면, 조문과 다르다는 이유로 그 답변을
        # 고치라고 할 수 없다 — 그건 외부 자료로 제공 자료를 뒤집는 것이다.
        #
        # 저촉을 채택하는 경우는 하나뿐이다:
        #   **제공 문서와도 맞지 않고 법령과도 맞지 않을 때.**
        supported, why = corpus_supports_span(c.answer_span,
                                              evidence_texts or [])
        if supported:
            c.verified = False
            c.reason = f"제공 문서 우선 — {why}"
            trace.append(
                f"[저촉] 주장 폐기(제공 문서 우선) — {check.article.ref}와 "
                f"어긋난다고 지목했으나 {why}. 외부 법령보다 제공 자료가 "
                f"최종 근거이므로 판정하지 않음")
            continue

        c.verified = True
        c.reason = f"조문·답변 양쪽 인용 확인 + 제공 문서 미뒷받침({why})"
        kept.append(c)
        trace.append(f"[저촉] 주장 채택 — {check.article.ref}: "
                     f"답변의 {c.answer_span[:40]!r} 부분 "
                     f"(제공 문서와도 어긋남: {why})")

    return kept, trace


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
