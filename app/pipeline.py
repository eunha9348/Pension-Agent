"""6계층 파이프라인 통합.

    L0  사전 검색      grounding_retrieval + coarse_search      LLM 없음
    L1  질의 분석      query_spec (HyperCLOVA X + 규칙 폴백)
    1.5 계획 감사      supervise_plan                            LLM 없음
    L2  함정 감지      trap_rules (26종)                         LLM 없음
    L3  Exploration    retrieve_hybrid                           LLM 없음
    L4  Exploitation   구법탐지 → 엔티티충돌 → 가입자격 → 하드제약  LLM 없음
    L5  Prediction     CALC_REGISTRY 15종                        LLM 없음
    L5' Supervisor     answer_prompt (HyperCLOVA X)
    L6  감독 이사회    supervise_hybrid (결정론 + HyperCLOVA X)

━━ Exploration → Exploitation은 순차 ━━
병렬로 만들지 말 것. 자격 검증 없이 수치를 비교하면
"총보수 최저인데 가입 불가능한 상품"을 추천하게 된다.

━━ 타임아웃에 대하여 ━━
coverage_pipeline.run_with_timeout()은 ThreadPoolExecutor 기반이라
타임아웃이 나도 **스레드가 계속 돌아간다**(파이썬은 스레드를 죽일 수 없다).
그래서 본 경로에서는 쓰지 않는다.

실제로 느려질 수 있는 단계는 LLM HTTP 호출뿐이고, 그건 httpx 타임아웃으로
이미 경계가 있다. 여기서는 남은 예산(Deadline)을 추적해서, 예산을 넘겼으면
다음 LLM 단계를 **아예 건너뛰고** 결정론적 경로로 축퇴시킨다.
스레드를 억지로 죽이는 것보다 이쪽이 정직하고 안전하다.

단계별 예산(BUDGET_*)은 2026-08-18 실배포 로그의 실측 지연으로 갱신했다.
근거와 재조정 방법은 아래 상수 정의부의 주석을 볼 것.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.analysis.bridge import find_bridge
from app.analysis.calc_params import make_calc_params_builder
from app.analysis.conditions import describe_conditions
from app.analysis.products import extract_class_expenses
from app.analysis.query_spec import make_extract_query_spec
from app.analysis.refusal import check_refusal
from app.analysis.slot_matching import answer_covers_slot, make_slot_evidence_matcher
from app.config import SETTINGS
from app.core.citation_system import (attach_citations, build_citations,
                                      citations_to_retrieved_context,
                                      verify_citation_integrity)
from app.core.coverage_pipeline import (CALC_REGISTRY, Answerability,
                                        EvidenceChunk, RequirementSlot,
                                        SlotStatus, TraceLogger,
                                        decide_answerability,
                                        extract_required_slots,
                                        filter_irrelevant_evidence,
                                        map_evidence_to_slots, run_calculations,
                                        verify_requirement_coverage)
from app.core.numeric_verifier import verify_calc_presence
from app.core.grounding_retrieval import (build_refuse_response, ground_query,
                                          should_refuse_early)
from app.core.pension_calc_functions import (check_class_eligibility,
                                             detect_legacy_tax_content)
from app.core.supervisory_board import (Verdict, build_remediation_prompt,
                                        supervise_plan)
from app.core.trap_rules import build_trap_context
from app.generation.answer_prompt import (make_generate_answer,
                                          render_template_answer,
                                          strip_forbidden)
from app.generation.grounding import make_verify_grounding
from app.ingest.store import get_store
from app.llm.clova import MOCK_BANNER, get_client, llm_call_adapter
from app.retrieval.coarse import make_coarse_search
from app.retrieval.embedding import embedding_enabled
from app.retrieval.hybrid import make_retrieve_hybrid

# 전체 요청 예산(초). 평가 API는 단일 GET 안에서 끝나야 한다.
#
# 2026-08-18 실코퍼스(158문서) 대상 실배포 로그로 재갱신:
#   smoke_test의 짧은 프로브(554ms~844ms)와 달리, 근거 문서가 실린 실제
#   프롬프트(입력 3000토큰 안팎)는 L5'/L6가 5~8초씩 걸렸다. REVISE 재생성이
#   붙고 그 중 한 번이라도 타임아웃→재시도를 타면 L1+L5+L6+regen(L5)+L6
#   전체 경로가 30초를 넘길 수 있다(실측: 정상 완료 31160ms). 25초는 이
#   정상 경로조차 못 담아 조기에 잘라내므로 상향한다.
TOTAL_BUDGET_SEC = 45.0
# 단계별 예산 — 이 시점까지 남은 시간이 없으면 해당 LLM 단계를 건너뛴다.
# CLOVA_TIMEOUT_SEC(기본 15초)를 함께 올렸으므로, 정상적인 대형 프롬프트
# 호출(5~8초)이 타임아웃으로 오인돼 재시도되는 일을 줄이는 게 우선이다.
BUDGET_L1 = 7.0
BUDGET_L5 = 10.0
BUDGET_L6 = 3.0
BUDGET_REGEN = 10.0

# 세제 관련 질의에서 구법 문서를 근거로 쓰지 않기 위한 신호
_TAX_INTENTS = {"세액공제", "과세방식", "원천징수", "퇴직소득세", "퇴직소득세_감면"}


@dataclass
class Deadline:
    total: float = TOTAL_BUDGET_SEC
    started: float = field(default_factory=time.time)

    @property
    def remaining(self) -> float:
        return self.total - (time.time() - self.started)

    def allows(self, need: float) -> bool:
        return self.remaining >= need


# ════════════════════════════════════════════════════════════════
# L4 · Exploitation — 순차 필터
# ════════════════════════════════════════════════════════════════

def _exploit(evidence: list[EvidenceChunk], query_spec: dict,
             conditions: dict, trace: TraceLogger) -> tuple[list[EvidenceChunk], list[str]]:
    """구법탐지 → 엔티티충돌 → 가입자격 → 하드제약 (순차).

    반환: (통과한 근거, 하드제약 경고 문구)
    """
    warnings: list[str] = []
    is_tax_query = query_spec.get("intent") in _TAX_INTENTS

    # ── 1. 구법 탐지 ──
    kept: list[EvidenceChunk] = []
    dropped_legacy: list[str] = []
    for c in evidence:
        verdict = detect_legacy_tax_content(c.text, c.doc_id)
        if verdict["is_legacy_suspect"]:
            if is_tax_query:
                # 세제 질의에서 구법 수치는 환산으로 해소되지 않는 오답이다
                dropped_legacy.append(c.doc_id)
                continue
            c.entities = {**(c.entities or {}), "legacy": "true"}
        kept.append(c)
    if dropped_legacy:
        trace.log("L4_구법탐지",
                  f"세제 질의에 법 개정 이전 수치를 담은 문서 {len(dropped_legacy)}건을 "
                  f"근거에서 제외 (환산으로 해소되지 않는 오답이므로)",
                  excluded=dropped_legacy)
        warnings.append("검색 범위에 법 개정 이전 수치를 담은 문서가 있어 "
                        "현행 기준 문서만 근거로 사용했습니다")

    # ── 2. 엔티티 충돌 (+ 점수 임계값) ──
    # ⚠️ 폴백 대상은 반드시 '구법 필터를 통과한' after_legacy 여야 한다.
    #    원본 evidence로 되돌리면, 세제 질의에서 방금 제외한 구법 문서가
    #    그대로 되살아난다(함정 C5 재발). 완화해도 되는 건 점수 임계값뿐이고,
    #    구법 제외는 완화 대상이 아니다.
    after_legacy = list(kept)
    kept = filter_irrelevant_evidence(kept, query_spec, trace=trace)
    if not kept and after_legacy:
        kept = filter_irrelevant_evidence(
            after_legacy, query_spec, score_threshold=0.0, trace=None)[:3]
        trace.log("L4_필터_완화",
                  "필터 후 근거가 0건이 되어 점수 임계값만 낮춰 상위 3건 유지 "
                  "(구법 제외는 유지)")

    # ── 3. 가입자격 ──
    fund_class = conditions.get("fund_class")
    account_type = conditions.get("account_type")
    if fund_class and account_type:
        verdict = check_class_eligibility(fund_class, account_type)
        trace.log("L4_가입자격",
                  f"{fund_class} × {account_type} → "
                  f"{'가입 가능' if verdict['eligible'] else '가입 불가'}: {verdict['reason']}")
        if not verdict["eligible"]:
            warnings.append(verdict["reason"])

    # ── 4. 하드제약 ──
    warnings.extend(_hard_constraints(conditions, trace))
    return kept, warnings


def _hard_constraints(conditions: dict, trace: TraceLogger) -> list[str]:
    """수치를 계산하기 전에 걸러야 하는 제도적 제약."""
    out: list[str] = []

    age = conditions.get("age")
    if isinstance(age, int) and age < 55 and conditions.get("private_pension_monthly_manwon"):
        out.append("만 55세 미만은 연금수령 요건을 충족하지 않아, 인출 시 "
                   "연금소득세가 아닌 기타소득세가 적용됩니다")

    saving = conditions.get("pension_saving_manwon") or 0
    irp = conditions.get("irp_manwon") or 0
    if saving + irp > 1800:
        out.append("연금저축과 IRP 합산 연간 납입한도(1,800만원)를 초과했습니다")

    if out:
        trace.log("L4_하드제약", f"제도적 제약 {len(out)}건 감지", constraints=out)
    return out


# ════════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════════

def answer_question(question_id: str, question: str,
                    store=None, client=None) -> dict:
    """평가 API 5필드 응답을 만든다. 어떤 경우에도 예외를 밖으로 던지지 않는다."""
    deadline = Deadline()
    trace = TraceLogger()
    store = store or get_store()
    client = client or get_client()

    if getattr(client, "is_mock", False):
        trace.log("LLM_모드", MOCK_BANNER + " — L1/L5'/L6의 LLM 단계는 "
                                            "결정론적 경로로 대체됩니다")
    if store.corpus_kind == "mock":
        trace.log("코퍼스_모드",
                  "⚠️ 실제 제공 문서가 아닌 mock 코퍼스로 검색 중입니다 "
                  "(data/corpus/ 에 실물 zip을 넣고 재빌드하면 자동 교체)")

    # ── L0 · 사전 검색 ────────────────────────────────────────
    coarse_search = make_coarse_search(store)
    grounding = ground_query(question, coarse_search,
                             legacy_checker=detect_legacy_tax_content)
    trace.log("L0_사전검색", grounding.trace,
              areas=grounding.domain_areas, coverage=grounding.coverage_score)

    early_refuse, reason = should_refuse_early(grounding)
    pre_refusal = check_refusal(question, grounding)
    if early_refuse or pre_refusal.refuse:
        detail = pre_refusal.detail if pre_refusal.refuse else grounding.trace
        why = pre_refusal.reason if pre_refusal.refuse else reason
        trace.log("조기_거절",
                  f"{detail} — LLM 호출 없이 종결 (호출 0회)")
        resp = build_refuse_response(question_id, question, why)
        resp["think_trace"] = trace.as_text() + "\n" + resp["think_trace"]
        return resp

    # ── L1 · 질의 분석 ────────────────────────────────────────
    extract_query_spec = make_extract_query_spec(
        client=client,
        grounding_hint=grounding.as_analysis_hint(),   # 원문 아닌 영역·용어만
        trace_log=trace.log)
    if deadline.allows(BUDGET_L1):
        query_spec = extract_query_spec(question)
    else:
        from app.analysis.query_spec import rule_based_spec
        query_spec = rule_based_spec(question)
        trace.log("L1_예산초과", "남은 예산 부족 → 규칙 기반 질의 분석으로 진행")

    trace.log("L1_질의분석",
              f"의도 '{query_spec.get('intent')}' · 요구사항 {len(query_spec.get('asked_for', []))}건 "
              f"(추출 경로: {query_spec.get('source', 'rule')})",
              plan=query_spec.get("plan"))

    # ── 1.5 · 계획 감사 ───────────────────────────────────────
    plan_result, query_spec = supervise_plan(query_spec, set(CALC_REGISTRY), grounding)
    if plan_result.verdict != Verdict.APPROVE:
        trace.log("계획_감사", plan_result.as_trace())
    else:
        trace.log("계획_감사", "실행 계획 승인 — 미등록 호출 없음")

    conditions = query_spec.get("user_conditions") or {}

    # ── L2 · 함정 감지 ────────────────────────────────────────
    trap_context = build_trap_context(question)
    trace.log("L2_함정감지", trap_context["trace"])

    # ── L3 · Exploration ──────────────────────────────────────
    # 함정이 지목한 근거 문서를 검색에 넘긴다. L2가 L3보다 먼저 도는 덕에
    # "이 질의는 doc55를 봐야 한다"를 검색 시작 전에 알 수 있다.
    query_spec["retrieval_steer"] = trap_context.get("retrieval_steer") or []

    retrieve_hybrid = make_retrieve_hybrid(store)
    raw_evidence = retrieve_hybrid(query_spec)

    backend = ("BM25 + 임베딩 RRF 융합" if embedding_enabled()
               else "BM25 단독 (임베딩 미사용)")
    detail = f"후보 근거 {len(raw_evidence)}건 확보 ({backend})"
    if rewritten := query_spec.get("search_terms"):
        detail += f" · 검색어 재작성: {', '.join(rewritten[:5])}"
    if steered_docs := query_spec.get("_steered_docs"):
        detail += f" · 함정 유도로 {', '.join(steered_docs)} 예약 확보"
    if rr := query_spec.get("_rerank_trace"):
        detail += f" · 재순위: {rr}"
    trace.log("L3_정밀검색", detail)

    # ── L4 · Exploitation (순차) ──────────────────────────────
    evidence, constraint_warnings = _exploit(raw_evidence, query_spec, conditions, trace)

    # 문서에서 상품 후보를 뽑아 계산 인자로 공급 (총보수_비교용)
    candidates = extract_class_expenses(evidence)
    if candidates:
        conditions["product_candidates"] = candidates
        trace.log("L4_상품후보", f"근거 문서에서 판매 클래스 {len(candidates)}건 추출 "
                              f"(총보수 비교는 가입 가능한 클래스끼리만 수행)")

    # ── 슬롯 매핑 ─────────────────────────────────────────────
    slots = extract_required_slots(query_spec)
    slots = map_evidence_to_slots(slots, evidence,
                                  make_slot_evidence_matcher(query_spec), trace=trace)

    # ── L5 · Prediction ───────────────────────────────────────
    builder = make_calc_params_builder(conditions)
    slots = run_calculations(slots, builder, trace=trace)
    ask_back_items = builder.ask_back_items(limit=2)
    assumptions = builder.assumption_items()
    # 조건 해석 단계에서 남긴 주의사항(예: 합산액 기준 계산)도 한계 고지로 올린다
    assumptions.extend(conditions.get("condition_notes") or [])
    if trap_context.get("ask_back_candidates"):
        for item in trap_context["ask_back_candidates"]:
            if len(ask_back_items) < 2 and item not in ask_back_items:
                ask_back_items.append(item)

    # ── 답변가능성 판정 ───────────────────────────────────────
    refusal = check_refusal(question, grounding, evidence_count=len(evidence))
    decision = decide_answerability(slots, trace=trace, refusal=refusal,
                                    evidence_count=len(evidence))

    # 함정 critical 감지 시 한 단계 보수화
    if trap_context["critical_count"] and decision == Answerability.ANSWER:
        decision = Answerability.PARTIAL
        trace.log("답변가능성_보수화",
                  f"critical 함정 {trap_context['critical_count']}건 감지 → "
                  f"ANSWER를 PARTIAL로 낮추고 확인 조건을 함께 제시")

    if decision == Answerability.REFUSE:
        # 거절 직전에 한 번만 묻는다 — "자료 안에 이어 줄 인접 주제가 있는가".
        # 없으면 None이 돌아오고 기존 거절 그대로다.
        bridge = find_bridge(question, refusal, store)
        return _refuse_response(question_id, question, refusal, evidence, trace,
                                bridge=bridge)

    # ── L5' · Supervisor ──────────────────────────────────────
    generate_answer = make_generate_answer(
        client=client, trap_context=trap_context, assumptions=assumptions,
        ask_back_items=ask_back_items, trace_log=trace.log)

    if deadline.allows(BUDGET_L5):
        draft = generate_answer(query_spec, evidence, slots)
    else:
        draft = render_template_answer(query_spec, evidence, slots, trap_context,
                                       assumptions, ask_back_items)
        trace.log("L5'_예산초과", "남은 예산 부족 → 결정론적 템플릿 답변으로 진행")

    draft, forbidden = strip_forbidden(draft)
    if forbidden:
        trace.log("금지표현_치환",
                  f"단정 표현 {forbidden}을(를) 조건부 표현으로 치환 "
                  f"(단정적 추천 금지 요건)")

    # ── 요구사항 반영 검증 ────────────────────────────────────
    unmet = verify_requirement_coverage(draft, slots, answer_covers_slot, trace=trace)
    if unmet:
        draft += ("\n\n※ " + ", ".join(s.description for s in unmet)
                  + " 관련 내용은 제공 자료로 확정하기 어려워 별도 확인이 필요합니다.")

    if constraint_warnings:
        draft += "\n\n※ " + " / ".join(constraint_warnings)

    # ── 인용 조립 ─────────────────────────────────────────────
    used_evidence = _used_evidence(evidence, slots, query_spec)
    calc_results = [s.calc_result for s in slots
                    if s.status == SlotStatus.CALC_DONE and s.calc_result is not None]
    external = _external_sources(calc_results)
    citations = build_citations(used_evidence, calc_results,
                                doc_meta=store.doc_meta_map(),
                                external_sources=external,
                                legacy_checker=detect_legacy_tax_content)

    # ── L6 · 감독 이사회 ──────────────────────────────────────
    #
    # mentioned_products는 '검색된 상품 후보'가 아니라 **답변이 실제로 언급한
    # 상품**이어야 한다. 적합성 감사가 "가입 불가 상품이 답변에 등장했는가"를
    # 보기 때문이다. 검색 후보를 그대로 넘기면
    #   · 가입자격(eligible) 정보가 없어 함정 D1 감사가 무력화되고
    #   · 답변이 상품을 언급하지도 않았는데 '조건 미확인 상태의 상품 제시'로
    #     오판해 등급이 강등된다.
    mentioned = _products_in_answer(draft, candidates, conditions)

    # 부분 답변이 가능한데 되묻기만 하고 있는지를 감사가 판단할 수 있게 한다
    # (근거나 계산이 하나라도 확보된 상태면 부분 답변이 가능하다)
    partial_possible = any(
        s.status in (SlotStatus.COVERED, SlotStatus.CALC_DONE) for s in slots)

    # 감사자에게 '무엇을 조심해서 볼지'를 준다. 이게 없으면 의미 감사가
    # 모델 내부 지식에 의존하게 되는데, 이 도메인은 그걸 신뢰할 수 없다.
    audit_context = "\n".join(
        f"· {f['fact']}" for f in trap_context.get("facts", [])[:4])

    verify_grounding = make_verify_grounding(
        question=question, slots=slots,
        llm_call=(llm_call_adapter(client, audit_context)
                  if deadline.allows(BUDGET_L6) else None),
        citations=citations, user_conditions=conditions,
        ask_back_items=ask_back_items, answerability=decision.value,
        trap_ids=trap_context["detected"],
        trap_checks=trap_context.get("checks") or [],
        mentioned_products=mentioned,
        partial_answer_possible=partial_possible)

    verdict = verify_grounding(draft, evidence)
    trace.log("L6_감독심사", verdict.as_trace() or "심사 완료")

    supervision = verdict.supervision
    if supervision is not None and supervision.revised_ask_back:
        ask_back_items = supervision.revised_ask_back[:2]

    # 감독이 지적한 문제가 끝내 해소되지 않았는가.
    # ⚠️ 이 값이 True인 채로 그냥 답변을 내보내면 안 된다 —
    #    "틀릴 수 있다는 걸 알면서 확신 있게 답하는" 상태가 되기 때문이다.
    unresolved = False

    # ── REVISE → 재생성 1회 ───────────────────────────────────
    if supervision is not None and supervision.verdict == Verdict.REVISE:
        if deadline.allows(BUDGET_REGEN) and not getattr(client, "is_mock", False):
            remediation = build_remediation_prompt(supervision, draft)
            trace.log("L6_재생성", "REVISE 판정 — 시정 지시와 함께 L5'로 1회 되돌림 "
                                "(재생성은 1회로 제한)")
            try:
                from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT
                revised = client.call(SUPERVISOR_SYSTEM_PROMPT, remediation,
                                      purpose="l5_regenerate", max_tokens=1500)
            except Exception as e:
                revised = ""
                trace.log("L6_재생성_실패", f"재생성 호출 실패({e}) → 원본 답변 유지")
            if revised.strip():
                revised, _ = strip_forbidden(revised)
                recheck = verify_grounding(revised, evidence)
                if recheck:
                    draft = revised.strip()
                    # ⚠️ 판정도 함께 갱신해야 한다. 예전에는 옛 verdict를 그대로
                    #    들고 있어서, 재생성이 수치 오류를 고쳤는데도 아래
                    #    "수치검증 실패 → 템플릿 축퇴" 분기에 걸려 **성공한
                    #    재생성 결과를 그대로 버렸다.**
                    verdict = recheck
                    supervision = recheck.supervision
                    trace.log("L6_재생성_반영",
                              "재생성 답변이 검증을 통과해 채택 (판정도 갱신)")
                else:
                    unresolved = True
                    trace.log("L6_재생성_기각",
                              "재생성 답변도 검증에 실패 → 원본을 유지하되, "
                              "검증을 통과하지 못했다는 사실을 답변에 고지하고 "
                              "답변 등급을 낮춘다")
            else:
                unresolved = True
        else:
            unresolved = True
            trace.log("L6_재생성_생략",
                      "재생성 예산이 없거나 mock 모드 — 검증 미통과 사실을 고지하고 "
                      "등급을 낮춘 채로 진행")

    # ── BLOCK → 축퇴 ─────────────────────────────────────────
    if supervision is not None and supervision.verdict == Verdict.BLOCK:
        trace.log("L6_차단", "감독 심사 BLOCK — 생성 답변을 폐기하고 "
                           "계산 결과·근거만 담은 보수적 답변으로 축퇴")
        draft = render_template_answer(query_spec, evidence, slots, trap_context,
                                       assumptions, ask_back_items)

    # 수치 검증 실패는 반드시 답변에 반영한다 (조용히 넘기지 않는다)
    if verdict.numeric is not None and not verdict.numeric.passed:
        trace.log("수치검증_실패",
                  f"{verdict.numeric.reason} → 근거 없는 수치를 제거한 "
                  f"결정론적 답변으로 축퇴")
        draft = render_template_answer(query_spec, evidence, slots, trap_context,
                                       assumptions, ask_back_items)

    # ── 계산값이 끝내 답변에 없으면 결정론적으로 덧붙인다 ──────
    #
    # ⚠️ 재생성까지 했는데도 L5'가 숫자를 안 쓴 경우다. 여기서 포기하면
    #    "정확히 계산해 놓고 사용자에게는 안 알려주는" 답변이 나간다 —
    #    E-04/E-05가 실제로 그랬다(한도 1,200만원을 구해 놓고 답변에는
    #    산정 방식만 설명).
    #
    #    덧붙이는 값은 **계산함수의 출력을 그대로 렌더링한 것**이므로
    #    "계산은 함수, 설명은 LLM" 원칙에 어긋나지 않는다. LLM이 숫자를
    #    만드는 것이 아니라, 함수가 만든 숫자가 도달하도록 보장하는 것이다.
    final_presence = verify_calc_presence(draft, calc_results)
    if not final_presence.passed:
        lines = "\n".join(f"· {label}: {shown}"
                          for label, _v, shown in final_presence.missing)
        draft += f"\n\n계산 결과\n{lines}"
        trace.log("계산값_보강",
                  f"{final_presence.as_trace()} → 계산함수 출력을 결정론적으로 "
                  f"덧붙였다 (LLM이 숫자를 생성한 것이 아니다)")

    # ── 등급 강등 반영 ────────────────────────────────────────
    if supervision is not None and supervision.downgraded_answerability:
        trace.log("답변등급_강등",
                  f"{decision.value} → {supervision.downgraded_answerability}")
        decision = Answerability(supervision.downgraded_answerability)

    # ── 검증을 통과하지 못한 답변은 그 사실을 밝힌다 ──────────
    #
    # ⚠️ 이 블록이 이 시스템에서 가장 중요한 부분일지도 모른다.
    #    예전에는 감독이 초안과 재생성을 모두 반려해 놓고도, 원본을 그대로
    #    "보수적으로 유지"라는 이름으로 내보냈다. 사용자에게는 아무 표시도
    #    없었다. 즉 **시스템이 자기 답변의 부적절함을 두 번 확인하고도
    #    확신에 찬 어조로 답한 것**이다(Q-001). 틀린 답보다 나쁘다.
    #
    #    감독이 있다는 주장은, 감독 결과가 사용자에게 도달할 때만 참이다.
    if unresolved and supervision is not None:
        if decision == Answerability.ANSWER:
            trace.log("답변등급_강등",
                      "ANSWER → PARTIAL (감독 지적이 해소되지 않음)")
            decision = Answerability.PARTIAL

        notice, extra_asks = _unresolved_notice(supervision)
        if notice:
            draft += f"\n\n{notice}"
        # 해소되지 않은 지적은 되물을 항목으로 돌린다 —
        # 확인이 필요한 사항을 사용자가 알아야 다음 수를 둘 수 있다.
        for a in extra_asks:
            if a not in ask_back_items:
                ask_back_items.append(a)
        ask_back_items = ask_back_items[:2]      # 확인 항목은 최대 2건 (CLAUDE.md)
        trace.log("검증_미통과_고지",
                  "감독 지적이 남은 채로 답변이 나가므로, 그 사실을 답변 본문에 "
                  "명시하고 확인 항목으로 전환했다")

    if decision in (Answerability.PARTIAL, Answerability.ASK_BACK) and ask_back_items:
        draft += ("\n\n확인해 주시면 더 정확히 안내드릴 수 있습니다: "
                  + " / ".join(ask_back_items[:2]))

    # ── 인용 무결성 ───────────────────────────────────────────
    integrity = verify_citation_integrity(
        draft, citations, slots_used=[s.description for s in slots
                                      if s.status != SlotStatus.MISSING])
    trace.log("인용_무결성", integrity["trace"])

    answer = attach_citations(draft, citations)
    retrieved = citations_to_retrieved_context(citations, used_evidence)

    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": retrieved or "근거 문서 없음 — 제공 자료에서 "
                                          "이 질의를 뒷받침할 문서를 찾지 못했습니다.",
        "think_trace": _compose_trace(query_spec, trace),
        "answer": answer,
    }


# ════════════════════════════════════════════════════════════════
# 보조
# ════════════════════════════════════════════════════════════════

def _used_evidence(evidence: list[EvidenceChunk],
                   slots: list[RequirementSlot],
                   query_spec: Optional[dict] = None) -> list[dict]:
    """**사용한** 근거만 인용 대상으로 추린다 (검색된 전부가 아니라).

    ⚠️ 슬롯에 매핑된 근거가 하나도 없으면 **아무것도 인용하지 않는다.**
       예전에는 "인용 없는 답변을 만들지 않으려고" 상위 2건을 '검색 근거'라는
       이름으로 붙였다. 그 결과 "연금 말고 부동산 조언 좀"이라는 질의에
       판매 클래스 가입자격 문서가 근거로 달렸다. BM25는 무엇을 물어도
       상위 k건을 돌려주므로, 관련성 판단 없이 붙이면 반드시 이렇게 된다.

       근거가 없는 것보다 **무관한 근거가 있는 척하는 것이 훨씬 나쁘다.**
       인용이 비면 retrieved_context에 "근거 문서 없음"이 명시되고,
       그것이 정직한 상태다.
    """
    used_ids: dict[str, list[str]] = {}
    for s in slots:
        if s.status == SlotStatus.MISSING:
            continue
        for did in s.evidence_ids:
            used_ids.setdefault(did, []).append(s.description)

    # 계산 슬롯은 evidence_ids가 비어 있다(계산은 근거 매핑을 거치지 않는다).
    # 그러나 계산의 제도적 근거가 된 문서는 인용돼야 한다 —
    # 그렇지 않으면 "수치는 있는데 근거가 없는 답변"이 된다.
    calc_slots = [s for s in slots if s.status == SlotStatus.CALC_DONE]
    if calc_slots:
        matcher = make_slot_evidence_matcher(query_spec or {})
        for s in calc_slots:
            for c in evidence:
                if matcher(s, c):
                    supports = used_ids.setdefault(c.doc_id, [])
                    if s.description not in supports:
                        supports.append(s.description)
                    break        # 계산 1건당 대표 근거 1건이면 충분하다

    if not used_ids:
        return []

    out = []
    for c in evidence:
        if c.doc_id in used_ids:
            out.append({"doc_id": c.doc_id, "text": c.text,
                        "supports": used_ids[c.doc_id]})
    return out


def _products_in_answer(answer: str, candidates: list[dict],
                        conditions: dict) -> list[dict]:
    """답변이 실제로 언급한 상품만, 가입자격 판정을 붙여서 반환.

    적합성 감사(audit_fitness)가 기대하는 형태는
    `{"name": ..., "eligible": bool}` 이다. 검색 후보를 그대로 넘기면
    eligible 키가 없어 '가입 불가 상품 언급' 감사가 통과해 버린다
    (함정 D1 — 총보수 최저 클래스가 가입 불가인 경우).
    """
    if not answer or not candidates:
        return []
    account_type = conditions.get("account_type")
    out: list[dict] = []
    for c in candidates:
        fund_class = c.get("fund_class", "")
        if not fund_class or fund_class not in answer:
            continue
        entry = {**c, "name": c.get("name") or fund_class}
        if account_type:
            verdict = check_class_eligibility(fund_class, account_type)
            entry["eligible"] = verdict["eligible"]
            entry["reason"] = verdict["reason"]
        out.append(entry)
    return out


def _external_sources(calc_results: list) -> list[str]:
    """제공 자료 밖 근거를 쓴 계산이 있으면 외부 출처로 표시한다."""
    blob = str(calc_results)
    out = []
    if "일반 세법" in blob or "제공문서 외" in blob:
        out.append("소득세법 제47조의2 연금소득공제 등 일반 세법 기준")
    if "국민연금" in blob and "외부" in blob:
        out.append("국민연금공단 고시 (제공 자료 외 기본 제도 자료)")
    return out


def _refuse_response(question_id: str, question: str, refusal,
                     evidence: list[EvidenceChunk], trace: TraceLogger,
                     bridge=None) -> dict:
    """거절 응답. bridge가 있으면 '못 하는 것 + 이어 줄 것'으로 답한다.

    bridge는 자료에 인접 근거가 실재할 때만 채워져 온다(app/analysis/bridge.py).
    None이면 예전과 똑같이 순수 거절이다.
    """
    if bridge is not None:
        trace.log("거절_대신_연결", bridge.as_trace(), code=refusal.code)
        # 연결에 쓴 근거를 인용한다 — 실제로 이 문서들을 근거로 안내했으므로,
        # 여기서 비워 두면 '근거 없이 상품을 언급한' 답변이 된다.
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": "\n---\n".join(
                f"[{c.doc_id}]\n{c.text}" for c in bridge.evidence[:3]),
            "think_trace": trace.as_text(),
            "answer": bridge.as_answer(),
        }

    trace.log("최종_거절",
              refusal.detail or "근거 부족으로 답변하지 않음",
              code=refusal.code)
    retrieved = ("근거 문서 없음 — 제공 자료에서 관련 문서를 찾지 못했습니다."
                 if not evidence else
                 "\n---\n".join(f"[{c.doc_id}]\n{c.text}" for c in evidence[:3]))
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": retrieved,
        "think_trace": trace.as_text(),
        "answer": (refusal.reason or
                   "제공된 자료 범위에서는 답변드리기 어렵습니다.")
                  + " 연금 제도·세제·상품에 관한 질문이라면 다시 문의해 주세요.",
    }


def _compose_trace(query_spec: dict, trace: TraceLogger) -> str:
    """실행 계획을 서두에 두고 판단 흐름을 잇는다.

    평가지표 '추론 논리성'은 사람이 읽고 검증할 수 있어야 하므로,
    '무엇을 했다'가 아니라 '왜 그렇게 판단했다'가 남아야 한다.
    """
    lines = []
    if plan := query_spec.get("plan"):
        lines.append("[실행 계획]")
        lines.extend(f"  {i}. {p}" for i, p in enumerate(plan, 1))
        lines.append("")
    lines.append("[판단 과정]")
    lines.append(trace.as_text())
    return "\n".join(lines)


# 파이프라인이 스스로 해결하는 지적 — 사용자에게 고지할 것이 남지 않는다.
# CALC_NOT_SHOWN은 재생성이 실패해도 '계산 결과' 블록을 결정론적으로
# 덧붙여 반드시 해소되므로, 여기 남겨 두면 두 가지가 잘못된다:
#   ① 이미 해결된 문제를 "확인해 주십시오"라고 사용자에게 떠넘긴다
#   ② directive가 LLM용 지시문("반드시 문장 안에 그대로 적으십시오")이라
#      내부 프롬프트가 답변에 그대로 노출된다
_SELF_RESOLVING = {"CALC_NOT_SHOWN"}


def _unresolved_notice(supervision) -> tuple[str, list[str]]:
    """해소되지 않은 감독 지적을 사용자용 고지문과 확인 항목으로 바꾼다.

    ━━ 왜 내부 용어를 그대로 쓰지 않는가 ━━
    "TRAP_UNADDRESSED", "REVISE" 같은 말은 우리 쪽 사정이다. 사용자에게
    필요한 것은 "이 답변의 어느 부분을 그대로 믿으면 안 되는가"이다.
    그래서 코드가 아니라 **무엇을 더 확인해야 하는지**로 옮겨 적는다.

    반환: (답변에 덧붙일 고지문, 확인 항목으로 추가할 것들)
    """
    unresolved = [f for f in getattr(supervision, "findings", [])
                  if f.severity in (Verdict.REVISE, Verdict.BLOCK)
                  and f.code not in _SELF_RESOLVING]
    if not unresolved:
        return "", []

    lines = ["※ 이 답변은 내부 검증을 완전히 통과하지 못했습니다. "
             "아래 항목은 반드시 별도로 확인해 주십시오."]
    asks: list[str] = []
    for f in unresolved[:2]:
        # 시정 지시가 있으면 그게 가장 구체적이다
        detail = (f.directive or f.detail or "").strip()
        if not detail:
            continue
        lines.append(f"· {detail}")
        asks.append(detail[:80])

    if len(lines) == 1:      # 담을 내용이 없으면 고지문도 만들지 않는다
        return "", []
    return "\n".join(lines), asks


def health_info() -> dict:
    """/health 응답에 실을 운영 상태."""
    store = get_store()
    client = get_client()
    return {
        "status": "ok",
        "llm": {
            "mode": SETTINGS.llm_mode,
            "is_mock": bool(getattr(client, "is_mock", False)),
            "endpoint": SETTINGS.clova_endpoint,
            "warning": (MOCK_BANNER if getattr(client, "is_mock", False) else ""),
        },
        "corpus": {
            "kind": store.corpus_kind,
            "documents": len(store.docs),
            "chunks": len(store.chunks),
            # 판독 실패한 파일을 여기 드러낸다 — "넣었는데 검색이 안 된다"의
            # 원인이 대부분 여기 있고, 로그를 안 보면 알 수 없기 때문
            "skipped_files": store.skipped_files[:10],
            "skipped_count": len(store.skipped_files),
            "warning": ("⚠️ mock 코퍼스 — 실제 제공 문서가 아닙니다"
                        if store.corpus_kind == "mock" else ""),
        },
        "retrieval": _retrieval_health(store),
        "calc_functions": len(CALC_REGISTRY),
    }


def _retrieval_health(store) -> dict:
    """검색 백엔드 상태.

    ⚠️ USE_EMBEDDING=true 인데 벡터가 0건인 상황을 반드시 드러낸다.
       이 경우 조용히 BM25로 도는데, 겉보기에는 임베딩이 켜진 것처럼
       보여서 "왜 의미 검색이 안 되지"를 한참 헤매게 된다.
    """
    from app.retrieval.hybrid import vector_count

    vectors = vector_count()
    enabled = bool(SETTINGS.use_embedding)
    active = enabled and vectors > 0

    # 모델 자체를 못 불러온 경우(예: torch 미설치 이미지)를 가장 먼저 드러낸다.
    # 벡터 파일은 멀쩡히 있으므로 위의 '벡터 0건' 경고에는 걸리지 않고,
    # 겉보기에는 임베딩이 켜진 것처럼 보인 채로 계속 축퇴한다.
    from app.retrieval.embedding import local_unavailable_reason
    unavailable = local_unavailable_reason()

    warning = ""
    if unavailable:
        warning = (f"⚠️ 로컬 임베딩 모델을 불러오지 못해 BM25 단독으로 동작 중 "
                   f"({unavailable}). 이미지를 WITH_EMBEDDING=true 로 다시 "
                   f"빌드하십시오: `WITH_EMBEDDING=true docker compose build`")
    elif enabled and not vectors:
        warning = ("⚠️ 임베딩이 켜져 있으나 청크 벡터가 0건 — BM25 단독으로 동작 중. "
                   "`python -m app.ingest.build_embeddings` 를 실행하십시오.")
    elif active and vectors < len(store.chunks):
        warning = (f"⚠️ 청크 {len(store.chunks)}건 중 {vectors}건만 벡터가 있습니다. "
                   f"build_embeddings 를 다시 실행하면 남은 것만 채웁니다.")

    return {
        "embedding_enabled": enabled,
        "vectors": vectors,
        "mode": "BM25 + 벡터 RRF" if active else "BM25 단독",
        "warning": warning,
    }
