"""6계층 파이프라인 통합.

    L0   분류          grounding_retrieval (categorization만)    LLM 없음
    안전  게이트        check_safety_refusal                      LLM 없음
    L1   질의 분석      query_spec (HyperCLOVA X + 규칙 폴백)
    경로  분류          routing.classify_route                    LLM 없음
    1.5  계획 감사      supervise_plan                            LLM 없음
    L2   함정 감지      trap_rules (26종)                         LLM 없음
    L3 ∥ L4            retrieve_hybrid ∥ 가입자격 판정            LLM 없음
    합류  barrier       _eligibility_barrier                      LLM 없음
    L5   Prediction     CALC_REGISTRY 15종                        LLM 없음
    L4-sub / L5'       advisory ∥ answer_prompt (HyperCLOVA X)
    L6   감독 이사회    supervise_hybrid (결정론 + HyperCLOVA X)

━━ L3와 L4는 병렬이다 (2026-08-29 변경) ━━
L4의 두 일은 의존성이 다르다:
  · 근거 필터(구법·엔티티충돌) — L3 결과가 있어야 한다. 병렬 불가.
  · 가입자격 판정            — 사용자 조건만 보면 된다. 병렬 가능.

예전에는 자격 판정이 L4에 묶여 검색이 끝나야 시작됐고, 심지어 계산(L5)
뒤로 밀려 있었다. 그래서 "자격을 모른다"가 추천을 막는 방향으로 작동했다.
지금은 검색과 동시에 판정하고, 합류 지점의 barrier가 **확정적으로 불가한
것만** 걷어낸다. "총보수 최저인데 가입 불가능한 상품"을 막는 목적은
그대로 지키되, **모른다는 이유로는 막지 않는다.**

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

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from typing import Any, Optional

from app.analysis.bridge import find_bridge
from app.analysis.calc_params import make_calc_params_builder
from app.analysis.conditions import describe_conditions
from app.analysis.product_facts import _fact_lines as _fact_lines_of
from app.analysis.product_facts import (collect_facts, fact_snippets,
                                        facts_reflected_in_answer)
from app.analysis.products import extract_class_expenses
from app.analysis.query_spec import make_extract_query_spec
from app.analysis.refusal import check_refusal, check_safety_refusal
from app.analysis.routing import classify_route
from app.analysis.slot_matching import answer_covers_slot, make_slot_evidence_matcher
from app.config import SETTINGS
from app.core.citation_system import (attach_citations, build_citations,
                                      verify_product_grounding,
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
from app.core.grounding_retrieval import build_refuse_response, ground_query
from app.core.pension_calc_functions import (check_class_eligibility,
                                             detect_legacy_tax_content)
from app.core.sub_agent import supervise_logic
from app.core.supervisory_board import (Verdict, build_remediation_prompt,
                                        supervise_plan)
from app.core.trap_rules import build_trap_context, unaddressed_traps
from app.generation.advisory import (make_generate_advisory,
                                      render_advisory_fallback)
from app.generation.answer_prompt import (make_generate_answer,
                                          render_template_answer,
                                          strip_forbidden, strip_markdown)
from app.generation.grounding import make_verify_grounding
from app.ingest.store import get_store
from app.llm.clova import MOCK_BANNER, get_client, llm_call_adapter
from app.retrieval.coarse import make_coarse_search
from app.retrieval.embedding import embedding_enabled
from app.retrieval.hybrid import make_retrieve_hybrid

log = logging.getLogger("pipeline")


def _budget_sec(name: str, default: float) -> float:
    """예산 환경변수 읽기. 잘못된 값은 조용히 기본값으로 — 기동을 막지 않는다.

    ⚠️ 하한을 둔다. 0이나 음수가 들어오면 모든 LLM 단계가 통째로 생략돼
       "200 OK인데 HCX가 만들지 않은 답변"이 나간다 — 절대 제약 #1 위반을
       오타 하나로 만들 수 있는 자리다.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        log.warning("%s 값을 읽을 수 없어 기본값 %.1f초를 씁니다: %r",
                    name, default, raw)
        return default
    if val < 10.0:
        log.warning("%s=%.1f 는 너무 작아 기본값 %.1f초를 씁니다 "
                    "(LLM 단계가 통째로 생략됩니다)", name, val, default)
        return default
    return val


# 전체 요청 예산(초). 평가 API는 단일 GET 안에서 끝나야 한다.
#
# 2026-08-18 실코퍼스(158문서) 대상 실배포 로그로 재갱신:
#   smoke_test의 짧은 프로브(554ms~844ms)와 달리, 근거 문서가 실린 실제
#   프롬프트(입력 3000토큰 안팎)는 L5'/L6가 5~8초씩 걸렸다. REVISE 재생성이
#   붙고 그 중 한 번이라도 타임아웃→재시도를 타면 L1+L5+L6+regen(L5)+L6
#   전체 경로가 30초를 넘길 수 있다(실측: 정상 완료 31160ms). 25초는 이
#   정상 경로조차 못 담아 조기에 잘라내므로 상향한다.
#
# 2026-09-02 — 환경변수로 빼고, "상한 60초"에 맞춰 55초로 올린다.
#   실측에서 REVISE가 떴는데 `L6_재생성_생략`으로 빠지는 경우가 나왔다.
#   이 예산은 **평가 측 타임아웃과의 트레이드오프**다 — 올리면 재생성
#   기회가 생기고, 너무 올리면 평가가 먼저 끊는다.
#
# 2026-09-05 — ⚠️ **전제가 틀렸다. 실제 상한은 60초가 아니라 300초다.**
#   주최측 공지 원문: "문항 당 응답 대기 시간(타임아웃)은 300초입니다.
#   타임아웃 또는 5xx 오류 발생 시, 최대 2회를 재시도합니다."
#   또한 "평가 질의는 각 팀당 순차적으로 1건씩 전송되며, 동시 요청은
#   없습니다" — 동시성으로 인한 지연 경쟁도 없다.
#
#   55초는 허용치의 18%만 쓰고 있었고, 그 대가로 **품질을 올릴 수 있는
#   단계를 스스로 잘라내고 있었다** — REVISE가 떠도 `L6_재생성_생략`,
#   구제 재생성은 `SubAgent_구제_생략`으로 빠지는 일이 실제로 있었다.
#   재생성은 감독이 반려한 답변을 고칠 유일한 기회이므로, 그것을 예산
#   부족으로 건너뛰는 것은 잘못된 트레이드오프였다.
#
#   왜 240인가:
#     · 최악 경로 = HCX 7회 × (CLOVA_TIMEOUT_SEC 15초 + 재시도 1회)
#       ≈ 210초 + 결정론 단계 1~2초 ≈ 212초.
#     · 마지막 게이트(BUDGET_SUBAGENT_REWRITE = 18)는 경과 ≤ 222초일 때만
#       열리므로, 그 단계가 최악으로 30초를 써도 t≈252초에 끝난다.
#     · 300초까지 48초가 남는다. 상한과 총 예산을 같은 값으로 두지 않는다는
#       원칙(아래)을 지키면서도 전 구간이 열린다.
#   배포에서 재조정할 수 있게 값은 PIPELINE_BUDGET_SEC로 노출한다.
#
#   ⚠️ 이 예산은 **게이트일 뿐 중단 장치가 아니다.** 이미 시작한 호출을
#      끊지는 않으므로, 낮춘다고 응답이 그만큼 빨라지지는 않는다. 그래서
#      "총 예산 = 상한"으로 두면 안 된다 — 마지막 게이트가 아슬아슬하게
#      열린 뒤 그 단계가 통째로 상한 밖에서 끝난다.
#   ⚠️ **응답 시간은 평가지표가 아니다**(평가지표 7종에 없다). 300초 안에서는
#      빠른 것보다 정확한 것이 낫다 — 예산을 다시 조일 이유가 있다면 그건
#      속도가 아니라 타임아웃 위험이어야 한다.
TOTAL_BUDGET_SEC = _budget_sec("PIPELINE_BUDGET_SEC", 240.0)

# ── 단계별 예산 ───────────────────────────────────────────────
#
# 이 시점까지 남은 시간이 자기 예산보다 적으면 해당 LLM 단계를 건너뛴다.
#
# ⚠️ 값의 의미는 "이 단계가 실제로 쓰는 시간"이다. 실측(대형 프롬프트
#    5~8초)보다 작게 잡으면 게이트가 통과시켜 놓고 총 예산을 넘긴다 —
#    예산표가 거짓이 되고, 그 상태로는 어떤 값을 조정해도 근거가 없다.
#    그래서 HCX 호출 1회를 8초로 잡고, 단계가 몇 번 호출하는지로 센다.
#
# 예산이 정직하면 **어떤 단계든 늦어도 t≈TOTAL에 끝난다** — 게이트가
# `remaining >= need`일 때만 열리기 때문이다. 그 뒤는 전부 결정론적이라
# 1초 미만이다. 상한과 총 예산을 같은 값으로 두면 안 된다 — 게이트는
# 중단 장치가 아니라서 마지막 단계가 통째로 상한 밖에서 끝난다.
#
#   ✅ 전 구간(L1 + L5' + L6 + 재생성 + 구제 재생성 = HCX 7회)이 이제
#      **전부 열린다.** 정상 지연으로는 ≈56초, 매 호출이 타임아웃→재시도를
#      타는 최악으로도 ≈212초라 240초 예산 안에 들어간다.
#      (2026-09-05 이전에는 상한을 60초로 잘못 알고 있어서, 구제 재생성이
#       앞 단계가 빨리 끝났을 때만 열렸다. 그건 사실이 아니라 착오였다.)
#      그래도 열리지 못하는 경우가 생기면 `SubAgent_구제_생략`에 남은
#      시간과 함께 기록된다 — 조용히 사라지지 않으므로 조정 근거가 된다.
#   ⚠️ 호출 하나가 타임아웃(15초)→재시도를 타면 정상 지연 계산은 깨진다.
#      그건 예산이 아니라 CLOVA_TIMEOUT_SEC/CLOVA_MAX_RETRY의 영역이고,
#      위 240초는 그 최악까지 감안해 잡은 값이다.
BUDGET_L1 = 8.0             # 질의 분석 1회
BUDGET_L5 = 10.0            # 답변 생성 1회 (본문이라 길다)
# ⚠️ 의미 감사 1회. 8.0 → 10.0 (2026-09-04, 답변–조문 저촉 검사 추가)
#    같은 호출 안에서 조문까지 대조하므로 페이로드가 커진다. 실측으로
#    조문 4건이 실릴 때 페이로드가 697자 → 3,772자였다(+3,075자).
#    게이트 값이 실제 소요보다 작으면 **통과시켜 놓고 총 예산을 넘긴다** —
#    BUDGET_REGEN이 10.0이던 시절에 정확히 그 일이 있었다(위 주석).
#    ⚠️ 이 +2.0은 페이로드 증가분에서 잡은 보수적 추정이지 지연 실측이
#       아니다. T2(실 HCX 백테스트)에서 L6 소요를 재고 확정할 것.
#    법령 수집본이 없는 서버에서는 페이로드가 커지지 않으므로 이 값이
#    2초 보수적으로 동작할 뿐이다(뒤 단계가 조금 덜 열린다). 상한 60초를
#    넘기는 것보다는 그쪽이 낫다.
BUDGET_L6 = 10.0
# ⚠️ 재생성은 **두 번** 호출한다 — 생성 + 재검증(의미 감사)이다.
#    재검증은 이 게이트와 무관하게 무조건 수행된다. 검증 없이 채택하면
#    감사를 우회하는 뒷문이 되기 때문이다(CLAUDE.md). 예전에는 이 값이
#    10.0이라 **생성 한 번 값만 잡혀 있었고**, 게이트가 통과시킨 뒤
#    재검증이 예산 밖에서 돌아 총 예산을 조용히 넘겼다.
BUDGET_REGEN = 10.0 + BUDGET_L6
# Sub-Agent 진단은 보조 장치다(1회 호출). 남은 시간이 이만큼 없으면
# 감지만 하고 호출하지 않는다 — 보조 장치가 본체를 지연시키면 그 자체가
# 결함이다.
BUDGET_SUBAGENT = 8.0
# 구제 재생성도 생성 + 재검증 두 번이다. L6가 REVISE를 내고 L5' 재생성
# 마저 기각된 드문 경우에만 쓰인다.
BUDGET_SUBAGENT_REWRITE = 8.0 + BUDGET_L6

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
    """근거를 다듬고 제도적 제약을 모은다.

    반환: (통과한 근거, 경고 문구)

    ━━ 가입자격은 여기서 판정하지 않는다 ━━
    예전에는 구법탐지 → 엔티티충돌 → **가입자격** → 하드제약 순으로 한 줄에
    엮여 있었다. 문제는 실제 사용자 질의가 대부분 짧다는 것이다 —
    "나 몇 살인데 연금 계획 좀" 같은 질문에는 판매 클래스도 계좌 유형도 없다.
    그러면 조건이 없어 가입자격 판정이 **조용히 통째로 건너뛰어졌고**,
    경고도 되묻기도 남지 않았다.

    그래서 성격이 다른 둘을 분리했다:
      · 여기(_exploit)  — 근거 문서를 고르고 다듬는 규칙. 조건이 없어도 돈다.
      · _eligibility()  — 사용자 조건이 있어야만 가능한 판정. 마지막에 따로.
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

    # ⚠️ 함정이 지목한 근거 문서(retrieval_steer)는 이 필터에서 면제한다.
    #    L3가 이미 이 문서를 예약(pinned, score 0.9)해 순위 경쟁에서
    #    보호했는데, 그 뒤 여기서 엔티티 충돌·점수 임계값으로 조용히
    #    떨어뜨리면 인용까지 못 간다. _addressed_trap_docs는 evidence에
    #    남은 것만 인용할 수 있어서, 여기서 탈락하면 답변이 함정을
    #    정확히 교정해도 근거 문서가 안 실린다.
    #    같은 계열 실패가 실측에서 3회 반복됐다(L-01/doc55·E-19/doc20·
    #    E-26/doc40). 구법 배제(1단계)는 이미 이 청크에도 적용됐으므로
    #    여기서 면제해도 구법 문서가 되살아나지는 않는다.
    steered_docs = set(query_spec.get("_steered_docs") or ())
    protected = [c for c in after_legacy if c.doc_id in steered_docs]
    unprotected = [c for c in after_legacy if c.doc_id not in steered_docs]

    filtered = filter_irrelevant_evidence(unprotected, query_spec, trace=trace)
    if protected:
        # 트레이스용 — 보호가 실제로 뭔가를 구했는지 알려준다(순수 조회,
        # protected 자체는 건드리지 않는다).
        would_survive = {id(c) for c in
                         filter_irrelevant_evidence(protected, query_spec, trace=None)}
        saved = [c for c in protected if id(c) not in would_survive]
        if saved:
            trace.log("L4_함정근거_보호",
                      f"함정이 지목한 근거 {len(saved)}건이 일반 필터라면 "
                      f"제외됐을 것이나 함정 근거이므로 유지",
                      docs=sorted({c.doc_id for c in saved}))
    kept = filtered + protected

    if not kept and after_legacy:
        kept = filter_irrelevant_evidence(
            after_legacy, query_spec, score_threshold=0.0, trace=None)[:3]
        trace.log("L4_필터_완화",
                  "필터 후 근거가 0건이 되어 점수 임계값만 낮춰 상위 3건 유지 "
                  "(구법 제외는 유지)")

    # ── 3. 하드제약 (조건 기반 — 근거 필터와 독립) ──
    warnings.extend(_hard_constraints(conditions, trace))
    return kept, warnings


# 가입자격을 물은 것으로 볼 신호. 이게 없으면 판정 자체가 질의와 무관하므로
# 굳이 되묻지 않는다 — 확인 항목은 최대 2건이라 자리가 아깝다(CLAUDE.md).
_ELIGIBILITY_SIGNALS = ("클래스", "가입", "class", "총보수", "수수료",
                        "어떤 상품", "상품 추천", "펀드")


@dataclass
class EligibilityVerdict:
    """가입자격 판정 — L3와 **병렬로** 산출된다.

    ━━ '모른다'와 '안 된다'를 구분한다 ━━
    이 구분이 이번 개편의 핵심이다. 예전에는 자격을 모르면 추천 자체를
    보류하거나 등급을 강등했다. 그런데 사용자 입장에서는

        "추천했는데 자격이 안 되면 의미가 없다"  (예전)
        "자격을 모르니 일단 추천하고, 자격을 알려주시면 더 자세히" (지금)

    후자가 옳다. 모르면 통과시키고 확인을 요청하며, **안 되는 것이
    확정됐을 때만** barrier가 제외한다.
    """

    known: bool = False               # 판정에 필요한 조건이 다 있는가
    eligible: Optional[bool] = None   # None = 미상 (모름 ≠ 불가)
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    account_type: str = ""

    @property
    def blocks(self) -> bool:
        """추천을 막아야 하는가. **확정적으로 불가할 때만 참.**"""
        return self.known and self.eligible is False


def _eligibility_verdict(conditions: dict, question: str) -> EligibilityVerdict:
    """사용자 조건만으로 가입자격을 판정한다 (근거 문서 불필요).

    ⚠️ 이 함수는 L3 검색 결과에 의존하지 않는다 — 그래서 병렬 실행이
       가능하다. 예전에는 L4 안에 묶여 있어 검색이 끝나야 시작됐다.
    """
    fund_class = conditions.get("fund_class")
    account_type = conditions.get("account_type") or ""

    if fund_class and account_type:
        v = check_class_eligibility(fund_class, account_type)
        return EligibilityVerdict(
            known=True, eligible=bool(v["eligible"]),
            reason=v["reason"], account_type=account_type)

    if not any(s in (question or "") for s in _ELIGIBILITY_SIGNALS):
        # 가입자격을 묻지 않은 질의 — 되물을 이유가 없다
        return EligibilityVerdict(known=False, account_type=account_type)

    missing = []
    if not fund_class:
        missing.append("가입하려는 판매 클래스(예: C-Pe, C-Re)")
    if not account_type:
        missing.append("연금계좌 유형(연금저축 / IRP / DC)")
    return EligibilityVerdict(known=False, missing=missing,
                              account_type=account_type)


def _eligibility_barrier(candidates: list[dict],
                         verdict: EligibilityVerdict,
                         trace: TraceLogger) -> tuple[list[dict], list[str]]:
    """L3∥L4 **합류 지점의 barrier.** 자격 미달 상품만 제외한다.

    반환: (추천 가능한 후보, 경고 문구)

    ━━ 통과 규칙 ━━
    · 자격이 확정적으로 불가 → 제외하고 그 사실을 밝힌다
    · 자격을 모름               → **통과.** 추천하되 확인을 요청한다
    · 계좌유형만 아는 경우      → 그 유형에 맞지 않는 클래스만 제외

    "총보수 최저인데 가입 불가능한 상품"을 추천하는 사고를 막는 것이
    이 barrier의 목적이다. 다만 **모른다는 이유로 막지는 않는다.**
    """
    if not candidates:
        return [], []

    warnings: list[str] = []

    # 계좌유형을 알면 클래스별로 개별 판정한다 — 모르면 전부 통과시킨다.
    if verdict.account_type:
        kept, dropped = [], []
        for c in candidates:
            name = c.get("fund_class") or c.get("name") or ""
            v = check_class_eligibility(name, verdict.account_type)
            c["eligible"] = bool(v["eligible"])
            c["eligibility_reason"] = v["reason"]
            (kept if v["eligible"] else dropped).append(c)
        if dropped:
            names = ", ".join(d.get("fund_class") or d.get("name", "?")
                              for d in dropped)
            trace.log("합류_barrier",
                      f"{verdict.account_type} 기준 가입 불가 클래스 "
                      f"{len(dropped)}건을 추천에서 제외: {names}")
            warnings.append(
                f"{verdict.account_type} 계좌로는 가입할 수 없는 클래스"
                f"({names})는 비교에서 제외했습니다")
        return kept, warnings

    # 계좌유형 미상 — 전부 통과시키고 확인을 요청한다
    for c in candidates:
        c.setdefault("eligible", None)
    trace.log("합류_barrier",
              f"계좌유형 미확인 → 후보 {len(candidates)}건을 그대로 두고 "
              f"확인을 요청 (모른다는 이유로 추천을 막지 않는다)")
    return candidates, []


def _hard_constraints(conditions: dict, trace: TraceLogger) -> list[str]:
    """수치를 계산하기 전에 걸러야 하는 제도적 제약."""
    out: list[str] = []

    age = conditions.get("age")
    # ⚠️ int만 받으면 안 된다. LLM이 준 age는 conditions.py의 입력 검증
    # 단계에서 float으로 통일된다(비정상 문자열을 걸러내기 위해) — 규칙
    # 기반 파싱만 int를 낸다. 여기서 int만 받으면 LLM 경로의 55세 미만
    # 제약이 조용히 빠진다.
    if isinstance(age, (int, float)) and age < 55 and conditions.get("private_pension_monthly_manwon"):
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
    """평가 API 5필드 응답을 만든다. 어떤 경우에도 예외를 밖으로 던지지 않는다.

    ⚠️ 이 약속은 원래 문서화만 돼 있고 실제로 지켜지지 않았다. 실제 사고:
       프롬프트 탈취 질의(E-18)에서 L1이 내놓은 JSON에 만원 필드 값으로
       마크다운 조각("**") 같은 비정상 문자열이 섞여 들어왔고, 그걸 그대로
       conditions에 저장한 뒤 format_manwon()이 float()으로 변환하다 죽었다.
       이 예외가 그대로 위로 뚫고 나가 **평가 전체가 중단**됐다 — 단일 GET
       요청 규격에서 한 문항의 입력 이상이 전체를 죽이면 안 된다.
       그래서 본문을 얇은 시도-실패 경계로 감싼다. 근본 원인(비검증 LLM
       출력)은 conditions.py에서 따로 막았지만, 이 경계는 **아직 못 막은
       다음 사고**에 대한 최후 방어선이다.
    """
    try:
        return _answer_question_impl(question_id, question, store, client)
    except Exception as e:      # noqa: BLE001 — 평가 API는 절대 죽으면 안 된다
        log.error("[pipeline] answer_question 미처리 예외 (Q=%s): %s",
                 question_id, e, exc_info=True)
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": "근거 문서 없음 — 처리 중 오류가 발생했습니다.",
            "think_trace": f"[치명적 오류] {type(e).__name__}: {e}\n"
                           f"파이프라인이 예상치 못한 예외로 중단되어 안전 응답으로 대체합니다.",
            "answer": "죄송합니다. 이 질문을 처리하는 중 오류가 발생했습니다. "
                      "질문을 조금 다르게 표현해 다시 시도해 주세요.",
        }


def _answer_question_impl(question_id: str, question: str,
                          store=None, client=None) -> dict:
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
    trace.log("L0_분류", grounding.trace, areas=grounding.domain_areas)

    # ── 안전 게이트 (L1 진입 직전) ────────────────────────────
    #
    # ⚠️ 여기 남은 것은 **조건을 더 안다고 판단이 뒤집히지 않는** 셋뿐이다:
    #    빈 질의 · 개인정보 조회 요구 · 프롬프트 인젝션.
    #    예전의 조기 거절(도메인 커버리지·근거 0건)은 전부 제거했다 —
    #    사용자 조건을 하나도 모르는 시점의 판정이라 과최적화됐고,
    #    "부동산은 없고 현금 3,500만원" 같은 정상 질의를 잘라냈다.
    safety = check_safety_refusal(question)
    if safety.refuse:
        trace.log("안전_거절", f"{safety.detail} — LLM 호출 없이 종결 (호출 0회)")
        resp = build_refuse_response(question_id, question, safety.reason)
        resp["think_trace"] = trace.as_text() + "\n" + resp["think_trace"]
        return resp

    # ── 거절 대신 연결 ────────────────────────────────────────
    # 자료 밖 주제인데 자료 안에 이어 줄 근거가 실재할 때만 채워져 온다.
    pre_refusal = check_refusal(question, grounding)
    if (bridge := find_bridge(question, pre_refusal, store)) is not None:
        trace.log("거절_대신_연결", bridge.as_trace(), code=pre_refusal.code)
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": "\n---\n".join(
                f"[{c.doc_id}]\n{c.text}" for c in bridge.evidence[:3]),
            "think_trace": trace.as_text(),
            "answer": bridge.as_answer(),
        }

    if pre_refusal.refuse:
        trace.log("자료범위_밖", f"{pre_refusal.detail} — 연결할 근거도 없어 종결")
        resp = build_refuse_response(question_id, question, pre_refusal.reason)
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
    extra_conditions = query_spec.get("extra_conditions") or {}
    if extra_conditions:
        # 정규 스키마에 자리가 없어 예전에는 통째로 버려지던 정보다.
        # 계산에는 못 쓰지만 무엇을 안내하고 무엇을 되물을지는 이것이 정한다.
        trace.log("자유_조건",
                  ", ".join(f"{k}={v}" for k, v in extra_conditions.items()))

    # ── 경로 분류 (결정론적) ──────────────────────────────────
    #
    # ⚠️ HCX 재량에 맡기지 않는다. 같은 질의가 실행마다 다른 계층을 타면
    #    재현도 디버깅도 불가능해진다. L1의 HCX는 조건을 뽑고, 경로는
    #    그 결과를 보고 코드가 정한다 ("판단은 코드, 문장은 LLM").
    route = classify_route(question, conditions, query_spec.get("asked_for"))
    trace.log("경로_분류", route.as_trace())

    # 계산이 틀렸을 때 "조건이 잘못 잡힌 것"과 "계산이 잘못된 것"을 가르는
    # 유일한 단서다. 특히 금액 단위 사고(만원↔억↔원)는 이 줄이 없으면
    # 답변만 보고는 원인을 좁힐 수 없다.
    trace.log("확정_조건", ", ".join(f"{k}={v}" for k, v in conditions.items())
                          or "확인된 조건 없음")

    # ── L2 · 함정 감지 ────────────────────────────────────────
    trap_context = build_trap_context(question)
    trace.log("L2_함정감지", trap_context["trace"])

    # ── L3 · Exploration ──────────────────────────────────────
    # 함정이 지목한 근거 문서를 검색에 넘긴다. L2가 L3보다 먼저 도는 덕에
    # "이 질의는 doc55를 봐야 한다"를 검색 시작 전에 알 수 있다.
    query_spec["retrieval_steer"] = trap_context.get("retrieval_steer") or []

    # ── L3 ∥ L4 · 병렬 수행 ───────────────────────────────────
    #
    # ━━ 무엇이 진짜로 병렬 가능한가 ━━
    # L4의 두 일은 의존성이 다르다:
    #   · 근거 필터(구법·엔티티충돌) — L3 결과가 있어야 한다. 병렬 불가.
    #   · 가입자격 판정            — **사용자 조건만** 보면 된다. 병렬 가능.
    #
    # 예전에는 자격 판정이 L4 안에 묶여 검색이 끝나야 시작됐고, 게다가
    # 계산(L5) 뒤로 밀려 있었다. 그래서 "자격을 모른다"가 추천을 막는
    # 방향으로 작동했다. 지금은 검색과 동시에 판정하고, 합류 지점의
    # barrier가 **확정적으로 불가한 것만** 걷어낸다.
    retrieve_hybrid = make_retrieve_hybrid(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_retrieve = pool.submit(retrieve_hybrid, query_spec)
        f_elig = pool.submit(_eligibility_verdict, conditions, question)
        raw_evidence = f_retrieve.result()
        elig = f_elig.result()

    backend = ("BM25 + 임베딩 RRF 융합" if embedding_enabled()
               else "BM25 단독 (임베딩 미사용)")
    detail = f"후보 근거 {len(raw_evidence)}건 확보 ({backend})"
    if rewritten := query_spec.get("search_terms"):
        detail += f" · 검색어 재작성: {', '.join(rewritten[:5])}"
    if steered_docs := query_spec.get("_steered_docs"):
        detail += f" · 함정 유도로 {', '.join(steered_docs)} 예약 확보"
    if rr := query_spec.get("_rerank_trace"):
        detail += f" · 재순위: {rr}"
    trace.log("L3_정밀검색", detail + " (L4 자격판정과 병렬 수행)")

    if elig.known:
        trace.log("L4_가입자격",
                  f"{'가입 가능' if elig.eligible else '가입 불가'}: {elig.reason}")
    elif elig.missing:
        trace.log("L4_가입자격_미상",
                  f"판정 조건 미확인 {elig.missing} — 추천을 막지 않고 확인을 요청")

    # ── 근거 필터 (L3 결과 의존) ──────────────────────────────
    evidence, constraint_warnings = _exploit(raw_evidence, query_spec, conditions, trace)

    # ── 합류 barrier — 자격 미달 상품만 제외 ──────────────────
    candidates = extract_class_expenses(evidence)
    candidates, barrier_warnings = _eligibility_barrier(candidates, elig, trace)
    constraint_warnings.extend(barrier_warnings)
    if elig.blocks and elig.reason:
        constraint_warnings.append(elig.reason)
    if candidates:
        conditions["product_candidates"] = candidates
        trace.log("L4_상품후보", f"근거 문서에서 판매 클래스 {len(candidates)}건 확보 "
                              f"(barrier 통과분)")

    # ── 상품 팩트 결합 (색인 시점 전수 파싱 결과) ──────────────
    #
    # 위험등급·상품분류·수익률·시장잔고는 **검색된 청크가 아니라 색인 시점에
    # 문서 전문에서** 뽑아 doc_meta에 넣어 둔 값이다(ingest/metadata.py).
    # 여기서는 근거로 채택된 문서의 것만 꺼내 쓴다 — 색인에는 전 문서의
    # 팩트가 있지만, 검색이 고르지 않은 문서의 수치를 답변에 쓰면 그건
    # 근거 없는 인용이다.
    #
    # query_spec에 실어 두면 L5'와 L4-sub가 **같은 값을 같은 방식으로** 본다.
    # 생성기마다 따로 넘기면 경로별로 처리가 갈리고 반드시 어긋난다.
    product_facts = collect_facts(
        [c.doc_id for c in evidence], store.doc_meta)
    if product_facts:
        query_spec["_product_facts"] = product_facts
        axes = sorted({a for f in product_facts
                       for a, _l, _s in _fact_lines_of(f)})
        trace.log("L4_상품팩트",
                  f"근거 문서 {len(product_facts)}건에서 확정 팩트 확보 "
                  f"({', '.join(axes)}) — 색인 시점 전수 파싱분")

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

    # ── 자격 미확인 항목을 되물을 목록에 올린다 ────────────────
    # 판정 자체는 L3와 병렬로 이미 끝났다(elig). 여기서는 '무엇을 더
    # 알려주시면 좁힐 수 있는지'만 확인 항목으로 옮긴다 — 추천을 막지는
    # 않는다. barrier가 확정적 불가만 걷어냈고, 나머지는 통과시켰다.
    for item in elig.missing:
        if len(ask_back_items) < 2 and item not in ask_back_items:
            ask_back_items.append(item)

    # ── 답변가능성 판정 ───────────────────────────────────────
    refusal = check_refusal(question, grounding, evidence_count=len(evidence))
    decision = decide_answerability(slots, trace=trace, refusal=refusal,
                                    evidence_count=len(evidence),
                                    is_advisory=route.is_advisory)

    # ── 1.5 계획 감사 판정 반영 ────────────────────────────────
    #
    # ⚠️ 계획 감사는 라우팅 **앞**에서 돌린다 — 미등록 호출 제거가 계산보다
    #    먼저 끝나야 하기 때문이다. 그런데 판정까지 그 자리에서 적용하면
    #    경로를 모르는 채로 깎게 된다. 그래서 교정(safe_spec)은 즉시,
    #    판정은 경로가 정해진 여기서 반영한다.
    #
    #    NO_SLOTS를 ADVISORY에서 제외하는 이유: 슬롯이 비는 것은 불특정
    #    서술의 **정상 상태**이지 결함이 아니다. R3에서 L4-sub가 받기로 한
    #    바로 그 경우를 다시 깎으면 그 변경이 무효가 된다.
    #
    #    (2026-08-29 이전에는 plan_result가 trace 로그로만 쓰이고 판정이
    #     어디에도 반영되지 않았다 — 감사는 돌지만 결과가 버려졌다.)
    if plan_result.downgraded_answerability:
        codes = {f.code for f in plan_result.findings}
        if route.is_advisory and codes <= {"NO_SLOTS"}:
            trace.log("계획_감사_판정",
                      "NO_SLOTS는 ADVISORY 경로의 정상 상태 — 강등하지 않는다")
        elif decision == Answerability.ANSWER:
            decision = Answerability(plan_result.downgraded_answerability)
            trace.log("계획_감사_판정",
                      f"ANSWER → {decision.value} "
                      f"(계획 감사 지적: {', '.join(sorted(codes))})")

    # 함정 critical 감지 시 한 단계 보수화
    if trap_context["critical_count"] and decision == Answerability.ANSWER:
        decision = Answerability.PARTIAL
        trace.log("답변가능성_보수화",
                  f"critical 함정 {trap_context['critical_count']}건 감지 → "
                  f"ANSWER를 PARTIAL로 낮추고 확인 조건을 함께 제시")

    if decision == Answerability.REFUSE:
        return _refuse_response(question_id, question, refusal, evidence, trace)

    # ── 답변 생성 — 경로에 따라 L4-sub 또는 L5' ────────────────
    #
    # 두 생성기는 시그니처가 같다. 여기서만 갈리고 이후 검증·인용·감독은
    # **한 벌로 공유한다** — 경로마다 다른 처리를 만들면 검증이 두 벌이
    # 되고 반드시 어긋난다.
    if route.is_advisory:
        generate = make_generate_advisory(
            client=client, extra_conditions=extra_conditions,
            route_reason=route.reason, trace_log=trace.log,
            trap_context=trap_context)
        stage, fallback_note = "L4sub", "확인 항목 안내"
    else:
        generate = make_generate_answer(
            client=client, trap_context=trap_context, assumptions=assumptions,
            ask_back_items=ask_back_items, trace_log=trace.log)
        stage, fallback_note = "L5'", "결정론적 템플릿 답변"

    if deadline.allows(BUDGET_L5):
        draft = generate(query_spec, evidence, slots)
        trace.log(f"{stage}_답변생성",
                  f"{'상담 위임' if route.is_advisory else '계산·근거 기반'} "
                  f"경로로 초안 생성")
    elif route.is_advisory:
        draft = render_advisory_fallback(query_spec, evidence, extra_conditions,
                                         trap_context)
        trace.log("L4sub_예산초과", f"남은 예산 부족 → {fallback_note}로 진행")
    else:
        draft = render_template_answer(query_spec, evidence, slots, trap_context,
                                       assumptions, ask_back_items)
        trace.log("L5'_예산초과", f"남은 예산 부족 → {fallback_note}로 진행")

    draft, md_found = strip_markdown(draft)
    if md_found:
        trace.log("마크다운_제거", "HCX가 낸 강조·제목 표기를 제거 "
                                "(평가자는 answer를 일반 텍스트로 읽는다)")
    draft, forbidden = strip_forbidden(draft)
    if forbidden:
        trace.log("금지표현_치환",
                  f"단정 표현 {forbidden}을(를) 조건부 표현으로 치환 "
                  f"(단정적 추천 금지 요건)")

    # ── 요구사항 반영 검증 ────────────────────────────────────
    unmet = verify_requirement_coverage(draft, slots, answer_covers_slot, trace=trace)
    unmet = _drop_covered_by_calc(unmet, slots, trace)
    if unmet:
        draft += ("\n\n※ " + ", ".join(s.description for s in unmet)
                  + " 관련 내용은 제공 자료로 확정하기 어려워 별도 확인이 필요합니다.")

    if constraint_warnings:
        draft += "\n\n※ " + " / ".join(constraint_warnings)

    # ⚠️ critical 함정 강제 삽입은 **여기서 하지 않는다.** REVISE→재생성→
    #    구제재생성 뒤, 정말 끝까지 반영되지 않았을 때만 돈다(맨 아래
    #    "critical 함정 교정 강제 (최후의 보루)" 참조). 이유는 그 자리의
    #    주석에 있다 — 여기서 먼저 돌면 재생성이 본문을 고칠 기회 자체를
    #    잃는다(2026-09-01 실물 확인).

    # ── 인용 조립 ─────────────────────────────────────────────
    used_evidence = _used_evidence(
        evidence, slots, query_spec,
        trap_docs=_addressed_trap_docs(draft, trap_context.get("checks")),
        fact_docs=facts_reflected_in_answer(
            draft, query_spec.get("_product_facts") or []))
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
        partial_answer_possible=partial_possible,
        fact_texts=fact_snippets(query_spec.get("_product_facts") or []))

    verdict = verify_grounding(draft, evidence)
    trace.log("L6_감독심사", verdict.as_trace() or "심사 완료")

    supervision = verdict.supervision
    if supervision is not None and supervision.revised_ask_back:
        ask_back_items = supervision.revised_ask_back[:2]

    # 감독이 지적한 문제가 끝내 해소되지 않았는가.
    # ⚠️ 이 값이 True인 채로 그냥 답변을 내보내면 안 된다 —
    #    "틀릴 수 있다는 걸 알면서 확신 있게 답하는" 상태가 되기 때문이다.
    unresolved = False
    # Sub-Agent의 루프 감지가 읽는다 — 재생성이 진전 없이 반복되는지
    regen_count = 0

    # ── REVISE → 재생성 1회 ───────────────────────────────────
    if supervision is not None and supervision.verdict == Verdict.REVISE:
        if deadline.allows(BUDGET_REGEN) and not getattr(client, "is_mock", False):
            remediation = build_remediation_prompt(supervision, draft)
            regen_count += 1
            regen_stage_name = "L4-sub" if route.is_advisory else "L5'"
            trace.log("L6_재생성",
                      f"REVISE 판정 — 시정 지시와 함께 {regen_stage_name}로 "
                      f"1회 되돌림 (재생성은 1회로 제한)")
            try:
                # ⚠️ 경로에 맞는 프롬프트를 써야 한다. ADVISORY 답변이 REVISE를
                #    맞았는데 여기서 SUPERVISOR_SYSTEM_PROMPT(L5' 전용, 계산
                #    중심 어조)로 재생성하면, 두 생성기가 "이후 검증·인용·
                #    감독은 한 벌로 공유한다"던 설계 의도가 재생성 단계에서만
                #    깨진다 — 상담형 답변이 감사에 걸리는 순간 계산형 어조로
                #    바뀐다(2026-09-03 코드 점검 F3).
                if route.is_advisory:
                    from app.generation.advisory import ADVISORY_SYSTEM_PROMPT
                    regen_system_prompt = ADVISORY_SYSTEM_PROMPT
                else:
                    from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT
                    regen_system_prompt = SUPERVISOR_SYSTEM_PROMPT
                revised = client.call(regen_system_prompt, remediation,
                                      purpose="l5_regenerate", max_tokens=1500)
            except Exception as e:
                revised = ""
                trace.log("L6_재생성_실패", f"재생성 호출 실패({e}) → 원본 답변 유지")
            if revised.strip():
                revised, _ = strip_markdown(revised)
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
                elif _is_improvement(draft, revised, recheck,
                                     trap_context.get("checks"),
                                     old_verdict=verdict):
                    # ⚠️ 완전히 통과하지는 못했지만 **원본보다 확실히 낫다.**
                    #    예전에는 이 경우도 통째로 기각해 더 나쁜 원본을
                    #    내보냈다(2026-09-02 실측 — 미해소 ['C1','C2']인
                    #    오답을 유지하고 ['C1']로 줄인 개선안을 버렸다).
                    #    판정은 완화하지 않는다 — unresolved를 True로 두어
                    #    고지·강등은 그대로 하고, 두 후보 중 덜 나쁜 쪽만 고른다.
                    draft = revised.strip()
                    verdict = recheck
                    supervision = recheck.supervision
                    unresolved = True
                    trace.log("L6_재생성_부분반영",
                              "재생성 답변이 검증을 완전히 통과하지는 못했으나 "
                              "미해소 지적이 줄었거나 근거 없는 수치가 사라져 "
                              "원본보다 낫다 → 채택하되 남은 지적은 그대로 "
                              "고지하고 등급을 낮춘다")
                else:
                    unresolved = True
                    trace.log("L6_재생성_기각",
                              "재생성 답변도 검증에 실패했고 원본보다 낫지도 "
                              "않다 → 원본을 유지하되, 검증을 통과하지 못했다는 "
                              "사실을 답변에 고지하고 답변 등급을 낮춘다")
            else:
                unresolved = True
        else:
            unresolved = True
            # ⚠️ 사유를 뭉뚱그리지 않는다. 예전 문구는 "예산이 없거나 mock
            #    모드"라 **둘 중 무엇이었는지 알 수 없었다.** 실측에서 이
            #    분기가 걸렸을 때(2026-09-02 UI-019) 남은 시간이 얼마였는지
            #    확인할 방법이 없어 예산 상수를 근거 있게 조정할 수 없었다.
            #    "법령 판정을 '못 한 것'과 '대상이 없던 것'을 구별할 것"과
            #    같은 이유다 — 사유가 없으면 고칠 수 없다.
            if getattr(client, "is_mock", False):
                why = "mock 모드에서는 재생성 경로를 열지 않는다"
            else:
                why = (f"남은 시간 {deadline.remaining:.1f}초 < 재생성 예산 "
                       f"{BUDGET_REGEN:.1f}초 (총 예산 {deadline.total:.0f}초)")
            trace.log("L6_재생성_생략",
                      f"재생성을 건너뛴다 — {why}. 검증 미통과 사실을 고지하고 "
                      f"등급을 낮춘 채로 진행")

    # ── 구제 재생성 — Sub-Agent가 직접 다시 쓴다 ──────────────
    #
    # ⚠️ 여기까지 왔다는 것은 감독이 REVISE를 냈고 L5' 재생성마저 해소하지
    #    못했다는 뜻이다. 예전에는 이 지점에서 **원본을 그대로 내보내고
    #    고지문만 붙였다** — 감독이 두 번 반려한 문장이 그대로 나갔다.
    #    고지는 정직하지만, 답변 자체가 나아지지는 않는다.
    #
    #    그래서 마지막 한 번을 Sub-Agent에게 준다. L5'와 같은 프롬프트로
    #    또 시도하면 같은 실패를 반복하기 쉬우므로, 지적사항을 정면에 놓고
    #    다시 쓰는 **다른 역할**로 접근한다(SUB_AGENT_REWRITE_PROMPT).
    #
    # ⚠️ 이 답변도 반드시 verify_grounding을 다시 통과해야 채택된다.
    #    검증을 건너뛰면 Sub-Agent가 감사를 빠져나가는 뒷문이 되고,
    #    "LLM 감사는 심각도를 올릴 수만 있다"는 단조성이 무너진다.
    #    통과하지 못하면 예전과 똑같이 원본 + 고지 + 강등으로 간다.
    rescue_wanted = (unresolved and supervision is not None
                     and supervision.verdict == Verdict.REVISE)
    if rescue_wanted and (not deadline.allows(BUDGET_SUBAGENT_REWRITE)
                          or getattr(client, "is_mock", False)):
        # ⚠️ 예전에는 이 경우가 **아무 기록도 남기지 않았다.** 구제 재생성이
        #    필요한 상황이었는데 열리지 않았다는 사실이 think_trace에서
        #    사라지면, 밖에서 보기에는 "구제를 시도했는데 실패한 것"과
        #    구별되지 않는다("법령 판정을 '못 한 것'과 '대상이 없던 것'을
        #    구별할 것"과 같은 계열의 결함이다).
        trace.log("SubAgent_구제_생략",
                  ("mock 모드에서는 구제 재생성 경로를 열지 않는다"
                   if getattr(client, "is_mock", False) else
                   f"남은 시간 {deadline.remaining:.1f}초 < 구제 재생성 예산 "
                   f"{BUDGET_SUBAGENT_REWRITE:.1f}초 → 원본 유지 + 고지"))
    if (rescue_wanted
            and deadline.allows(BUDGET_SUBAGENT_REWRITE)
            and not getattr(client, "is_mock", False)):
        from app.core.sub_agent import rescue_answer

        trace.log("SubAgent_구제재생성",
                  "L5' 재생성이 지적을 해소하지 못함 → Sub-Agent가 직접 "
                  "답변을 다시 쓴다 (결과는 다시 검증한다)")
        rescued = rescue_answer(
            question=question,
            rejected_draft=draft,
            supervision=supervision,
            calc_results=calc_results,
            evidence_texts=[c.text for c in evidence],
            llm_call=llm_call_adapter(client,
                                      purpose="subagent_rewrite",
                                      max_tokens=1500))

        if rescued:
            rescued, _ = strip_markdown(rescued)
            rescued, _ = strip_forbidden(rescued)
            recheck = verify_grounding(rescued, evidence)
            if recheck:
                draft = rescued.strip()
                verdict = recheck
                supervision = recheck.supervision
                unresolved = False
                trace.log("SubAgent_구제_반영",
                          "Sub-Agent가 다시 쓴 답변이 검증을 통과해 채택 "
                          "(판정도 갱신)")
            elif _is_improvement(draft, rescued, recheck,
                                 trap_context.get("checks"),
                                 old_verdict=verdict):
                # L5' 재생성과 같은 원칙 — 완전히 통과하지 못했어도 원본보다
                # 확실히 나으면 채택한다. unresolved는 True로 남겨 고지·강등을
                # 유지하므로 사용자에게 "완전히 검증되지 않았다"는 사실은
                # 그대로 전달된다.
                draft = rescued.strip()
                verdict = recheck
                supervision = recheck.supervision
                trace.log("SubAgent_구제_부분반영",
                          "Sub-Agent 답변이 검증을 완전히 통과하지는 못했으나 "
                          "미해소 지적이 줄었거나 근거 없는 수치가 사라져 "
                          "원본보다 낫다 → 채택하되 남은 지적은 그대로 고지한다")
            else:
                trace.log("SubAgent_구제_기각",
                          "Sub-Agent 답변도 검증에 실패했고 원본보다 낫지도 "
                          "않다 → 원본을 유지하고 검증 미통과 사실을 고지한다")
        else:
            trace.log("SubAgent_구제_실패",
                      "Sub-Agent 재생성 호출이 답변을 만들지 못함 → 원본 유지")

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

    # ── 상품명 접지 검사 ──────────────────────────────────────
    #
    # ⚠️ 근거에 없는 상품명은 **근거에 없는 수치와 같은 종류의 사고**이므로
    #    같은 자리에서 같은 방식으로 처리한다. 실물에서 확인됐다
    #    (2026-09-01) — 근거 문서가 0건인데 답변이 실존 펀드명을 콕 집어
    #    추천했다. 과제 자료의 "근거 문서 고정" 원칙 위반이고, 채점의
    #    'Hallucination 방지' 항목에 직접 걸린다.
    #
    #    기존 상품 감사(audit_fitness)는 이걸 잡을 수 없다. 그 감사는
    #    검색 후보와 답변의 **교집합**만 보는데, 근거가 0건이면 후보가 비어
    #    지어낸 이름은 검사 대상에 오르지도 않는다.
    #
    #    REVISE(재생성)가 아니라 축퇴를 고르는 이유: 근거가 없다는 것은
    #    확정 사실이므로 다시 써도 그 상품을 말할 근거는 생기지 않는다.
    #    상품 얘기를 뺀 답변이 정답이고, 그것이 render_template_answer다.
    product_check = verify_product_grounding(draft, [c.text for c in evidence])
    if not product_check["passed"]:
        trace.log("상품명_접지_실패",
                  f"{product_check['reason']} → 근거 없는 상품 언급을 제거한 "
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

    # ── critical 함정 교정 강제 (최후의 보루) ─────────────────
    #
    # ⚠️ 2026-09-01 실물 확인 — 예전에는 이 블록이 **REVISE→재생성→
    #    구제재생성보다 앞**(요구사항 반영 검증 직후)에서 돌았다. 그러면
    #    강제 삽입이 만든 각주 하나로 `unaddressed_traps`가 즉시 '해소'로
    #    보고, critical 함정의 TRAP_UNADDRESSED가 REVISE를 낼 이유
    #    자체가 사라졌다 — 재생성이 본문을 실제로 고칠 기회를 얻지
    #    못하고 매번 건너뛰어졌다.
    #
    #    실제로 이렇게 나갔다: "IRP 퇴직금 3억, 1500만원 넘으면
    #    종합과세?"에 각주("이연퇴직소득은 1,500만원 계산에 포함되지
    #    않습니다")는 붙었지만, 본문은 "1,500만원 이하로 조절하는 게
    #    중요합니다"라는 **반대** 조언을 그대로 유지했다. L6은 각주
    #    덕분에 TRAP_UNADDRESSED가 이미 꺼진 채로 감사해 "지적사항
    #    없음"으로 승인했다 — 각주와 본문이 서로 모순인 채로 나갔다.
    #
    #    지금은 REVISE→재생성→구제재생성이 **먼저** 시도된다. 그 두
    #    경로는 이미 시정 지시(directive)에 이 함정의 correction 문구를
    #    그대로 담아 LLM에 넘기므로(build_remediation_prompt ·
    #    build_rewrite_payload), 각주를 억지로 붙이는 것보다 본문 자체를
    #    일관되게 고칠 기회를 얻는다. 그래도 끝내 반영되지 않은 critical
    #    함정만 지금 이 자리에서 결정론적으로 덧붙인다 — 원래 이 함수가
    #    설계된 의도 그대로다(아래 docstring 참조).
    draft = _enforce_critical_traps(draft, trap_context.get("checks"), trace)

    # ⚠️ 위에서 방금 덧붙인 교정문의 근거 문서가 있다면 인용에도 반영한다.
    #    시점을 옮기고 여기서 다시 계산하지 않으면 "본문에는 답이 나갔는데
    #    그 답의 근거는 retrieved_context에 없는" 상태가 된다 — 근거를
    #    빠짐없이 싣는다는 원칙(CLAUDE.md "사용한 것만 인용")에 어긋난다.
    #    이미 자연스럽게 반영돼 바뀐 것이 없으면(대다수의 경우) 여기서
    #    다시 계산해도 결과가 같으므로 비용은 없다.
    used_evidence = _used_evidence(
        evidence, slots, query_spec,
        trap_docs=_addressed_trap_docs(draft, trap_context.get("checks")),
        fact_docs=facts_reflected_in_answer(
            draft, query_spec.get("_product_facts") or []))
    citations = build_citations(used_evidence, calc_results,
                                doc_meta=store.doc_meta_map(),
                                external_sources=external,
                                legacy_checker=detect_legacy_tax_content)

    # ── 인용 무결성 ───────────────────────────────────────────
    integrity = verify_citation_integrity(
        draft, citations, slots_used=[s.description for s in slots
                                      if s.status != SlotStatus.MISSING])
    # ⚠️ 이 검사의 결과는 **등급에 반영되지 않는다.** 남은 항목(인용 미연결 ·
    #    구법 문서 인용 · 외부자료 미고지)은 사람이 보고 판단할 참고 정보이지,
    #    답변을 반려할 근거가 아니기 때문이다. 답변을 실제로 막는 판정은
    #    수치 검증(verify_numeric_grounding)과 상품명 접지
    #    (verify_product_grounding)가 각각 위에서 이미 수행했다.
    #    "감사가 있다는 주장은 결과가 반영될 때만 참"이므로, 반영하지 않는
    #    검사는 반영하지 않는다고 적어 둔다.
    trace.log("인용_무결성", integrity["trace"] + " (참고 정보 — 등급 미반영)")

    # ── Sub-Agent · 전 구간 로직 건전성 ───────────────────────
    #
    # ⚠️ **기본 로직이 우선이다.** 이상이 감지되지 않으면 호출조차 하지
    #    않는다. 잘 도는 것을 굳이 들여다보면 결정론적 계층이 확보한
    #    재현성을 LLM 재량이 갉아먹는다.
    #    개입 판정은 결정론적 코드(detect_anomalies)가 한다 — LLM이 스스로
    #    "이상한 것 같다"고 나서는 경로는 없다.
    #    예산이 없으면 감지만 하고 넘어간다. 보조 장치가 본체를 지연시키면
    #    그 자체가 결함이다.
    health = supervise_logic(
        trace_entries=trace.entries(),
        answer=draft,
        question=question,
        supervision=supervision,
        regeneration_count=regen_count,
        answerability=decision.value,
        llm_call=(llm_call_adapter(client, purpose="subagent_diagnosis")
                  if deadline.allows(BUDGET_SUBAGENT) else None))
    if not health.healthy:
        trace.log("SubAgent_건전성", health.as_trace())
    else:
        trace.log("SubAgent_건전성", "로직 건전성 정상 — 개입 없음")

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

def _drop_covered_by_calc(unmet: list, slots: list,
                          trace: TraceLogger) -> list:
    """같은 주제의 계산이 성공한 사실 슬롯은 '확정 불가' 고지에서 뺀다.

    ━━ 왜 필요한가 (2026-09-04 실서버 확인) ━━
    "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?"에
    답변이 1,200만원을 정확히 산출·단정해 놓고 맨 아래에 이렇게 붙였다:

        ※ 연금수령한도 **산정 방식** 관련 내용은 제공 자료로 확정하기
          어려워 별도 확인이 필요합니다.

    사실과 다르다. `연금수령한도_계산`이 limit=1200.0, denominator=10,
    **source=doc39**을 냈다 — 산정 방식은 제공 자료에 근거해 이미 산출됐다.

    원인은 `verify_requirement_coverage`가 "LLM 문장이 이 슬롯을 설명했는가"
    만 보고, 같은 TopicRule에서 쌍으로 생긴 계산 슬롯이 이미 성공했는지는
    보지 않는 것이다. 슬롯은 `{base}_fact` · `{base}_calc`로 만들어지므로
    (query_spec.rule_based_spec), base가 같고 계산이 CALC_DONE이면 그
    주제는 확정된 것이다.

    ⚠️ 고지를 무르는 것이 아니다. 계산이 **실패했거나 없는** 주제의 고지는
       그대로 남는다 — 없앤 것은 **사실이 아닌 고지**뿐이다. 나이·수령방식에
       따라 과세율이 갈린다는 다른 고지들도 이 함수와 무관하게 유지된다.
    """
    if not unmet:
        return unmet
    done_bases = {
        s.slot_id[:-len("_calc")]
        for s in slots
        if s.status == SlotStatus.CALC_DONE and s.slot_id.endswith("_calc")
    }
    if not done_bases:
        return unmet

    kept, dropped = [], []
    for s in unmet:
        base = (s.slot_id[:-len("_fact")]
                if s.slot_id.endswith("_fact") else None)
        if base and base in done_bases:
            dropped.append(s)
        else:
            kept.append(s)

    if dropped:
        trace.log("한계고지_교정",
                  f"계산이 성공한 주제 {len(dropped)}건을 '확정 불가' 고지에서 "
                  f"제외 (계산 결과가 근거 문서를 출처로 이미 산출함)",
                  slots=[s.slot_id for s in dropped])
    return kept


def _enforce_critical_traps(draft: str, trap_checks: Optional[list[dict]],
                            trace: TraceLogger) -> str:
    """critical 함정이 끝내 반영되지 않으면 교정 문장을 직접 덧붙인다.

    ━━ 왜 권고로는 부족한가 ━━
    함정 감지도, 검증도, REVISE 지시도 정확히 동작한다. 그런데 L5'가 재생성
    후에도 빠뜨리면 등급만 낮추고 그대로 나갔다. 그 결과 사용자는 틀린 전제
    ("11년이면 40% 감면")를 교정받지 못한 답을 받는다(평가 E-08·E-09).

    설계 원칙에 어긋나지 않는다 — 교정 문장은 규칙(trap_rules)이 이미 갖고
    있고, 판단도 결정론적이다. LLM 재량에 맡기지 않는 것이 원래 방침이다.

    high 이하는 강제하지 않는다. 전부 끼워 넣으면 답변이 경고문 더미가 된다.
    """
    remaining = [t for t in unaddressed_traps(draft, trap_checks or [])
                 if t.get("severity") == "critical" and t.get("correction")]
    if not remaining:
        return draft
    trace.log("함정교정_강제삽입",
              f"critical 함정 {[t['id'] for t in remaining]}이(가) 재생성 후에도 "
              f"반영되지 않아 규칙이 보유한 교정 문장을 직접 덧붙였다",
              traps=[t["id"] for t in remaining])
    return draft + "\n\n" + "\n".join(f"※ {t['correction']}" for t in remaining)


def _addressed_trap_docs(answer: str, trap_checks: list[dict]) -> dict[str, str]:
    """답변이 실제로 반영한 함정의 근거 문서. {doc_id: 무엇을 뒷받침했는지}

    ━━ 왜 필요한가 ━━
    함정은 요구사항 슬롯이 아니라서 슬롯-근거 매칭에 잡히지 않는다. 그래서
    검색도 되고 핀 고정도 됐는데 **인용 단계에서만** 탈락했다
    (평가 L-01/doc55 · E-19/doc20 · E-26/doc40 이 3회 연속 실패).
    임베딩을 켜서 검색을 개선해도 같은 문서가 빠진 것이 검색 문제가 아님을
    증명했다.

    판정 기준은 새로 만들지 않는다 — unaddressed_traps()가 이미 '해소됐는가'를
    안다. 그 여집합이 곧 '답변이 반영한 함정'이다.
    """
    if not trap_checks:
        return {}
    unresolved = {t["id"] for t in unaddressed_traps(answer, trap_checks)}
    out: dict[str, str] = {}
    for c in trap_checks:
        if c.get("id") in unresolved:
            continue
        for did in c.get("docs") or ():
            out.setdefault(did, c.get("title") or "함정 교정 근거")
    return out


def _used_evidence(evidence: list[EvidenceChunk],
                   slots: list[RequirementSlot],
                   query_spec: Optional[dict] = None,
                   trap_docs: Optional[dict[str, str]] = None,
                   fact_docs: Optional[dict[str, str]] = None) -> list[dict]:
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

    # 답변이 반영한 함정의 근거 문서 — 실제로 답변을 형성했으므로 인용한다
    for did, why in (trap_docs or {}).items():
        supports = used_ids.setdefault(did, [])
        if why not in supports:
            supports.append(why)

    # 답변이 반영한 상품 팩트의 근거 문서 — 위와 같은 이유다.
    # 팩트 블록은 슬롯 매핑을 거치지 않고 생성 프롬프트에 실리므로, 여기서
    # 되살리지 않으면 "답변에는 위험등급 4등급이 있는데 retrieved_context는
    # 근거 문서 없음"이라는 상태가 만들어진다(2026-09-05 배선 테스트로 확인).
    for did, why in (fact_docs or {}).items():
        supports = used_ids.setdefault(did, [])
        if why not in supports:
            supports.append(why)

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
                     evidence: list[EvidenceChunk], trace: TraceLogger) -> dict:
    """순수 거절 응답. 연결(bridge)은 L0 직후에 이미 판단했다."""
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


def _numeric_passed(verdict) -> bool:
    """수치 검증을 통과했는가. 검증기가 돌지 않았으면(None) 통과로 본다."""
    numeric = getattr(verdict, "numeric", None)
    return True if numeric is None else bool(numeric.passed)


def _is_improvement(old_draft: str, new_draft: str,
                    new_verdict, trap_checks: Optional[list[dict]],
                    old_verdict=None) -> bool:
    """재생성 결과가 원본보다 **확실히 나은가** (결정론적 판정).

    ━━ 왜 필요한가 (2026-09-02 실측 확인) ━━
    예전에는 재생성 결과를 "완전히 통과했는가"로만 채택했다(all-or-nothing).
    그래서 **명백히 개선된 답변이 통째로 버려지고 더 나쁜 원본이 나갔다.**
    실측 재현:

        원본   : 미해소 함정 ['C1', 'C2']  ("1,500만원 이하로 조절하세요" — 오답)
        재생성 : 미해소 함정 ['C1']        (C2를 정확히 반영한 개선된 답변)
        결과   : C1이 남았다는 이유로 재생성 기각 → **원본(오답)이 최종 답변**

    "확실히 통과하지 못한 답변은 재생성으로 품질을 높인다"는 설계 의도가
    정확히 반대로 동작한 것이다. 완벽하지 않다고 더 나쁜 것을 고르면 안 된다.

    ━━ 개선의 종류는 둘이다 (2026-09-02 2차 보강) ━━
    처음에는 **함정 해소**만 개선으로 셌는데, 그러면 실측에서 관측된 다른
    개선 유형을 통째로 버린다:

        원본   : 근거 없는 수치 [56.0] 포함 → 수치검증 실패
        재생성 : 그 수치를 제거 (함정 미해소 집합은 그대로)
        결과   : new_missed < old_missed 가 아니므로 **기각** →
                 원본이 유지되고, 수치검증 실패 때문에 곧바로
                 **템플릿 축퇴**(근거 발췌 나열)로 떨어진다

    수치 검증 실패는 `verify_grounding` 직후 무조건 축퇴를 부르므로,
    "수치를 고친 재생성"은 축퇴를 피할 유일한 기회다. 그것을 기각하는 것은
    재생성을 넣은 목적과 정반대다. 그래서 판정을 둘로 나눈다:

      ① 미해소 함정이 **엄격히 줄었다** (진부분집합) — 수치는 나빠지지 않았다
      ② 수치 검증이 **실패 → 통과**로 바뀌었다 — 함정은 나빠지지 않았다

    ━━ 무엇을 완화하지 않는가 ━━
    · **새 답변의 수치 검증 실패는 어떤 경우에도 개선이 아니다.** 근거 없는
      수치가 들어간 답변은 '개선'이 아니라 날조 위험이다 — 함정을 몇 개 더
      반영했든 상관없이 즉시 False다.
    · 두 축 모두 **한쪽이 나아지는 동안 다른 쪽이 나빠지면 안 된다.** 하나
      고치고 하나 깨뜨린 답변을 채택하면 품질이 단조 증가하지 않는다.

    ━━ 단조성과 충돌하지 않는다 ━━
    감사 판정을 **완화하는 것이 아니다.** 판정은 그대로 REVISE로 남고
    (unresolved=True, 고지·강등 그대로), 두 후보 중 **덜 나쁜 쪽을 고르는**
    것뿐이다. 사용자에게는 여전히 "검증을 완전히 통과하지 못했다"고 알린다.
    """
    if not _numeric_passed(new_verdict):
        return False           # 근거 없는 수치 — 개선이 아니라 사고다

    if trap_checks:
        old_missed = {t["id"] for t in unaddressed_traps(old_draft, trap_checks)}
        new_missed = {t["id"] for t in unaddressed_traps(new_draft, trap_checks)}
    else:
        # 비교 기준이 없으면 "함정이 줄었다"고 주장하지 않는다. 다만 수치
        # 개선(②)까지 막을 이유는 없으므로 '동률'로 두고 아래에서 판단한다.
        old_missed = new_missed = frozenset()

    # ① 함정이 엄격히 줄었다 — 줄었고, 새로 깨진 것이 없다
    if new_missed < old_missed:
        return True

    # ② 수치 검증이 실패 → 통과 — 축퇴를 피할 유일한 기회다.
    #    (old_verdict가 없으면 비교 대상이 없으므로 주장하지 않는다)
    if old_verdict is not None and not _numeric_passed(old_verdict):
        return new_missed <= old_missed      # 함정이 나빠지지 않았을 때만

    return False


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
