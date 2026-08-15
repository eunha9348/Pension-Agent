"""L3 Exploration — 정밀 검색 (retrieve_hybrid).

BM25(어휘) + 벡터(의미)를 RRF로 융합한다.
**임베딩이 꺼져 있으면 BM25 단독으로 자동 축퇴한다** — 대회 제약 1이
확인되지 않았으므로 그것이 기본 경로다(app/retrieval/embedding.py 참고).

━━ 질의 확장 ━━
평가 질의는 구어체다("얼마까지 되나요"). 문서는 법령체다("한도로 한다").
그래서 질의 원문만으로 BM25를 돌리면 재현율이 떨어진다.
도메인 동의어 사전(app/analysis/vocab.py)으로 질의를 확장해 이를 메운다.

━━ 메타데이터 선필터 ━━
query_spec에 엔티티(상품명·클래스 등)가 있으면 그 엔티티를 가진 청크로
후보를 먼저 좁힌다. 검색 후 필터링(filter_irrelevant_evidence)만으로는
상위 k에 엉뚱한 상품이 차 버리면 정답 청크가 아예 안 올라온다.
"""

from __future__ import annotations

from typing import Optional

from app.analysis.vocab import expand
from app.core.coverage_pipeline import ENTITY_KEYS, EvidenceChunk
from app.ingest.store import ChunkRecord, DocumentStore, get_store
from app.retrieval.embedding import embed_texts, embedding_enabled
from app.retrieval.tokenize import content_terms

TOP_K = 8
RRF_K = 60          # RRF 표준 상수


def _expand_query(query: str) -> str:
    """도메인 동의어로 질의 확장. 원 질의를 앞에 두어 가중치를 유지한다."""
    extra = expand(content_terms(query)) - content_terms(query)
    if not extra:
        return query
    return query + " " + " ".join(sorted(extra))


def _entity_prefilter(store: DocumentStore, entities: dict) -> Optional[set[str]]:
    """질의 엔티티와 충돌하지 않는 청크 ID 집합. 없으면 None(전체 대상)."""
    active = {k: v for k, v in (entities or {}).items()
              if k in ENTITY_KEYS and v}
    if not active:
        return None

    allowed: set[str] = set()
    for rec in store.all_chunks():
        conflict = False
        for key, qv in active.items():
            cv = (rec.entities or {}).get(key)
            if cv and str(qv) not in str(cv):
                conflict = True
                break
        if not conflict:
            allowed.add(rec.chunk_id)
    # 선필터가 전부 걸러버리면 검색 자체가 죽는다 — 그때는 필터를 포기한다
    return allowed or None


def _rrf_merge(*ranked_lists: list[str]) -> dict[str, float]:
    """Reciprocal Rank Fusion — 점수 스케일이 다른 순위들을 합친다."""
    fused: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, key in enumerate(lst, 1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def _to_evidence(rec: ChunkRecord, score: float) -> EvidenceChunk:
    entities = dict(rec.entities or {})
    if rec.locator:
        entities.setdefault("clause_no", rec.locator)
    return EvidenceChunk(doc_id=rec.doc_id, text=rec.text,
                         entities=entities, score=round(score, 6))


def make_retrieve_hybrid(store: Optional[DocumentStore] = None,
                         top_k: int = TOP_K):
    """(query_spec) -> [EvidenceChunk] 시그니처의 함수를 만든다.

    coverage_pipeline.build_answer(retrieve_hybrid=...)에 그대로 주입된다.
    """
    s = store or get_store()

    def retrieve_hybrid(query_spec: dict) -> list[EvidenceChunk]:
        query = (query_spec or {}).get("query") or (query_spec or {}).get("question") or ""
        if not query:
            return []

        allowed = _entity_prefilter(s, (query_spec or {}).get("entities", {}))
        expanded = _expand_query(query)

        lexical = s.search_bm25(expanded, top_k=top_k * 2, allowed=allowed)
        if not lexical:
            return []

        by_id = {rec.chunk_id: (rec, score) for rec, score in lexical}
        lexical_rank = [rec.chunk_id for rec, _ in lexical]

        vector_rank = _vector_rank(s, query, allowed)
        if vector_rank:
            fused = _rrf_merge(lexical_rank, vector_rank)
            order = sorted(fused, key=lambda k: -fused[k])
        else:
            # 임베딩 미사용(기본) — BM25 순위를 그대로 쓴다
            order = lexical_rank

        out: list[EvidenceChunk] = []
        for chunk_id in order[:top_k]:
            entry = by_id.get(chunk_id)
            if entry is None:
                rec = s.get_chunk(chunk_id)
                if rec is None:
                    continue
                entry = (rec, 0.0)
            out.append(_to_evidence(*entry))
        return out

    return retrieve_hybrid


def _vector_rank(store: DocumentStore, query: str,
                 allowed: Optional[set[str]]) -> list[str]:
    """벡터 검색 순위. 임베딩이 꺼져 있으면 빈 리스트 → BM25 단독 경로."""
    if not embedding_enabled():
        return []
    qvec = embed_texts([query])
    if not qvec:
        return []

    from app.retrieval.embedding import cosine
    scored: list[tuple[str, float]] = []
    for rec in store.all_chunks():
        if allowed is not None and rec.chunk_id not in allowed:
            continue
        vec = (rec.entities or {}).get("_embedding")
        if not vec:
            continue
        scored.append((rec.chunk_id, cosine(qvec[0], vec)))
    scored.sort(key=lambda x: -x[1])
    return [k for k, _ in scored]
