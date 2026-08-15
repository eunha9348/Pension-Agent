"""인덱스 저장소 — 파일 기반(외부 DB 불필요).

    data/index/
      chunks.json     청크 본문 + 메타데이터
      docs.json       문서 메타데이터
      bm25.json       역색인

PostgreSQL로 갈아탈 때는 이 클래스의 인터페이스(get_chunk / all_chunks /
search_bm25 / doc_meta)만 맞추면 상위 계층은 손댈 필요가 없다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import REPO_ROOT
from app.retrieval.bm25 import BM25Index, normalize_scores

DEFAULT_INDEX_DIR = REPO_ROOT / "data" / "index"


@dataclass
class ChunkRecord:
    chunk_id: str
    doc_id: str
    text: str
    ordinal: int = 0
    page_from: int = 0
    page_to: int = 0
    locator: Optional[str] = None
    is_table: bool = False
    entities: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkRecord":
        return cls(
            chunk_id=d["chunk_id"], doc_id=d["doc_id"], text=d.get("text", ""),
            ordinal=d.get("ordinal", 0), page_from=d.get("page_from", 0),
            page_to=d.get("page_to", 0), locator=d.get("locator"),
            is_table=bool(d.get("is_table")), entities=d.get("entities") or {},
        )


@dataclass
class DocumentStore:
    chunks: dict[str, ChunkRecord] = field(default_factory=dict)
    docs: dict[str, dict] = field(default_factory=dict)
    bm25: BM25Index = field(default_factory=BM25Index)
    corpus_kind: str = "unknown"       # "real" | "mock" | "empty"

    # ── 조회 ────────────────────────────────────────────────
    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def get_chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        return self.chunks.get(chunk_id)

    def all_chunks(self) -> list[ChunkRecord]:
        return list(self.chunks.values())

    def doc_meta(self, doc_id: str) -> dict:
        return self.docs.get(doc_id, {})

    def doc_meta_map(self) -> dict[str, dict]:
        """citation_system.build_citations(doc_meta=...)에 넘길 형태."""
        return {did: {"type": m.get("type"), "title": m.get("title")}
                for did, m in self.docs.items()}

    def search_bm25(self, query: str, top_k: int = 10,
                    allowed: Optional[set[str]] = None) -> list[tuple[ChunkRecord, float]]:
        """정규화 점수(0~1)와 함께 청크를 반환."""
        hits = normalize_scores(self.bm25.search(query, top_k=top_k, allowed=allowed))
        out = []
        for chunk_id, score in hits:
            rec = self.chunks.get(chunk_id)
            if rec is not None:
                out.append((rec, score))
        return out

    # ── 저장 / 적재 ─────────────────────────────────────────
    def save(self, index_dir: str | Path = DEFAULT_INDEX_DIR) -> Path:
        d = Path(index_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(json.dumps(
            {cid: c.__dict__ for cid, c in self.chunks.items()},
            ensure_ascii=False), encoding="utf-8")
        (d / "docs.json").write_text(json.dumps(
            {"corpus_kind": self.corpus_kind, "docs": self.docs},
            ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "bm25.json").write_text(json.dumps(
            self.bm25.to_dict(), ensure_ascii=False), encoding="utf-8")
        return d

    @classmethod
    def load(cls, index_dir: str | Path = DEFAULT_INDEX_DIR) -> "DocumentStore":
        d = Path(index_dir)
        store = cls()
        if not (d / "chunks.json").exists():
            store.corpus_kind = "empty"
            return store

        raw_chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
        store.chunks = {cid: ChunkRecord.from_dict(c) for cid, c in raw_chunks.items()}

        docs_blob = json.loads((d / "docs.json").read_text(encoding="utf-8"))
        store.docs = docs_blob.get("docs", {})
        store.corpus_kind = docs_blob.get("corpus_kind", "unknown")

        store.bm25 = BM25Index.from_dict(
            json.loads((d / "bm25.json").read_text(encoding="utf-8")))
        return store


# ── 프로세스 싱글턴 ─────────────────────────────────────────
_STORE: Optional[DocumentStore] = None


def get_store(index_dir: str | Path = DEFAULT_INDEX_DIR,
              reload: bool = False) -> DocumentStore:
    global _STORE
    if _STORE is None or reload:
        _STORE = DocumentStore.load(index_dir)
    return _STORE


def set_store(store: DocumentStore) -> None:
    """테스트에서 인덱스를 갈아끼울 때 사용."""
    global _STORE
    _STORE = store
