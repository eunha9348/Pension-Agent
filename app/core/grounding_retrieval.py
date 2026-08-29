"""
L0 · 사전 검색 (Grounding Retrieval)
=====================================
질의 분석(L1)을 문서에 접지(grounding)시키기 위한 저비용 개략 검색.

왜 필요한가
-----------
L1이 질의만 보고 요구사항 슬롯과 호출할 계산함수를 정하면,
그 판단의 근거는 **모델의 내부 지식**이 된다.

그런데 이 도메인에서 모델 내부 지식은 신뢰할 수 없다.
세법이 개정됐고(700만원→900만원, 1200만원→1500만원), 모델이 구법을 알고 있으면
**슬롯 자체가 틀리게 추출된다.** 검색은 그 뒤에 오므로 이미 늦다.

L0는 L1 이전에 문서 landscape를 훑어, 분석이 실제 문서 용어와 범위 위에서
이뤄지도록 만든다.

━━ 절대 원칙 · L0 결과는 답변 근거가 아니다 ━━
L0는 Exploitation 필터(구법탐지·엔티티충돌·가입자격·하드제약)를 거치지 않았다.
따라서 L0가 찾은 문서를 답변 인용에 쓰면 "사용한 것만 인용" 원칙이 무너지고,
검증되지 않은 근거가 답변에 섞인다.

  L0 산출물 용도 : 질의 분석 접지, 도메인 커버리지 판정, 조기 거절
  L0 산출물 금지 : 답변 인용, retrieved_context 기재, 계산 파라미터 공급

정밀 근거는 반드시 L3(Exploration) → L4(Exploitation)를 거친 것만 사용한다.

━━ 비용 ━━
LLM 호출 없음. BM25/키워드 스캔 + 메타데이터 조회만 사용한다.
따라서 이 계층을 추가해도 LLM 호출 횟수는 늘지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional


# ════════════════════════════════════════════════════════════════
# 도메인 커버리지 판정
# ════════════════════════════════════════════════════════════════

# 제공 자료가 다루는 영역. 여기 걸리지 않으면 도메인 밖일 가능성이 높다.
DOMAIN_AREAS = {
    "제도": ["퇴직연금", "DB", "DC", "IRP", "확정급여", "확정기여", "개인형",
             "연금저축", "제도전환", "가입자교육", "중도인출", "담보대출"],
    "세제": ["세액공제", "연금소득세", "기타소득세", "퇴직소득세", "과세이연",
             "분리과세", "종합과세", "원천징수", "이연퇴직소득", "연금수령한도"],
    "상품": ["펀드", "투자신탁", "총보수", "위험등급", "수익률", "클래스",
             "환매", "국공채", "채권형", "주식형", "시장잔고"],
    "절차": ["신청", "서류", "이전", "해지", "개시", "수령", "지급", "정산"],
}

# 제공 자료 범위 밖이 확실한 영역 — 조기 거절 후보
OUT_OF_SCOPE_SIGNALS = [
    "주식 종목", "코인", "비트코인", "부동산 매매", "보험 해지환급",
    "대출 금리 비교", "환율", "세무조사", "상속세", "증여세",
]


@dataclass
class GroundingResult:
    """L0 산출물 — **분류 결과일 뿐 판정이 아니다.**

    ⚠️ 2026-08-29 개편: 이 자료구조는 더 이상 답변가능성을 판단하지 않는다.
       예전에는 coverage_score와 early_refuse를 함께 들고 다니며 L1 이전에
       질의를 잘라냈는데, 그 판정이 과최적화돼 정상 질의까지 거절했다.
       지금은 '어떤 영역의 질의인가'와 '문서가 쓰는 용어는 무엇인가'만 담는다.

       early_refuse / refuse_reason / coverage_score 필드는 **호환을 위해
       남겨 두되 항상 기본값**이다. 새 코드에서 참조하지 말 것.
    """
    domain_covered: bool
    domain_areas: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)      # 문서에서 실제 쓰이는 용어
    candidate_doc_ids: list[str] = field(default_factory=list)
    legacy_docs_present: list[str] = field(default_factory=list)
    # ── 아래 셋은 폐기 예정. L0는 판정하지 않는다 ──
    coverage_score: float = 0.0
    early_refuse: bool = False
    refuse_reason: str = ""
    trace: str = ""

    def as_analysis_hint(self) -> str:
        """L1 프롬프트에 주입할 분류 정보.

        원문 전체가 아니라 '어떤 영역의 어떤 용어를 다루는 질의인지'만 전달한다.
        원문을 통째로 넣으면 L1이 이를 근거로 착각해 답변을 만들어낼 위험이 있다.

        ⚠️ 영역을 못 찾아도 "찾지 못했다"로 끝내지 않는다. 그 문장이 L1에게
           '이 질의는 답할 수 없다'는 신호로 읽혀, 개인 서술형 질의를 되돌려
           보내는 원인이 됐다. 분류가 비었다는 사실만 중립적으로 전한다.
        """
        lines: list[str] = []
        if self.domain_areas:
            lines.append(f"관련 영역: {', '.join(self.domain_areas)}")
        else:
            lines.append("관련 영역: 사전 분류에서 특정되지 않음 "
                         "(분류 실패일 뿐 답변 불가를 뜻하지 않음)")
        if self.vocabulary:
            lines.append(f"문서에서 사용하는 용어: {', '.join(self.vocabulary[:10])}")
        if self.legacy_docs_present:
            lines.append(
                f"주의: 검색 범위에 법 개정 이전 수치를 담은 문서가 있습니다 "
                f"({', '.join(self.legacy_docs_present[:3])}). 현행 기준으로 판단하십시오."
            )
        lines.append("※ 위 정보는 분석 참고용이며 답변 근거가 아닙니다. "
                     "근거는 이후 검색 단계에서 확보합니다.")
        return "\n".join(lines)


def _extract_query_terms(query: str, min_len: int = 2) -> list[str]:
    """질의에서 내용어 후보 추출. 조사·어미는 대략적으로 제거."""
    tokens = re.findall(r'[가-힣A-Za-z0-9]{%d,}' % min_len, query)
    stop = {"어떻게", "얼마나", "얼마", "무엇", "뭐가", "인가요", "인데요", "해주세요",
            "알려주세요", "있나요", "되나요", "하나요", "그런데", "그리고", "이랑",
            "저는", "제가", "우리", "지금", "정말", "너무"}
    return [t for t in tokens if t not in stop]


def ground_query(query: str,
                  coarse_search: Callable[[str, int], list[dict]],
                  legacy_checker: Optional[Callable] = None,
                  top_k: int = 8,
                  min_coverage: float = 0.15) -> GroundingResult:
    """질의를 문서 landscape에 접지시킨다.

    coarse_search : (query, k) -> [{"doc_id":..., "text":..., "score":...}]
                    BM25/tsvector 등 LLM 없는 검색 함수를 주입한다.
    legacy_checker: detect_legacy_tax_content. 구법 문서 사전 경고용.

    ⚠️ **이 함수는 거절하지 않는다.** 분류와 용어 수집만 한다.
       도메인 밖 신호가 보이면 `out_of_scope_signals`로 기록만 하고,
       무엇을 할지는 L1이 조건을 다 본 뒤에 정한다 — 짧은 신호 하나로
       질의를 잘라내던 것이 오탐의 주된 원인이었다
       (예: "부동산은 없고 현금 3500만원" → '부동산 매매' 신호로 오인).
    """
    # ── 도메인 밖 신호는 '기록'만 한다 (거절하지 않는다) ──
    off_signals = [s for s in OUT_OF_SCOPE_SIGNALS if s in query]

    # ── 도메인 영역 분류 ──
    areas = [a for a, kws in DOMAIN_AREAS.items() if any(k in query for k in kws)]

    # ── 개략 검색 ──
    try:
        hits = coarse_search(query, top_k) or []
    except Exception as e:
        return GroundingResult(
            domain_covered=bool(areas), domain_areas=areas,
            trace=f"L0 개략 검색 실패({e}) — 접지 없이 분석 진행",
        )

    doc_ids = [h.get("doc_id", "") for h in hits if h.get("doc_id")]
    scores = [float(h.get("score", 0.0)) for h in hits]
    coverage = (sum(scores) / len(scores)) if scores else 0.0

    # ── 문서에서 실제 쓰이는 용어 수집 (질의어와 겹치는 것 우선) ──
    query_terms = set(_extract_query_terms(query))
    vocab: list[str] = []
    for h in hits:
        text = h.get("text", "")
        for area_kws in DOMAIN_AREAS.values():
            for kw in area_kws:
                if kw in text and kw not in vocab:
                    vocab.append(kw)
    # 질의에 등장한 용어를 앞으로
    vocab.sort(key=lambda v: (v not in query_terms, v))

    # ── 구법 문서 사전 탐지 ──
    legacy: list[str] = []
    if legacy_checker:
        for h in hits:
            verdict = legacy_checker(h.get("text", ""), h.get("doc_id", ""))
            if verdict.get("is_legacy_suspect"):
                legacy.append(h.get("doc_id", ""))

    # domain_covered는 '분류가 됐는가'일 뿐 '답할 수 있는가'가 아니다.
    # early_refuse는 항상 False — L0는 판정하지 않는다.
    covered = bool(areas) or coverage >= min_coverage

    return GroundingResult(
        domain_covered=covered,
        domain_areas=areas,
        vocabulary=vocab,
        candidate_doc_ids=doc_ids,
        legacy_docs_present=legacy,
        coverage_score=round(coverage, 4),
        early_refuse=False,
        refuse_reason="",
        trace=(f"L0 분류 — 영역 {areas or '미분류'} · 후보 {len(doc_ids)}건"
               + (f" · 구법 의심 {len(legacy)}건" if legacy else "")
               + (f" · 자료범위 밖 신호 {off_signals}(기록만, 판정은 L1)"
                  if off_signals else "")),
    )


# ════════════════════════════════════════════════════════════════
# 조기 거절 — LLM 호출 전에 판정
# ════════════════════════════════════════════════════════════════

def should_refuse_early(grounding: GroundingResult) -> tuple[bool, str]:
    """⚠️ 폐기됨 — 항상 (False, "")를 돌려준다.

    ━━ 왜 없앴는가 ━━
    L1 이전에 거절을 판정하는 구조 자체가 문제였다. 이 시점에는 사용자
    조건이 하나도 파악되지 않은 상태라, 판단 근거가 '질의에 어떤 낱말이
    있는가'뿐이다. 그 결과 과최적화가 일어나 정상 질의까지 잘려 나갔다:

        "부동산은 없고 현금 3,500만원이 있는데 노후 대비를 어떻게…"
          → '부동산 매매' 신호로 오인 → 조기 거절

    사람은 정확한 정보를 처음부터 주지 않는다. 불특정한 서술을 받아
    "이만큼은 답할 수 있고, 이런 정보를 주시면 더 정확히 답할 수 있다"고
    돌려주는 것이 옳지, 입구에서 잘라내는 것은 옳지 않다.

    시그니처는 호출부 호환을 위해 남긴다. 판정은 L1이 조건을 다 본 뒤에 한다.
    """
    return False, ""


def build_refuse_response(question_id: str, question: str,
                           reason: str) -> dict:
    """조기 거절 시 평가 API 5필드 응답.

    retrieved_context를 비우지 않는다 — 스키마 위반이 되고,
    '모든 답변에 근거 문서 표시' 요건에도 어긋난다.
    """
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": "근거 문서 없음 — 제공 자료 범위에서 관련 문서를 찾지 못했습니다.",
        "think_trace": f"L0 사전 검색 단계에서 도메인 범위 밖으로 판정. {reason} "
                       f"이후 단계(질의 분석·검색·생성)를 수행하지 않고 종료.",
        "answer": f"{reason} 연금 제도·세제·상품에 관한 질문이라면 다시 문의해 주세요.",
    }
