"""판독 실패 페이지 재OCR — 앵커 대조로 못 메운 자리를 원본 이미지에서 다시 읽는다.

━━ 왜 필요한가 (2026-09-04 실측) ━━
교차 문서 앵커 복원(`ocr_repair.py`)은 판독 실패가 페이지당 평균 26.7건으로
몰린 곳에서 구조적 한계에 부딪힌다 — 앞뒤 8자 안에 다른 손상이 끼어 있으면
앵커 자체를 만들 수 없다("판독 실패 앵커가 이웃 손상을 물면 대조가 항상
실패한다" 참조). 실측 5,279건 중 3,872건(73.3%)이 이 사유로 정직하게
격리됐다 — 코퍼스에 그 구절이 없어서가 아니라, 앵커를 만들 공간 자체가
없어서다. 교차 문서 대조를 아무리 정교하게 다듬어도 이 유형은 풀리지 않는다.

━━ 왜 이제는 가능한가 ━━
`zip_parser.py`가 처음부터 명시한 전제를 보면, 제공 zip은 "페이지별 JPEG
이미지 + 그 OCR 텍스트"다. 이미지는 항상 함께 들어 있고, 그동안은 "OCR을
우리가 다시 돌리지 않는다"는 결정으로 읽지 않았을 뿐이다(재OCR 없이도
복원이 됐기 때문이었다). 교차 문서 대조(다른 문서에서 같은 구절을
빌려오는 것)와 재OCR은 날조 위험의 성격이 다르다 — 코퍼스 밖에서 값을
만들어 오는 게 아니라, **그 페이지 자신의 원본 이미지**를 다시 읽는
것뿐이다.

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

━━ 이 뒤에도 안전망은 그대로 ━━
재OCR도 완벽하지 않을 수 있다(엔진이 다른 방식으로 틀릴 수 있다). 그래서
이 모듈이 끝난 뒤에도 `ocr_repair.repair_documents()`가 그대로 돈다 —
재OCR로 못 고친 페이지, 혹은 재OCR이 새로 만든 손상 패턴까지 같은 교차
문서 대조·격리 절차를 한 번 더 거친다. 이 모듈은 그 앞 단계에서 최대한
많은 페이지를 정직한 방법으로 미리 치워 두는 역할이다.

━━ 엔진이 없으면 ━━
`pytesseract`·시스템 `tesseract-ocr`(+`tesseract-ocr-kor`) 바이너리가 없어도
서비스는 정상 기동한다. 재OCR 단계만 건너뛰고 `engine_available=False`로
정직하게 보고한다 — 조용히 넘어가지 않는다("LLM 실패는 예외 없이 조용히
축퇴한다 — 세어서 노출할 것"과 같은 원칙).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from app.ingest.ocr_repair import looks_garbled
from app.ingest.zip_parser import ParsedDocument, _page_no_from_name

# 새 텍스트가 원문의 이 비율보다 짧으면 신뢰하지 않는다 — 이미지를
# 잘못 짚었거나 엔진이 사실상 실패한 신호로 본다.
MIN_LENGTH_RATIO = 0.2


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
    pages_no_image: int = 0        # 대응하는 이미지 엔트리를 못 찾음
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
                    f"(pytesseract·tesseract-ocr-kor 필요)")
        if not self.pages_targeted:
            return "재OCR 대상 없음"
        return (f"재OCR 대상 {self.pages_targeted}쪽 → "
                f"채택 {self.pages_adopted} · 거부(여전히 손상/과도 축소) "
                f"{self.pages_rejected} · 이미지 없음 {self.pages_no_image} · "
                f"zip 오류 {self.pages_zip_error} · 엔진 실패 {self.pages_ocr_failed}")


def _match_image(doc: ParsedDocument, page_no: int) -> str | None:
    """페이지 번호에 대응하는 이미지 엔트리 이름을 찾는다."""
    candidates = [name for name in doc.image_entries
                  if _page_no_from_name(name, -1) == page_no]
    if not candidates:
        return None
    # 흔치 않지만 여러 개가 걸리면, 임의 순서보다는 재현 가능한 쪽이 낫다.
    return sorted(candidates)[0]


def reocr_documents(documents: list[ParsedDocument]) -> ReocrReport:
    """손상이 확인된 페이지만 골라 원본 이미지로 재OCR한다. 제자리 수정."""
    report = ReocrReport()
    pytesseract, Image, err = _safe_import_ocr()
    if pytesseract is None:
        report.engine_available = False
        report.engine_error = err
        return report

    for doc in documents:
        if not doc.image_entries:
            continue
        garbled_pages = [pg for pg in doc.pages if looks_garbled(pg.text)]
        if not garbled_pages:
            continue

        try:
            zf = zipfile.ZipFile(doc.source_path)
        except (OSError, zipfile.BadZipFile):
            report.pages_zip_error += len(garbled_pages)
            continue

        with zf:
            for pg in garbled_pages:
                report.pages_targeted += 1
                entry = _match_image(doc, pg.page_no)
                if entry is None:
                    report.pages_no_image += 1
                    continue
                try:
                    raw = zf.read(entry)
                    img = Image.open(io.BytesIO(raw))
                    new_text = pytesseract.image_to_string(img, lang="kor+eng")
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as e:                       # noqa: BLE001
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
