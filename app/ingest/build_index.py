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
from app.ingest.loader import corpus_files, is_ingestible, iter_documents
from app.ingest.store import DEFAULT_INDEX_DIR, ChunkRecord, DocumentStore
from app.retrieval.bm25 import build_index as build_bm25

REAL_CORPUS = REPO_ROOT / "data" / "corpus"
MOCK_CORPUS = REPO_ROOT / "data" / "corpus_mock"


def choose_corpus(explicit: str | None = None) -> tuple[Path, str]:
    """코퍼스 디렉터리와 종류를 정한다.

    ⚠️ **data/corpus/ 에 파일이 하나라도 있으면 절대 mock으로 떨어지지 않는다.**
       예전에는 zip만 찾다가, 실물 PDF를 넣어 뒀는데도 조용히 mock 코퍼스로
       인덱스를 만드는 사고가 있었다. 그 상태로 평가를 받으면 지어낸 문서로
       답변하게 된다. 실물을 넣으려는 의도가 보이면, 판독에 실패하더라도
       mock으로 대체하지 않고 **소리 내어 실패**하는 쪽이 옳다.
    """
    if explicit:
        p = Path(explicit)
        kind = "mock" if p.resolve() == MOCK_CORPUS.resolve() else "real"
        return p, kind
    if corpus_files(REAL_CORPUS):
        return REAL_CORPUS, "real"
    return MOCK_CORPUS, "mock"


def ingest(corpus_dir: Path, corpus_kind: str) -> DocumentStore:
    store = DocumentStore(corpus_kind=corpus_kind)
    all_warnings: list[str] = []
    skipped: list[str] = []

    for doc in iter_documents(corpus_dir):
        if not doc.pages:
            # 판독 실패를 조용히 넘기지 않는다 — 문서가 통째로 빠진 채
            # 서비스가 뜨는 것이 가장 위험하다
            skipped.append(f"{Path(doc.source_path).name}: "
                           f"{'; '.join(doc.warnings) or '텍스트 없음'}")
            continue
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

    if skipped:
        print(f"\n[판독하지 못한 파일 {len(skipped)}건] — 인덱스에 포함되지 않았습니다")
        for s_ in skipped:
            print(f"  ❌ {s_}")

    if all_warnings:
        print("\n[경고]")
        for w in all_warnings:
            print(f"  ⚠ {w}")

    store.skipped_files = skipped
    return store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="문서 인제스트 및 검색 인덱스 생성")
    ap.add_argument("--corpus", help="zip이 있는 디렉터리")
    ap.add_argument("--index", default=str(DEFAULT_INDEX_DIR), help="인덱스 출력 위치")
    args = ap.parse_args(argv)

    corpus_dir, kind = choose_corpus(args.corpus)
    files = corpus_files(corpus_dir)

    if not files:
        if kind == "mock":
            print(f"mock 코퍼스가 없어 생성합니다 → {corpus_dir}")
            from app.ingest.make_mock_corpus import build
            build(corpus_dir)
            files = corpus_files(corpus_dir)
        else:
            print(f"❌ {corpus_dir} 가 비어 있습니다.")
            return 1

    ingestible = [f for f in files if is_ingestible(f)]
    print(f"코퍼스: {corpus_dir}  (종류: {kind})")
    print(f"파일 {len(files)}건 중 판독 가능 {len(ingestible)}건")

    if kind == "mock":
        print("⚠️  실제 제공 자료가 아닌 mock 문서로 인덱스를 만듭니다.")
        print("   실물 문서를 data/corpus/ 에 넣고 다시 실행하면 자동으로 교체됩니다.\n")
    elif not ingestible:
        # 실물을 넣으려는 의도가 분명한데 하나도 못 읽는 상황.
        # 여기서 mock으로 대체하면 '지어낸 문서로 답변하는' 최악의 사고가 난다.
        print("\n❌ 판독 가능한 파일이 하나도 없습니다. 인덱스를 만들지 않습니다.")
        print("   (mock 코퍼스로 대체하지 않습니다 — 지어낸 문서로 답변하게 되므로)")
        print("\n   무엇을 넣었는지 확인하려면:")
        print("     python -m app.ingest.check_corpus")
        return 1

    store = ingest(corpus_dir, kind)

    if kind == "real" and not store.docs:
        print("\n❌ 인제스트된 문서가 0건입니다. 기존 인덱스를 덮어쓰지 않고 종료합니다.")
        return 1

    out = store.save(args.index)
    print(f"\n문서 {len(store.docs)}건 · 청크 {len(store.chunks)}건 → {out}")
    if kind == "real":
        print("✅ 실물 코퍼스로 인덱스를 만들었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
