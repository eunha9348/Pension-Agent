"""재OCR 가능성 진단 — PDF에서 페이지 이미지를 꺼낼 수 있는가.

━━ 왜 필요한가 (2026-09-05) ━━
`reocr.py`는 "제공 문서는 페이지별 JPEG + OCR 텍스트 zip"이라는 전제로
만들었는데, 실물 코퍼스는 **PDF 156건 + xlsx 2건**이었다. zip 전제가
실물과 달라 재OCR이 대상 0건으로 놀고 있었다(`/health`의
`ocr_repair.reocr.pages_targeted: 0`).

PDF에도 길이 있다 — 스캔본 PDF는 **페이지 이미지 + 그 위에 얹은 OCR
텍스트 레이어** 구조라, PDF 안에 원본 이미지가 그대로 박혀 있다. 그걸
꺼내 다시 읽으면 재OCR이 된다. 반대로 born-digital PDF라면 꺼낼 이미지가
없고, 그때는 재OCR이 원천적으로 불가능하다.

이 스크립트는 그 둘을 **실측으로** 가른다. 추측으로 구현부터 하면
30분을 버리고 다시 여기로 돌아오게 된다.

━━ 무엇을 보는가 ━━
  1. 깨진 페이지(looks_garbled)를 가진 PDF를 고른다
  2. 그 페이지에서 pypdf로 이미지가 실제로 나오는지 센다
  3. 나오면 **실제로 tesseract에 넣어** 원문과 나란히 보여준다
     — 꺼낼 수 있느냐와 읽을 만하냐는 다른 질문이다

    python -m scripts.reocr_probe                 # 기본 5문서
    python -m scripts.reocr_probe --limit 20
    python -m scripts.reocr_probe --no-ocr        # 이미지 유무만 (빠름)
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.ingest.loader import iter_documents
from app.ingest.ocr_repair import looks_garbled

DEFAULT_CORPUS = REPO_ROOT / "data" / "corpus"
# 원문·재OCR 비교 출력 길이. 너무 길면 터미널에서 못 읽는다.
_PREVIEW = 300


def _load_pypdf():
    try:
        from pypdf import PdfReader
        return PdfReader, ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:                                  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _load_ocr():
    try:
        import pytesseract
        from PIL import Image
        pytesseract.get_tesseract_version()
        return pytesseract, Image, ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:                                  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PDF 재OCR 가능성 진단")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--limit", type=int, default=5,
                    help="이미지를 확인할 문서 수 (기본 5)")
    ap.add_argument("--no-ocr", action="store_true",
                    help="이미지 유무만 보고 실제 OCR은 건너뛴다")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"❌ {corpus} 가 없습니다.")
        return 1

    PdfReader, pdf_err = _load_pypdf()
    if PdfReader is None:
        print(f"❌ pypdf를 불러오지 못했습니다: {pdf_err}")
        return 1

    pytesseract = Image = None
    ocr_err = ""
    if not args.no_ocr:
        pytesseract, Image, ocr_err = _load_ocr()
        if pytesseract is None:
            print(f"⚠️  OCR 엔진 없음({ocr_err}) — 이미지 유무만 봅니다.\n")

    print("═" * 62)
    print(" 재OCR 가능성 진단")
    print("═" * 62)

    docs_total = docs_garbled = 0
    checked = 0
    pages_with_image = pages_without_image = 0

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
        try:
            reader = PdfReader(str(src))
        except Exception as e:                                  # noqa: BLE001
            print(f"   ❌ PDF 열기 실패: {type(e).__name__}: {e}")
            continue

        for pg in bad[:2]:                     # 문서당 2쪽까지만 본다
            i = pg.page_no - 1
            if not (0 <= i < len(reader.pages)):
                print(f"   p{pg.page_no}: PDF 페이지 범위 밖 (총 {len(reader.pages)}쪽)")
                continue
            try:
                images = list(reader.pages[i].images)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:                          # noqa: BLE001
                print(f"   p{pg.page_no}: 이미지 추출 실패 "
                      f"({type(e).__name__}: {e})")
                pages_without_image += 1
                continue

            if not images:
                pages_without_image += 1
                print(f"   p{pg.page_no}: 이미지 0건 "
                      f"← born-digital이면 재OCR 불가")
                continue

            pages_with_image += 1
            sizes = ", ".join(f"{len(im.data):,}B" for im in images[:3])
            print(f"   p{pg.page_no}: 이미지 {len(images)}건 ({sizes})")
            print(f"      [원문 OCR] {pg.text.strip()[:_PREVIEW]!r}")

            if pytesseract is None:
                continue
            big = max(images, key=lambda im: len(im.data))
            try:
                img = Image.open(io.BytesIO(big.data))
                new = pytesseract.image_to_string(img, lang="kor+eng")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:                          # noqa: BLE001
                print(f"      [재OCR] 실패 ({type(e).__name__}: {e})")
                continue
            mark = "깨짐 남음" if looks_garbled(new) else "깨짐 없음"
            print(f"      [재OCR·{mark}] {new.strip()[:_PREVIEW]!r}")

    print("\n" + "═" * 62)
    print(f" 문서 {docs_total}건 · 깨진 PDF {docs_garbled}건 · 확인 {checked}건")
    print(f" 이미지 있는 페이지 {pages_with_image} · 없는 페이지 {pages_without_image}")
    if pages_with_image:
        print("\n ✅ PDF에서 페이지 이미지를 꺼낼 수 있습니다 — 재OCR 경로가 있습니다.")
        print("    위의 [원문 OCR] vs [재OCR] 를 비교해 품질을 판단하십시오.")
    elif pages_without_image:
        print("\n ❌ 깨진 페이지에서 이미지가 나오지 않습니다 — born-digital로 보입니다.")
        print("    원본 이미지가 없으므로 재OCR은 불가능하고, 교차 문서 복원과")
        print("    '(판독불가)' 격리가 할 수 있는 전부입니다.")
    else:
        print("\n ⚠️  깨진 PDF를 찾지 못했습니다 — --limit 을 올리거나 코퍼스를 확인하십시오.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
