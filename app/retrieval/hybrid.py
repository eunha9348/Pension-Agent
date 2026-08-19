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

━━ 함정 유도 검색 ━━
L2 함정 감지는 이 계층보다 **먼저** 돈다. 감지된 함정은 자기 근거 문서를
알고 있으므로(TrapRule.source), 그 문서 안을 별도로 훑어 슬롯을 예약한다.
예약하지 않으면, 문서 158건에 반복되는 보일러플레이트가 상위를 다 차지해
정작 그 질의에만 해당하는 문서가 근거에 들어오지 못한다.

━━ 후처리 ━━
BM25 순위를 그대로 쓰지 않는다. 중복 제거·문서 다양성·저정보 청크 강등을
거쳐야 근거 8칸에 서로 다른 사실 8개가 들어간다(app/retrieval/rerank.py).
"""

from __future__ import annotations

from typing import Optional

from app.analysis.vocab import expand
from app.core.coverage_pipeline import ENTITY_KEYS, EvidenceChunk
from app.ingest.store import ChunkRecord, DocumentStore, get_store
from app.retrieval.embedding import embed_texts, embedding_enabled
from app.retrieval.rerank import rerank
from app.retrieval.tokenize import content_terms

TOP_K = 8
RRF_K = 60          # RRF 표준 상수

# 함정이 지목한 문서에서 예약할 최대 근거 수.
# 너무 크게 잡으면 함정 문서가 근거를 독점해 일반 질의 대응이 나빠진다.
TRAP_RESERVED = 3


# 동의어 확장 텀의 가중치. 원 질의어보다 낮아야 한다.
EXPANSION_WEIGHT = 0.35


def _expansion_terms(query: str) -> str:
    """원 질의에 없는 동의어만 모은다."""
    extra = expand(content_terms(query)) - content_terms(query)
    return " ".join(sorted(extra))


def _weighted_lexical(store: DocumentStore, query: str, top_k: int,
                      allowed: Optional[set[str]]) -> list[tuple[ChunkRecord, float]]:
    """원 질의 + 동의어 확장을 **가중 합산**한다.

    ⚠️ 확장 텀을 원 질의와 같은 비중으로 넣으면 안 된다.
       "연금저축·IRP 세액공제" 질의에서 동의어(연금저축계좌·개인형퇴직연금)만
       많이 포함한 짧은 청크(중도인출 조항)가, 정작 '세액공제'를 다루는 청크보다
       위로 올라오는 현상이 실제로 발생했다. BM25는 짧은 문서를 선호하기 때문이다.
       원 질의어에 온전한 가중치를 주고 확장은 보조로만 쓴다.
    """
    scores: dict[str, float] = {}
    records: dict[str, ChunkRecord] = {}

    for text, weight in ((query, 1.0), (_expansion_terms(query), EXPANSION_WEIGHT)):
        if not text.strip():
            continue
        for rec, score in store.search_bm25(text, top_k=top_k * 3, allowed=allowed):
            records[rec.chunk_id] = rec
            scores[rec.chunk_id] = scores.get(rec.chunk_id, 0.0) + score * weight

    if not scores:
        return []
    top = max(scores.values()) or 1.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
    return [(records[cid], round(sc / top, 6)) for cid, sc in ranked]


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


def _rrf_merge(*ranked: tuple[list[str], float]) -> dict[str, float]:
    """Reciprocal Rank Fusion — 점수 스케일이 다른 순위들을 합친다.

    각 항목은 (순위 목록, 가중치)다. BM25는 1.0을 기준으로 두고, 벡터 쪽은
    EMBEDDING_WEIGHT(기본 0.5)로 조절한다 — 이 도메인은 조문·수치처럼
    **정확한 어휘 일치가 중요한 질의**가 많아서 벡터를 동등하게 두면
    엉뚱한 문서가 올라온다. 의미 검색은 어휘 검색의 보완으로 쓴다.
    """
    fused: dict[str, float] = {}
    for lst, weight in ranked:
        for rank, key in enumerate(lst, 1):
            fused[key] = fused.get(key, 0.0) + weight / (RRF_K + rank)
    return fused


def _to_evidence(rec: ChunkRecord, score: float) -> EvidenceChunk:
    entities = dict(rec.entities or {})
    if rec.locator:
        entities.setdefault("clause_no", rec.locator)
    return EvidenceChunk(doc_id=rec.doc_id, text=rec.text,
                         entities=entities, score=round(score, 6))


def _chunks_of_docs(store: DocumentStore, doc_ids: set[str]) -> set[str]:
    """지정한 문서에 속한 청크 ID 집합."""
    return {rec.chunk_id for rec in store.all_chunks() if rec.doc_id in doc_ids}


def _trap_steered(store: DocumentStore, steer: list[dict],
                  limit: int = TRAP_RESERVED) -> list[tuple[ChunkRecord, float]]:
    """함정이 지목한 문서 안에서만 근거를 찾아 온다.

    질의로 쓰는 것은 사용자 원문이 아니라 함정 규칙의 `fact` 문장이다.
    사용자는 "명퇴수당 절세법만 알려주세요"라고 묻지만 문서에는
    "법정 외 퇴직급여는 IRP 의무이전 대상이 아니며…"라고 쓰여 있다.
    fact가 이미 문서와 같은 어휘·문체로 쓰여 있으므로, 이걸 질의로 쓰면
    구어체와 법령체 사이의 격차를 건너뛸 수 있다.

    critical 함정을 먼저 처리해 슬롯을 우선 배정한다.
    """
    if not steer:
        return []

    ordered = sorted(steer, key=lambda s_: 0 if s_.get("severity") == "critical" else 1)

    picked: list[tuple[ChunkRecord, float]] = []
    seen: set[str] = set()
    for item in ordered:
        if len(picked) >= limit:
            break
        docs = set(item.get("docs") or ())
        fact = item.get("fact") or ""
        if not docs or not fact:
            continue
        allowed = _chunks_of_docs(store, docs)
        if not allowed:
            # 함정이 지목한 문서가 이 코퍼스에 없을 수 있다(문서 구성이 다름).
            # 조용히 넘어간다 — 없는 문서를 만들어낼 수는 없다.
            continue
        for rec, score in store.search_bm25(fact, top_k=limit, allowed=allowed):
            if rec.chunk_id in seen:
                continue
            seen.add(rec.chunk_id)
            picked.append((rec, score))
            if len(picked) >= limit:
                break
    return picked


def make_retrieve_hybrid(store: Optional[DocumentStore] = None,
                         top_k: int = TOP_K):
    """(query_spec) -> [EvidenceChunk] 시그니처의 함수를 만든다.

    coverage_pipeline.build_answer(retrieve_hybrid=...)에 그대로 주입된다.

    query_spec["retrieval_steer"]가 있으면(L2 함정 감지 결과) 그 문서를
    별도로 훑어 슬롯을 예약한다. 없으면 기존 동작과 동일하다.
    """
    s = store or get_store()

    def retrieve_hybrid(query_spec: dict) -> list[EvidenceChunk]:
        spec = query_spec or {}
        query = spec.get("query") or spec.get("question") or ""
        if not query:
            return []

        allowed = _entity_prefilter(s, spec.get("entities", {}))

        # 재순위가 실제로 고를 수 있도록 후보를 넉넉히 확보한다.
        # 중복·저정보 청크가 걸러지고 나면 후보가 크게 줄기 때문이다.
        lexical = _weighted_lexical(s, query, top_k * 3, allowed)

        by_id: dict[str, tuple[ChunkRecord, float]] = {
            rec.chunk_id: (rec, score) for rec, score in lexical}
        lexical_rank = [rec.chunk_id for rec, _ in lexical]

        vector_rank = _vector_rank(s, query, allowed)
        if vector_rank:
            from app.config import get_settings
            w = get_settings().embedding_weight
            fused = _rrf_merge((lexical_rank, 1.0),
                               (vector_rank[:top_k * 3], w))
            order = sorted(fused, key=lambda k: -fused[k])
            # RRF 점수는 스케일이 다르므로 0~1로 다시 정규화해 둔다
            top_fused = max(fused.values()) or 1.0
            for cid in order:
                if cid not in by_id:
                    rec = s.get_chunk(cid)
                    if rec is None:
                        continue
                    by_id[cid] = (rec, 0.0)
                rec, _ = by_id[cid]
                by_id[cid] = (rec, round(fused[cid] / top_fused, 6))
        else:
            # 임베딩 미사용 — BM25 순위를 그대로 쓴다
            order = lexical_rank

        candidates = [by_id[cid] for cid in order if cid in by_id]

        # ── 함정 유도 검색 — 예약 슬롯 ──────────────────────
        steered = _trap_steered(s, spec.get("retrieval_steer") or [])
        pinned: set[str] = set()
        if steered:
            existing = {rec.chunk_id for rec, _ in candidates}
            for rec, score in steered:
                pinned.add(rec.chunk_id)
                if rec.chunk_id not in existing:
                    # 함정 근거는 일반 순위 경쟁에서 밀리더라도 후보에 넣는다.
                    # 점수는 상위권으로 올려 재순위에서 살아남게 한다.
                    candidates.append((rec, max(score, 0.9)))
                else:
                    candidates = [(r, max(sc, 0.9) if r.chunk_id == rec.chunk_id else sc)
                                  for r, sc in candidates]

        if not candidates:
            return []

        selected, report = rerank(candidates, top_k, pinned=pinned)
        # 무엇을 왜 걸렀는지는 파이프라인이 think_trace에 남긴다
        spec["_rerank_trace"] = report.as_trace()
        spec["_steered_docs"] = sorted({rec.doc_id for rec, _ in steered})

        return [_to_evidence(rec, score) for rec, score in selected]

    return retrieve_hybrid


_VECTORS: Optional["object"] = None


def _vectors():
    """청크 벡터 저장소 (프로세스 내 1회 로드).

    없으면 빈 저장소가 오고, 그러면 자동으로 BM25 단독 경로가 된다 —
    벡터를 아직 안 만들었다고 검색이 죽으면 안 된다.
    """
    global _VECTORS
    if _VECTORS is None:
        from app.config import get_settings
        from app.ingest.vector_store import VectorStore
        _VECTORS = VectorStore.load(get_settings().index_path)
    return _VECTORS


def vector_count() -> int:
    """보유한 청크 벡터 수 — /health 와 기동 로그에서 쓴다."""
    try:
        return len(_vectors())
    except Exception:      # noqa: BLE001 — 상태 보고가 서비스를 막으면 안 된다
        return 0


def _vector_rank(store: DocumentStore, query: str,
                 allowed: Optional[set[str]]) -> list[str]:
    """벡터 검색 순위. 임베딩이 꺼져 있거나 벡터가 없으면 빈 리스트."""
    if not embedding_enabled():
        return []
    vs = _vectors()
    if not len(vs):
        return []
    qvec = embed_texts([query])
    if not qvec:
        return []
    return [cid for cid, _ in vs.rank(qvec[0], allowed=allowed, top_k=TOP_K * 4)]
