"""거절 대신 연결 — 인접 주제로 자연스럽게 잇는다.

━━ 왜 필요한가 ━━
"오늘 코스피 지수 알려줘"에 "답변드리기 어렵습니다"만 내놓는 것은 정직하지만
불친절하다. 지수 **수치**는 제공 자료에 없지만, 지수를 추종하는 연금 편입
상품은 자료 안에 있을 수 있다. 그렇다면 못 하는 것은 못 한다고 밝히고
**할 수 있는 것으로 이어 주는** 편이 낫다. 평가지표 '정보한계 대응'도
한계를 숨기지 않으면서 대안을 제시하는 쪽을 더 높게 본다.

━━ 연결과 환각을 가르는 단 하나의 규칙 ━━
**연결 대상이 제공 문서에 실재할 때만 연결한다.**
문서에 없는 상품을 갖다 붙이면 그건 연결이 아니라 환각이고, 그냥 거절하는
것보다 훨씬 나쁘다. 그래서 이 모듈은 코퍼스를 실제로 검색해 근거를 확보하지
못하면 **None을 반환**한다 — 호출 측은 그대로 기존 거절로 간다.
코퍼스에 무엇이 있는지는 scripts/corpus_probe.py 로 미리 확인할 수 있다.

━━ 연결하지 않는 것 ━━
프롬프트 탈취처럼 **응해선 안 되는 요구**는 연결 대상이 아니다. 여기서
인접 주제를 제시하면 공격에 부분적으로 응답하는 꼴이 된다. 하드 거절을 유지한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.coverage_pipeline import EvidenceChunk

# 연결을 절대 하지 않는 거절 사유. 응해선 안 되는 요구는 대안도 주지 않는다.
NEVER_BRIDGE = {"PROMPT_INJECTION", "UNSAFE_REQUEST"}

# 연결이 성립하려면 최소 이만큼의 근거가 필요하다. 한 조각만 걸린 것은
# 우연일 수 있고, 그걸로 상품을 안내하면 근거가 빈약한 추천이 된다.
MIN_DOCS = 2
TOP_K = 4


@dataclass(frozen=True)
class BridgeTopic:
    """도메인 밖 주제 → 자료 안의 인접 주제."""
    name: str
    signals: tuple[str, ...]        # 질의에서 이 주제를 알아보는 말
    corpus_query: str               # 자료에서 인접 상품을 찾을 검색어
    cannot: str                     # 우리가 못 하는 것 (정직하게 밝힐 내용)
    can: str                        # 대신 할 수 있는 것


# 순서가 중요하다 — 앞의 항목이 더 구체적인 주제다.
BRIDGE_TOPICS: tuple[BridgeTopic, ...] = (
    BridgeTopic(
        "지수",
        ("코스피", "kospi", "코스닥", "s&p", "나스닥", "지수", "주가"),
        "인덱스 지수 추종 주식형 펀드 투자대상",
        "오늘의 지수나 시세 같은 실시간 시장 정보는 제공 자료에 없어 알려드릴 수 없습니다",
        "지수를 추종하거나 주식에 투자하는 연금 편입 상품",
    ),
    BridgeTopic(
        "부동산",
        ("부동산", "리츠", "reits", "아파트", "주택 투자"),
        "부동산 리츠 특별자산 투자대상 펀드",
        "부동산 투자 자체에 대한 조언은 제공 자료의 범위 밖입니다",
        "연금계좌에서 편입할 수 있는 부동산·리츠 관련 상품",
    ),
    BridgeTopic(
        "환율",
        ("환율", "달러", "환테크", "외화"),
        "해외 외화 환헤지 환율변동 위험 펀드",
        "환율 전망이나 시세는 제공 자료에 없습니다",
        "환율 변동에 노출되는 해외 투자 상품과 환헤지 관련 유의사항",
    ),
    BridgeTopic(
        "채권금리",
        ("금리 전망", "기준금리", "국채 금리", "채권 금리"),
        "채권형 금리변동 듀레이션 투자위험",
        "금리 전망은 제공 자료에 없습니다",
        "금리 변동이 수익률에 어떻게 작용하는지와 채권형 상품의 위험 고지",
    ),
)


@dataclass
class BridgeResult:
    topic: BridgeTopic
    evidence: list[EvidenceChunk] = field(default_factory=list)

    @property
    def doc_ids(self) -> list[str]:
        seen: list[str] = []
        for c in self.evidence:
            if c.doc_id not in seen:
                seen.append(c.doc_id)
        return seen

    def as_answer(self) -> str:
        """못 하는 것 → 할 수 있는 것 → 근거 순서로 잇는다.

        수치를 지어내지 않는다. 상품명·조건은 호출 측이 근거 본문에서
        읽어 가도록 doc_id만 명시한다.
        """
        docs = ", ".join(self.doc_ids)
        return (
            f"{self.topic.cannot}.\n\n"
            f"다만 {self.topic.can}에 대해서는 제공 자료로 안내드릴 수 있습니다. "
            f"아래 근거 문서에 관련 내용이 있습니다({docs}). "
            f"어떤 점이 궁금하신지 알려주시면 해당 부분을 자세히 설명드리겠습니다.\n\n"
            f"※ 특정 상품의 가입 여부는 판매 클래스별 가입자격과 "
            f"본인의 연금계좌 유형에 따라 달라집니다."
        )

    def as_trace(self) -> str:
        return (f"'{self.topic.name}' 주제 — 요청한 정보는 자료 밖이나 "
                f"인접 근거 {len(self.evidence)}건({', '.join(self.doc_ids)}) 확보 "
                f"→ 거절 대신 연결")


def match_topic(question: str) -> Optional[BridgeTopic]:
    q = (question or "").lower()
    for t in BRIDGE_TOPICS:
        if any(s in q for s in t.signals):
            return t
    return None


def find_bridge(question: str, refusal=None, store=None) -> Optional[BridgeResult]:
    """연결 가능하면 BridgeResult, 아니면 None(=기존 거절 유지).

    None을 돌려주는 경우가 정상 경로의 대부분이다 — 연결은 근거가
    실재할 때만 성립하는 예외적 처리다.
    """
    if refusal is not None and getattr(refusal, "code", "") in NEVER_BRIDGE:
        return None

    topic = match_topic(question)
    if topic is None:
        return None

    try:
        if store is None:
            from app.ingest.store import get_store
            store = get_store()
        hits = store.search_bm25(topic.corpus_query, top_k=TOP_K)
    except Exception:      # noqa: BLE001 — 연결은 부가 기능이다. 실패하면 거절로 간다.
        return None

    evidence = [EvidenceChunk(rec.doc_id, rec.text, score=score)
                for rec, score in hits]
    result = BridgeResult(topic, evidence)
    if len(result.doc_ids) < MIN_DOCS:
        # 자료에 이 주제가 없다 → 연결하지 않는다. 없는 상품을 만들지 않는다.
        return None
    return result
