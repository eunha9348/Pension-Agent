"""
연금 Agent - 답변 커버리지 검증 파이프라인
=========================================
기존 4레이어(가드레일 → 질의분석 → 하이브리드검색 → 답변검증) 사이에 끼워넣는
"근거 완전성 / 요구사항 충족 / 추론 논리성" 3종 보완 모듈.

설계 원칙
---------
1. 계산은 결정론적 함수만 (LLM이 숫자를 만들지 않는다) → CALC_REGISTRY 플러그인
2. 평가 API는 단일 GET 요청-응답으로 완결돼야 한다 (세션/멀티턴 없음)
   → 모든 단계가 하나의 동기 호출 안에서 타임아웃 가드와 함께 실행됨
3. think_trace는 "무엇을 했는지"가 아니라 "왜 그렇게 판단했는지"를 남긴다
   → 평가지표 '추론 논리성'을 사람이 읽고 검증 가능하게

팀원 연동 방법
--------------
SNU 수학과 팀원분이 만든 결정론적 계산 함수를 받으면:
    CALC_REGISTRY["세액공제_한도_계산"] = teammate_module.calc_tax_credit_limit
한 줄만 추가하면, slot_type="calculation"인 요구사항이 자동으로 그 함수로 라우팅됩니다.
함수 시그니처 컨벤션만 맞춰주시면 됩니다: fn(**params) -> dict | float | str
"""

from __future__ import annotations

import time
import concurrent.futures as cf
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.core.pension_calc_functions import PENSION_CALC_FUNCTIONS


# ════════════════════════════════════════════════════════════════
# 1. 데이터 구조
# ════════════════════════════════════════════════════════════════

class SlotStatus(str, Enum):
    COVERED = "covered"              # 근거/계산으로 충족됨
    MISSING = "missing"              # 근거 없음 → ASK_BACK 후보
    CALC_PENDING = "calc_pending"    # 결정론적 계산 대기
    CALC_DONE = "calc_done"          # 계산 완료


class Answerability(str, Enum):
    ANSWER = "ANSWER"
    PARTIAL = "PARTIAL"
    ASK_BACK = "ASK_BACK"
    REFUSE = "REFUSE"


@dataclass
class RequirementSlot:
    """질문이 명시적으로 요구한 답변 구성요소 하나.
    예) "연금저축이랑 IRP에 넣으면 세액공제 얼마까지 되나요? 다 합쳐서요."
        → slots = [
            RequirementSlot("seaek_hando_yeongeumjeochuk", "연금저축 단독 한도", "fact"),
            RequirementSlot("seaek_hando_irp", "IRP 단독 한도", "fact"),
            RequirementSlot("seaek_hando_hapsan", "합산 한도", "calculation",
                             calc_function="세액공제_합산한도_계산"),
        ]
    """
    slot_id: str
    description: str
    slot_type: str  # "fact" | "calculation" | "comparison"
    required: bool = True
    status: SlotStatus = SlotStatus.MISSING
    evidence_ids: list[str] = field(default_factory=list)
    calc_function: Optional[str] = None
    calc_result: Optional[Any] = None


@dataclass
class EvidenceChunk:
    doc_id: str
    text: str
    # 인제스트 단계에서 미리 태깅해두는 것을 권장 (조항번호, 상품유형, 상품코드 등)
    entities: dict[str, str] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class ThinkTraceStep:
    """추론 논리성 평가를 통과하려면 '무엇을 했다'가 아니라
    '왜 그 판단을 했다'가 남아야 함."""
    step: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: Optional[float] = None


class TraceLogger:
    def __init__(self):
        self._steps: list[ThinkTraceStep] = []
        self._t0 = time.time()

    def log(self, step: str, reason: str, **detail):
        self._steps.append(ThinkTraceStep(
            step=step, reason=reason, detail=detail,
            elapsed_ms=round((time.time() - self._t0) * 1000, 1),
        ))

    def as_text(self) -> str:
        """API 응답의 think_trace 필드(string)로 직렬화.
        사람이 순서대로 읽었을 때 판단 흐름이 그대로 이해되도록 문장형으로."""
        lines = []
        for s in self._steps:
            lines.append(f"[{s.elapsed_ms}ms] {s.step} — {s.reason}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 2. 근거 완전성 — 무관한 근거 걸러내기 (negative filtering)
# ════════════════════════════════════════════════════════════════

# 도메인 엔티티 타입 화이트리스트 (질의분석 Layer 2의 function-calling 스키마와 동일해야 함)
ENTITY_KEYS = ["plan_type", "product_code", "product_name", "clause_no", "tax_year"]


def _entity_conflict(query_entities: dict, chunk_entities: dict) -> bool:
    """같은 키에 서로 다른 값이 있으면 '비교 의도'가 아닌 이상 스코프 불일치로 간주.
    예) 질의: product_name=솔로몬국공채단기 / 근거: product_name=솔로몬국공채장기 → 충돌"""
    for key in ENTITY_KEYS:
        qv, cv = query_entities.get(key), chunk_entities.get(key)
        if qv and cv and qv != cv:
            return True
    return False


def filter_irrelevant_evidence(
    chunks: list[EvidenceChunk],
    query_spec: dict,
    score_threshold: float = 0.35,
    trace: Optional[TraceLogger] = None,
) -> list[EvidenceChunk]:
    """검색된 근거 중 '의미는 비슷하지만 대상이 다른' 청크를 제거.
    비교 질의(compare intent)일 때는 대상이 여러 개인 게 정상이므로 예외 처리."""
    is_comparison = query_spec.get("intent") == "상품_비교"
    query_entities = query_spec.get("entities", {})

    kept, dropped = [], []
    for c in chunks:
        if c.score < score_threshold:
            dropped.append((c, "유사도 임계값 미달"))
            continue
        if not is_comparison and _entity_conflict(query_entities, c.entities):
            dropped.append((c, f"엔티티 불일치 {c.entities}"))
            continue
        kept.append(c)

    if trace:
        trace.log(
            "근거_필터링",
            f"{len(chunks)}건 검색 → {len(kept)}건 유효 (제외 {len(dropped)}건)",
            dropped_reasons=[d[1] for d in dropped[:5]],  # 로그 과다 방지, 상위 5건만
        )
    return kept


# ════════════════════════════════════════════════════════════════
# 3. 요구사항 충족 — 슬롯 추출 & 커버리지 매핑
# ════════════════════════════════════════════════════════════════

def extract_required_slots(query_spec: dict) -> list[RequirementSlot]:
    """query_spec["asked_for"]는 Layer 2 function-calling 결과에서 이미 뽑혀 나온다는 전제.
    여기서는 그 리스트를 슬롯 객체로 변환하고, calc가 필요한 항목은 calc_function을 매핑.

    TODO(연동 포인트): asked_for → slot_type/calc_function 매핑 테이블은
    도메인 사전(세제/제도/상품) 화이트리스트로 별도 관리 권장 (하드코딩 지양).
    """
    slots = []
    for item in query_spec.get("asked_for", []):
        slot_type = item.get("type", "fact")
        slots.append(RequirementSlot(
            slot_id=item["id"],
            description=item.get("description", item["id"]),
            slot_type=slot_type,
            required=item.get("required", True),
            calc_function=item.get("calc_function") if slot_type == "calculation" else None,
        ))
    return slots


def map_evidence_to_slots(
    slots: list[RequirementSlot],
    evidence: list[EvidenceChunk],
    slot_evidence_matcher: Callable[[RequirementSlot, EvidenceChunk], bool],
    trace: Optional[TraceLogger] = None,
) -> list[RequirementSlot]:
    """각 슬롯에 근거를 매핑. slot_evidence_matcher는 실제로는 경량 LLM 호출이나
    키워드/엔티티 매칭으로 구현 (여기서는 주입받는 함수로 분리 — 테스트 용이성 위해).
    calculation 타입은 근거 매핑 대신 CALC_PENDING으로 표시."""
    for slot in slots:
        if slot.slot_type == "calculation":
            slot.status = SlotStatus.CALC_PENDING
            continue
        matched = [c.doc_id for c in evidence if slot_evidence_matcher(slot, c)]
        if matched:
            slot.status = SlotStatus.COVERED
            slot.evidence_ids = matched
        else:
            slot.status = SlotStatus.MISSING

    if trace:
        missing = [s.slot_id for s in slots if s.status == SlotStatus.MISSING and s.required]
        trace.log(
            "요구사항_커버리지_체크",
            f"필수 슬롯 {sum(s.required for s in slots)}건 중 미충족 {len(missing)}건",
            missing_slots=missing,
        )
    return slots


def decide_answerability(slots: list[RequirementSlot], trace: Optional[TraceLogger] = None) -> Answerability:
    """결정론적 규칙 — LLM에게 위임하지 않음 (기존 원칙 유지).
    필수 슬롯 전부 MISSING → ASK_BACK, 일부만 MISSING → PARTIAL, 전부 충족 → ANSWER."""
    required = [s for s in slots if s.required]
    missing = [s for s in required if s.status == SlotStatus.MISSING]

    if not required:
        result = Answerability.ANSWER
        reason = "필수 요구사항 없음 (일반 설명형 질의)"
    elif len(missing) == len(required):
        result = Answerability.ASK_BACK
        reason = f"필수 요구사항 {len(required)}건 전부 근거 없음 → 확인 필요한 조건 역질문"
    elif missing:
        result = Answerability.PARTIAL
        reason = f"필수 요구사항 {len(required)}건 중 {len(missing)}건 근거 없음 → 부분 답변 + 한계 고지"
    else:
        result = Answerability.ANSWER
        reason = "필수 요구사항 전부 근거 확보"

    if trace:
        trace.log("답변가능성_판정", reason, decision=result.value,
                   missing_slot_ids=[s.slot_id for s in missing])
    return result


def verify_requirement_coverage(
    draft_answer: str,
    slots: list[RequirementSlot],
    answer_covers_slot: Callable[[str, RequirementSlot], bool],
    trace: Optional[TraceLogger] = None,
) -> list[RequirementSlot]:
    """LLM이 생성한 초안이 실제로 모든 필수 슬롯을 답변에 반영했는지 최종 검증.
    (근거는 있었는데 생성 과정에서 누락되는 경우를 잡기 위한 이중 체크)"""
    unmet = [s for s in slots if s.required and s.status != SlotStatus.MISSING
             and not answer_covers_slot(draft_answer, s)]
    if trace:
        trace.log(
            "요구사항_반영_검증",
            f"근거는 있으나 답변에서 누락된 항목 {len(unmet)}건" if unmet else "필수 요구사항 전부 답변에 반영됨",
            unmet_slot_ids=[s.slot_id for s in unmet],
        )
    return unmet


# ════════════════════════════════════════════════════════════════
# 4. 결정론적 계산 함수 플러그인 — 팀원 함수 연결 지점
# ════════════════════════════════════════════════════════════════

CALC_REGISTRY: dict[str, Callable[..., Any]] = {
    **PENSION_CALC_FUNCTIONS,
    # 국민연금_본인부담금_계산 / 출산크레딧_인정개월_계산 / 국민연금_수령액_계산
    # 사적연금_납입한도_세액공제_계산 / 사적연금_원천징수_계산 / 과세방식_판정_계산
    # → pension_calc_functions.py 참고. 추가 함수는 여기 계속 등록.
}


def run_calculations(
    slots: list[RequirementSlot],
    calc_params_builder: Callable[[RequirementSlot], dict],
    trace: Optional[TraceLogger] = None,
) -> list[RequirementSlot]:
    """CALC_PENDING 슬롯을 등록된 결정론적 함수로 계산. LLM은 이 결과를
    '설명'만 하고 숫자를 새로 만들지 않음 — 기존 원칙 그대로 강제."""
    for slot in slots:
        if slot.status != SlotStatus.CALC_PENDING:
            continue
        fn = CALC_REGISTRY.get(slot.calc_function or "")
        if fn is None:
            slot.status = SlotStatus.MISSING
            if trace:
                trace.log(
                    "계산함수_미등록",
                    f"'{slot.calc_function}' 함수가 CALC_REGISTRY에 없어 계산 불가 → 한계 고지 대상",
                    slot_id=slot.slot_id,
                )
            continue

        params = calc_params_builder(slot)
        try:
            result = fn(**params)
            slot.calc_result = result
            slot.status = SlotStatus.CALC_DONE
            if trace:
                trace.log(
                    "결정론적_계산_실행",
                    f"'{slot.description}' 계산 완료",
                    slot_id=slot.slot_id, function=slot.calc_function,
                    inputs=params, output=result,
                )
        except Exception as e:
            slot.status = SlotStatus.MISSING
            if trace:
                trace.log(
                    "결정론적_계산_실패",
                    f"'{slot.description}' 계산 중 오류: {e} → 한계 고지 대상",
                    slot_id=slot.slot_id, inputs=params,
                )
    return slots


# ════════════════════════════════════════════════════════════════
# 5. 단일 동기 요청 안에서의 타임아웃 가드
# ════════════════════════════════════════════════════════════════

def run_with_timeout(fn: Callable, args: tuple = (), kwargs: dict = None,
                      timeout_sec: float = 3.0, fallback: Any = None,
                      trace: Optional[TraceLogger] = None, step_name: str = "단계"):
    """평가 API가 단일 GET 요청-응답으로 완결돼야 하므로, 각 내부 단계에
    타임아웃을 걸고 초과 시 폴백으로 진행 (전체 요청이 죽는 것보다 낫다)."""
    kwargs = kwargs or {}
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=timeout_sec)
            if trace:
                trace.log(f"{step_name}_완료", "정상 처리")
            return result
        except cf.TimeoutError:
            if trace:
                trace.log(f"{step_name}_타임아웃",
                           f"{timeout_sec}초 초과 → 폴백으로 진행 (해당 단계 결과 없이 계속)")
            return fallback


# ════════════════════════════════════════════════════════════════
# 6. 오케스트레이션 스켈레톤 (실제 검색/생성 함수는 기존 구현체와 연결)
# ════════════════════════════════════════════════════════════════

def build_answer(question_id: str, question: str,
                  # 아래 5개는 기존 pension-agent 구현체의 실제 함수로 주입
                  extract_query_spec: Callable[[str], dict],
                  retrieve_hybrid: Callable[[dict], list[EvidenceChunk]],
                  slot_evidence_matcher: Callable[[RequirementSlot, EvidenceChunk], bool],
                  calc_params_builder: Callable[[RequirementSlot], dict],
                  generate_answer: Callable[[dict, list[EvidenceChunk], list[RequirementSlot]], str],
                  answer_covers_slot: Callable[[str, RequirementSlot], bool],
                  verify_grounding: Callable[[str, list[EvidenceChunk]], bool],
                  ) -> dict:
    """평가용 API 응답 스키마( question_id, question, retrieved_context,
    think_trace, answer )를 그대로 채워서 리턴."""
    trace = TraceLogger()

    # 1. 질의 분석
    query_spec = run_with_timeout(extract_query_spec, args=(question,), timeout_sec=3.0,
                                   fallback={}, trace=trace, step_name="질의분석")
    slots = extract_required_slots(query_spec)
    trace.log("요구사항_슬롯_추출", f"{len(slots)}건의 필수 요구사항 식별",
              slot_ids=[s.slot_id for s in slots])

    # 2. 검색 + 근거 필터링
    raw_evidence = run_with_timeout(retrieve_hybrid, args=(query_spec,), timeout_sec=4.0,
                                     fallback=[], trace=trace, step_name="하이브리드검색")
    evidence = filter_irrelevant_evidence(raw_evidence, query_spec, trace=trace)

    # 3. 요구사항-근거 매핑
    slots = map_evidence_to_slots(slots, evidence, slot_evidence_matcher, trace=trace)

    # 4. 결정론적 계산 (있는 경우만)
    slots = run_calculations(slots, calc_params_builder, trace=trace)

    # 5. 답변가능성 최종 판정
    decision = decide_answerability(slots, trace=trace)
    if decision == Answerability.REFUSE:
        answer = "죄송하지만 제공된 자료 범위에서는 답변드리기 어렵습니다."
    elif decision == Answerability.ASK_BACK:
        missing_desc = ", ".join(s.description for s in slots if s.status == SlotStatus.MISSING)
        answer = f"정확한 안내를 위해 다음을 확인하고 싶어요: {missing_desc}"
    else:
        # 6. 답변 생성
        draft = run_with_timeout(generate_answer, args=(query_spec, evidence, slots),
                                  timeout_sec=6.0, fallback="", trace=trace, step_name="답변생성")

        # 7. 요구사항 반영 검증 (근거는 있는데 생성 시 누락된 경우 재확인)
        unmet = verify_requirement_coverage(draft, slots, answer_covers_slot, trace=trace)
        if unmet:
            trace.log("한계_고지_추가",
                       f"{[s.description for s in unmet]}는 답변에서 다루지 못해 한계 고지 문구 추가")
            draft += "\n\n※ " + ", ".join(s.description for s in unmet) + " 관련 정보는 제공 자료로 확정하기 어려워 별도 확인이 필요합니다."

        # 8. 근거기반 검증 (hallucination 체크)
        grounded = run_with_timeout(verify_grounding, args=(draft, evidence), timeout_sec=2.0,
                                     fallback=True, trace=trace, step_name="근거검증")
        if not grounded:
            trace.log("근거검증_실패", "생성 답변에 근거로 뒷받침되지 않는 문장 감지 → 보수적 재작성 필요 (재생성 루프 or 보류 처리)")
        answer = draft

    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": "\n---\n".join(f"[{c.doc_id}] {c.text}" for c in evidence),
        "think_trace": trace.as_text(),
        "answer": answer,
    }
