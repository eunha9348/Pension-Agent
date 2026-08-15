"""인제스트 오케스트레이터.

    python -m app.ingest.build_index                 # 자동 선택
    python -m app.ingest.build_index --corpus data/corpus

코퍼스 선택 규칙:
  1. data/corpus/ 에 zip이 있으면 **실물 우선**
  2. 없으면 data/corpus_mock/ 을 쓰고, 인덱스에 corpus_kind="mock"을 새긴다
     (API /health 와 think_trace 에 mock 사용 사실이 그대로 드러난다)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.ingest.chunker import chunk_document
from app.ingest.metadata import apply_chunk_metadata, build_doc_metadata
from app.ingest.store import DEFAULT_INDEX_DIR, ChunkRecord, DocumentStore
from app.ingest.zip_parser import iter_corpus
from app.retrieval.bm25 import build_index as build_bm25

REAL_CORPUS = REPO_ROOT / "data" / "corpus"
MOCK_CORPUS = REPO_ROOT / "data" / "corpus_mock"


def choose_corpus(explicit: str | None = None) -> tuple[Path, str]:
    if explicit:
        p = Path(explicit)
        kind = "real" if p.resolve() == REAL_CORPUS.resolve() else "mock"
        return p, kind
    if REAL_CORPUS.exists() and any(REAL_CORPUS.glob("*.zip")):
        return REAL_CORPUS, "real"
    return MOCK_CORPUS, "mock"


def ingest(corpus_dir: Path, corpus_kind: str) -> DocumentStore:
    store = DocumentStore(corpus_kind=corpus_kind)
    all_warnings: list[str] = []

    for doc in iter_corpus(corpus_dir):
        chunks = chunk_document(doc)
        meta = build_doc_metadata(doc, chunks)
        chunks = apply_chunk_metadata(chunks, meta)

        store.docs[doc.doc_id] = meta
        for c in chunks:
            store.chunks[c.chunk_id] = ChunkRecord.from_dict(c.as_dict())

        for w in doc.warnings:
            all_warnings.append(f"{doc.doc_id}: {w}")
        print(f"  · {doc.doc_id:20s} 페이지 {doc.page_count:2d} → 청크 {len(chunks):2d} "
              f"[{meta['type']}]"
              + ("  ⚠구법의심" if meta["legacy"]["is_legacy_suspect"] else ""))

    store.bm25 = build_bm25((c.chunk_id, c.text) for c in store.chunks.values())

    if all_warnings:
        print("\n[경고]")
        for w in all_warnings:
            print(f"  ⚠ {w}")
    return store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="문서 인제스트 및 검색 인덱스 생성")
    ap.add_argument("--corpus", help="zip이 있는 디렉터리")
    ap.add_argument("--index", default=str(DEFAULT_INDEX_DIR), help="인덱스 출력 위치")
    args = ap.parse_args(argv)

    corpus_dir, kind = choose_corpus(args.corpus)
    if not corpus_dir.exists() or not any(corpus_dir.glob("*.zip")):
        if kind == "mock":
            print(f"mock 코퍼스가 없어 생성합니다 → {corpus_dir}")
            from app.ingest.make_mock_corpus import build
            build(corpus_dir)
        else:
            print(f"❌ {corpus_dir} 에 zip이 없습니다.")
            return 1

    print(f"코퍼스: {corpus_dir}  (종류: {kind})")
    if kind == "mock":
        print("⚠️  실제 제공 자료가 아닌 mock 문서로 인덱스를 만듭니다.")
        print("   실물 zip을 data/corpus/ 에 넣고 다시 실행하면 자동으로 교체됩니다.\n")

    store = ingest(corpus_dir, kind)
    out = store.save(args.index)
    print(f"\n문서 {len(store.docs)}건 · 청크 {len(store.chunks)}건 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
