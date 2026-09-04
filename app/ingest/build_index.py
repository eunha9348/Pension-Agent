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
from app.ingest.ocr_repair import repair_documents
from app.ingest.reocr import reocr_documents
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

    # ⚠️ 청킹 전에 **코퍼스 전체를 모아** OCR 판독 실패 구간을 복원한다.
    #    교차 문서 대조라 한 문서씩 스트리밍하면서는 할 수 없다 — 깨진 자리를
    #    메울 근거가 다른 문서에 있기 때문이다. 청킹 뒤로 미루면 청크 경계가
    #    앵커를 잘라 복원율이 떨어진다.
    documents = list(iter_documents(corpus_dir))

    # 교차 문서 대조보다 먼저 재OCR을 돈다 — 손상이 촘촘한 페이지는 앵커
    # 자체를 만들 공간이 없어(이웃 손상과 겹침) 대조가 구조적으로 불가능한
    # 경우가 많다(`ocr_repair.py`의 "판독 실패 앵커가 이웃 손상을 물면
    # 대조가 항상 실패한다" 참조). 재OCR은 그 페이지 자신의 원본 이미지를
    # 다시 읽는 것이라 이 한계 밖이다(`reocr.py`). 재OCR로 정리된 뒤에도
    # 남은 손상은 그대로 교차 문서 복원으로 넘어간다 — 이중 안전망이다.
    reocr = reocr_documents(documents)
    if reocr.pages_targeted or not reocr.engine_available:
        print(f"\n[재OCR] {reocr.summary()}")
        for s_ in reocr.samples:
            print(f"    · {s_}")
        print()

    repair = repair_documents(documents)
    if repair.runs_found:
        print(f"\n[OCR 복원] {repair.summary()}")
        for s_ in repair.samples:
            print(f"    · {s_}")
        if repair.runs_masked:
            print(f"    ⚠ 복원하지 못한 {repair.runs_masked}건은 "
                  f"'(판독불가)'로 표시됩니다 — 원문이 깨진 것이며 "
                  f"인용문에 '?'가 그대로 나가지는 않습니다")
        print()

    for doc in documents:
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
    store.ocr_repair = {
        "runs_found": repair.runs_found,
        "runs_repaired": repair.runs_repaired,
        "runs_masked": repair.runs_masked,
        "pages_garbled": repair.pages_garbled,
        "summary": repair.summary(),
        "reocr": {
            "pages_targeted": reocr.pages_targeted,
            "pages_adopted": reocr.pages_adopted,
            "pages_rejected": reocr.pages_rejected,
            "pages_no_image": reocr.pages_no_image,
            "pages_ocr_failed": reocr.pages_ocr_failed,
            "engine_available": reocr.engine_available,
            "summary": reocr.summary(),
        },
    }
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
