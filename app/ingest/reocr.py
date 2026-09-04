"""판독 실패 페이지 재OCR — 앵커 대조로 못 메운 자리를 원본 이미지에서 다시 읽는다.

━━ 왜 필요한가 (2026-09-04 실측) ━━
교차 문서 앵커 복원(`ocr_repair.py`)은 판독 실패가 페이지당 평균 26.7건으로
몰린 곳에서 구조적 한계에 부딪힌다 — 앞뒤 8자 안에 다른 손상이 끼어 있으면
앵커 자체를 만들 수 없다("판독 실패 앵커가 이웃 손상을 물면 대조가 항상
실패한다" 참조). 실측 5,279건 중 3,872건(73.3%)이 이 사유로 정직하게
격리됐다 — 코퍼스에 그 구절이 없어서가 아니라, 앵커를 만들 공간 자체가
없어서다. 교차 문서 대조를 아무리 정교하게 다듬어도 이 유형은 풀리지 않는다.

━━ 페이지를 어떻게 이미지로 얻는가 (2026-09-05 정정) ━━
처음에는 "제공 zip은 페이지별 JPEG + OCR 텍스트"라는 `zip_parser.py`의
전제를 그대로 믿고 zip 안의 이미지 엔트리를 찾았다. 실물을 열어 보니
코퍼스는 zip이 아니라 **PDF 156건**이었다.

PDF 안에도 이미지가 있긴 하다 — pypdf의 `page.images`로 꺼낼 수 있다.
하지만 "페이지 안에서 가장 큰 임베디드 이미지 = 그 페이지의 스캔본"이라는
가정이 실물에서 깨졌다: 서로 다른 두 페이지가 같은 크기의 이미지 3개를
공유하고 있었고(문서 전체에 반복되는 배경/서식 그래픽), 그걸 "가장 크다"는
이유로 집었더니 완전히 다른 두 페이지가 똑같은 재OCR 결과를 냈다(doc33,
2026-09-05 실측).

그래서 임베디드 이미지를 추측해서 고르지 않는다. **PDF 페이지를 실제로
렌더링**(사람이 보는 그대로 픽셀로 그려내는 것)해서, 그 페이지에 어떤
이미지 객체가 몇 개 들어있든 상관없이 **화면에 보이는 최종 결과**를
얻는다. `pdftoppm`(poppler-utils, 시스템 바이너리)을 서브프로세스로
부른다 — PyMuPDF 같은 파이썬 라이브러리도 같은 일을 하지만 AGPL
라이선스라 대회 제출물에는 들이지 않는다. poppler-utils는 GPL이지만
서브프로세스로 호출만 하므로(코드에 링크하지 않음) 배포 라이선스에
영향이 없다.

zip 코퍼스(레이아웃 A: 페이지별 JPEG)에 대한 경로도 남겨 뒀다 — 대회가
자료를 바꿔 줄 가능성, 그리고 이미 작성된 테스트와의 하위 호환을 위해서다.

━━ 언제만 적용하는가 (오탐이 미탐보다 나쁘다) ━━
전체 6,708페이지를 다시 OCR하지 않는다. `looks_garbled()`로 이미 손상이
확인된 페이지만 대상이다(다른 페이지는 손댈 이유가 없다). 그리고 재OCR
결과를 무조건 채택하지 않는다:
  · 새 텍스트도 여전히 손상 패턴(`looks_garbled`)이면 — 재OCR도 실패한
    것이므로 원문을 유지하고 이후 교차 문서 복원에 맡긴다.
  · 새 텍스트 길이가 원문의 20% 미만이면 — 이미지를 잘못 짚었거나 엔진이
    사실상 실패한 신호로 보고 원문을 유지한다.
어느 쪽도 아니면 채택한다 — 손상 표식이 사라졌다는 것 자체가 이 페이지
안에서는 재OCR이 원문보다 낫다고 볼 근거다.

⚠️ **이 조건으로도 못 거르는 실패가 실측됐다.** 반복 문자가 아니라
"SASS ATMO"·"wycooas oye sane" 같은 그럴듯해 보이는 잡음을 내놓는
경우(doc26)는 `looks_garbled`를 통과한다. 이건 이미지를 잘못 골라서가
아니라 엔진의 인식 정확도 자체가 그 스캔 품질에서 낮기 때문이라, 페이지
렌더링으로 고쳐지는 종류가 아니다. 그래서 **`build_index.ingest()`는
아직 이 모듈을 호출하지 않는다** — 이 실패를 걸러낼 결정론적 기준을
검증 없이 배선하면 그 자체가 오탐(날조 위험)이 된다. 실물 검증은
`scripts/reocr_probe.py`로 한다.

━━ 이 뒤에도 안전망은 그대로 ━━
재OCR도 완벽하지 않을 수 있다(엔진이 다른 방식으로 틀릴 수 있다). 그래서
이 모듈이 끝난 뒤에도 `ocr_repair.repair_documents()`가 그대로 돈다 —
재OCR로 못 고친 페이지, 혹은 재OCR이 새로 만든 손상 패턴까지 같은 교차
문서 대조·격리 절차를 한 번 더 거친다. 이 모듈은 그 앞 단계에서 최대한
많은 페이지를 정직한 방법으로 미리 치워 두는 역할이다.

━━ 엔진이 없으면 ━━
`pytesseract`·시스템 `tesseract-ocr`(+`tesseract-ocr-kor`) 바이너리,
`pdftoppm`(poppler-utils) 바이너리가 없어도 서비스는 정상 기동한다.
재OCR 단계만 건너뛰고 `engine_available=False`로 정직하게 보고한다 —
조용히 넘어가지 않는다("LLM 실패는 예외 없이 조용히 축퇴한다 — 세어서
노출할 것"과 같은 원칙).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.ingest.ocr_repair import looks_garbled
from app.ingest.zip_parser import ParsedDocument, _page_no_from_name

# 새 텍스트가 원문의 이 비율보다 짧으면 신뢰하지 않는다 — 이미지를
# 잘못 짚었거나 엔진이 사실상 실패한 신호로 본다.
MIN_LENGTH_RATIO = 0.2

# PDF 페이지 렌더 해상도. 낮으면 OCR 정확도가 떨어지고, 높으면 느려지고
# 메모리를 더 쓴다. 300dpi는 스캔 문서 OCR의 통상 권장 상한이다 — 첫
# 실측(200dpi)에서 결과 대부분이 잡음이라 해상도를 올려 재시도한다.
RENDER_DPI = 300
_RENDER_TIMEOUT_SEC = 30

# apt의 tesseract-ocr-kor는 속도 우선(fast) LSTM 모델이라 정확도가 낮다.
# Dockerfile이 공식 고정밀(tessdata_best) 한국어 모델을 여기 받아 둔다.
# 없으면(로컬 개발 환경, 다운로드 실패 등) 조용히 apt 기본 모델로 대체한다.
_TESSDATA_BEST_DIR = "/opt/tessdata_best"


def _tesseract_config() -> str:
    """tessdata_best가 있으면 그걸 쓰도록 pytesseract config 문자열을 만든다."""
    if os.path.isfile(os.path.join(_TESSDATA_BEST_DIR, "kor.traineddata")):
        return f'--tessdata-dir "{_TESSDATA_BEST_DIR}"'
    return ""


def _safe_import_ocr():
    """선택적 의존성(pytesseract + 시스템 tesseract 바이너리)을 안전하게 불러온다.

    ⚠️ `except Exception`으로는 부족하다 — 네이티브 확장이 깨져 있으면
       `BaseException`을 상속한 예외가 올라올 수 있다(`loader._safe_import`와
       같은 이유). 재OCR 하나가 고장 났다고 인제스트 전체가 죽으면 안 된다.
    """
    try:
        import pytesseract
        from PIL import Image
        pytesseract.get_tesseract_version()   # 바이너리 자체가 없으면 여기서 실패
        return pytesseract, Image, ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:                                  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


@dataclass
class ReocrReport:
    pages_targeted: int = 0        # looks_garbled였던 페이지 수
    pages_no_image: int = 0        # 렌더 실패 · 대응 이미지 못 찾음
    pages_zip_error: int = 0       # zip을 다시 열지 못함
    pages_ocr_failed: int = 0      # OCR 호출 자체가 예외를 던짐
    pages_rejected: int = 0        # 결과를 신뢰 조건 미달로 버림 (여전히 손상 · 너무 짧음)
    pages_adopted: int = 0         # 재OCR 결과를 채택함
    engine_available: bool = True
    engine_error: str = ""
    samples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.engine_available:
            return (f"재OCR 엔진 없음 — {self.engine_error} "
                    f"(pytesseract·tesseract-ocr-kor·poppler-utils 필요)")
        if not self.pages_targeted:
            return "재OCR 대상 없음"
        return (f"재OCR 대상 {self.pages_targeted}쪽 → "
                f"채택 {self.pages_adopted} · 거부(여전히 손상/과도 축소) "
                f"{self.pages_rejected} · 이미지 없음 {self.pages_no_image} · "
                f"zip 오류 {self.pages_zip_error} · 엔진 실패 {self.pages_ocr_failed}")


def _match_image(doc: ParsedDocument, page_no: int) -> Optional[str]:
    """(zip 코퍼스 전용) 페이지 번호에 대응하는 이미지 엔트리 이름을 찾는다."""
    candidates = [name for name in doc.image_entries
                  if _page_no_from_name(name, -1) == page_no]
    if not candidates:
        return None
    # 흔치 않지만 여러 개가 걸리면, 임의 순서보다는 재현 가능한 쪽이 낫다.
    return sorted(candidates)[0]


def raster_pdf_page(pdf_path: str, page_no: int,
                    dpi: int = RENDER_DPI) -> Optional[bytes]:
    """PDF의 특정 페이지를 실제 화면 그대로 렌더링해 PNG 바이트로 반환한다.

    임베디드 이미지 객체를 추측해서 고르지 않는다 — 그 페이지에 이미지가
    몇 개 있든, 벡터 그래픽이 섞여 있든 상관없이 **최종적으로 보이는 픽셀**을
    얻는다. `pdftoppm`이 없거나 실패하면 None (엔진 부재/실패로 정직하게
    보고되도록 예외를 삼키지 않고 호출자가 판단하게 한다).
    """
    if shutil.which("pdftoppm") is None:
        return None
    with tempfile.TemporaryDirectory() as td:
        prefix = str(Path(td) / "page")
        try:
            subprocess.run(
                ["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                 "-r", str(dpi), "-png", "-singlefile", pdf_path, prefix],
                check=True, capture_output=True, timeout=_RENDER_TIMEOUT_SEC)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:                                    # noqa: BLE001
            return None
        out = Path(prefix + ".png")
        if not out.exists():
            return None
        return out.read_bytes()


def _page_image_bytes(doc: ParsedDocument, page_no: int) -> tuple[Optional[bytes], str]:
    """이 페이지의 원본 이미지를 구한다. 실패하면 (None, 실패사유코드)."""
    src = doc.source_path
    if src.lower().endswith(".pdf"):
        data = raster_pdf_page(src, page_no)
        if data is None:
            return None, "no_image"
        return data, ""

    entry = _match_image(doc, page_no)
    if entry is None:
        return None, "no_image"
    try:
        with zipfile.ZipFile(src) as zf:
            return zf.read(entry), ""
    except (OSError, zipfile.BadZipFile):
        return None, "zip_error"


def reocr_documents(documents: list[ParsedDocument]) -> ReocrReport:
    """손상이 확인된 페이지만 골라 원본 이미지로 재OCR한다. 제자리 수정."""
    report = ReocrReport()
    pytesseract, Image, err = _safe_import_ocr()
    if pytesseract is None:
        report.engine_available = False
        report.engine_error = err
        return report

    for doc in documents:
        is_pdf = doc.source_path.lower().endswith(".pdf")
        if not is_pdf and not doc.image_entries:
            continue        # 이미지 소스가 없는 형식(PDF도 zip도 아님)

        garbled_pages = [pg for pg in doc.pages if looks_garbled(pg.text)]
        if not garbled_pages:
            continue

        for pg in garbled_pages:
            report.pages_targeted += 1
            raw, fail = _page_image_bytes(doc, pg.page_no)
            if fail == "zip_error":
                report.pages_zip_error += 1
                continue
            if raw is None:
                report.pages_no_image += 1
                continue

            try:
                img = Image.open(io.BytesIO(raw))
                new_text = pytesseract.image_to_string(
                    img, lang="kor+eng", config=_tesseract_config())
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:                           # noqa: BLE001
                report.pages_ocr_failed += 1
                if len(report.samples) < 12:
                    report.samples.append(
                        f"{doc.doc_id} p{pg.page_no}: OCR 실패 "
                        f"({type(e).__name__}: {e})")
                continue

            if looks_garbled(new_text) or \
                    len(new_text) < len(pg.text) * MIN_LENGTH_RATIO:
                report.pages_rejected += 1
                continue

            if len(report.samples) < 12:
                report.samples.append(
                    f"{doc.doc_id} p{pg.page_no}: {len(pg.text)}자 → "
                    f"{len(new_text)}자로 재OCR 채택")
            pg.text = new_text
            report.pages_adopted += 1

    return report
