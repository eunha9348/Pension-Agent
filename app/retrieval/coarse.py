"""L0 개략 검색 (coarse_search).

`grounding_retrieval.ground_query(coarse_search=...)`에 주입되는 함수.

━━ L3 정밀 검색과 별개 함수로 유지하는 이유 ━━
L0 결과는 **답변 근거가 아니다.** Exploitation 필터(구법탐지·엔티티충돌·
가입자격·하드제약)를 거치지 않았기 때문이다. 두 검색이 같은 함수를 쓰면
L0 결과가 답변 근거 경로로 새어들 위험이 생긴다.

여기서 반환하는 dict는 ground_query가 기대하는 최소 형태
`{"doc_id", "text", "score"}` 만 담는다 — 청크 ID조차 넘기지 않아서,
호출 측이 실수로 이걸 인용에 쓰기 어렵게 만든다.
"""

from __future__ import annotations

from typing import Optional

from app.ingest.store import DocumentStore, get_store

# L0는 넓고 얕게 본다 — 정밀도가 아니라 '이 도메인 문서가 있는가'를 본다.
COARSE_TOP_K = 8


def make_coarse_search(store: Optional[DocumentStore] = None):
    """(query, k) -> [{"doc_id","text","score"}] 시그니처의 함수를 만든다."""
    s = store or get_store()

    def coarse_search(query: str, k: int = COARSE_TOP_K) -> list[dict]:
        hits = s.search_bm25(query, top_k=k)
        return [{"doc_id": rec.doc_id, "text": rec.text, "score": score}
                for rec, score in hits]

    return coarse_search
