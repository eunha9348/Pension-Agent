"""재OCR 가능성 진단 — 페이지 렌더링이 실제로 원문보다 나은 결과를 내는가.

━━ 이력 (2026-09-05) ━━
1차 진단은 pypdf의 `page.images`(임베디드 이미지 객체)로 "가장 큰 이미지 =
그 페이지"라고 추측했다가 실패했다 — 서로 다른 두 페이지가 문서 전체에
반복되는 배경/서식 그래픽을 공유하고 있어, 완전히 다른 두 페이지의 재OCR
결과가 글자 하나 안 틀리고 똑같이 나왔다(doc33). 지금은 `reocr.py`의
`raster_pdf_page()`(pdftoppm으로 페이지를 실제 화면 그대로 렌더링)를
그대로 써서, 이 스크립트와 실제 인제스트 파이프라인이 **같은 경로**를
검증하게 한다.

━━ 무엇을 보는가 ━━
  1. 깨진 페이지(looks_garbled)를 가진 PDF를 고른다
  2. 그 페이지를 렌더링해 tesseract로 다시 읽는다
  3. 원문과 나란히 보여준다 — 렌더가 되느냐와 결과가 읽을 만하냐는
     다른 질문이라 항상 같이 본다

    python -m scripts.reocr_probe                 # 기본 5문서
    python -m scripts.reocr_probe --limit 20
    python -m scripts.reocr_probe --pages-per-doc 3
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.ingest.loader import iter_documents
from app.ingest.ocr_repair import looks_garbled
from app.ingest.reocr import _safe_import_ocr, raster_pdf_page

DEFAULT_CORPUS = REPO_ROOT / "data" / "corpus"
_PREVIEW = 300


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="재OCR(페이지 렌더링) 품질 진단")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--limit", type=int, default=5, help="확인할 문서 수 (기본 5)")
    ap.add_argument("--pages-per-doc", type=int, default=2,
                    help="문서당 확인할 깨진 페이지 수 (기본 2)")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"❌ {corpus} 가 없습니다.")
        return 1

    pytesseract, Image, err = _safe_import_ocr()
    if pytesseract is None:
        print(f"❌ 재OCR 엔진 없음: {err}")
        print("   (pytesseract·tesseract-ocr-kor·poppler-utils 필요)")
        return 1

    print("═" * 62)
    print(" 재OCR 가능성 진단 (페이지 렌더링)")
    print("═" * 62)

    docs_total = docs_garbled = checked = 0
    render_ok = render_fail = 0
    still_garbled = 0

    for doc in iter_documents(corpus):
        docs_total += 1
        src = Path(doc.source_path)
        if src.suffix.lower() != ".pdf":
            continue
        bad = [pg for pg in doc.pages if looks_garbled(pg.text)]
        if not bad:
            continue
        docs_garbled += 1
        if checked >= args.limit:
            continue
        checked += 1

        print(f"\n── {doc.doc_id}  (깨진 페이지 {len(bad)}/{doc.page_count}) ──")

        for pg in bad[:args.pages_per_doc]:
            raw = raster_pdf_page(doc.source_path, pg.page_no)
            if raw is None:
                render_fail += 1
                print(f"   p{pg.page_no}: 렌더 실패")
                continue
            render_ok += 1

            print(f"   p{pg.page_no}: 렌더 {len(raw):,}B")
            print(f"      [원문 OCR] {pg.text.strip()[:_PREVIEW]!r}")
            try:
                img = Image.open(io.BytesIO(raw))
                new_text = pytesseract.image_to_string(img, lang="kor+eng")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:                          # noqa: BLE001
                print(f"      [재OCR] 실패 ({type(e).__name__}: {e})")
                continue
            garbled_still = looks_garbled(new_text)
            if garbled_still:
                still_garbled += 1
            mark = "깨짐 남음" if garbled_still else "깨짐 없음"
            print(f"      [재OCR·{mark}] {new_text.strip()[:_PREVIEW]!r}")

    print("\n" + "═" * 62)
    print(f" 문서 {docs_total}건 · 깨진 PDF {docs_garbled}건 · 확인 {checked}건")
    print(f" 렌더 성공 {render_ok} · 렌더 실패 {render_fail} · "
          f"재OCR도 여전히 손상 {still_garbled}")
    if render_ok:
        print("\n ✅ 페이지 렌더링은 됩니다. 위 [원문 OCR] vs [재OCR]를")
        print("    직접 눈으로 비교해 실제 내용과 일치하는지 판단하십시오 —")
        print("    '깨짐 없음'이 곧 '내용이 맞다'는 뜻은 아닙니다(그럴듯한")
        print("    잡음도 이 판정은 통과합니다. 2026-09-05 doc26 사례 참조).")
    else:
        print("\n ❌ 렌더가 전혀 안 됩니다 — poppler-utils(pdftoppm) 설치를 확인하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
