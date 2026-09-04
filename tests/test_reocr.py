"""재OCR(app/ingest/reocr.py) 단위 테스트.

실물 tesseract 바이너리·실물 이미지 없이도 로직을 검증하기 위해
`_safe_import_ocr`를 가짜 엔진으로 바꿔치기한다. 검증 대상은 엔진 자체의
정확도가 아니라 — 그건 실물 서버에서만 잴 수 있다 — 아래 결정 로직이다:
  · 손상된 페이지만 대상이 되는가
  · 결과가 여전히 손상돼 있으면 버리는가
  · 결과가 원문보다 지나치게 짧으면 버리는가
  · 엔진이 없으면 원문을 건드리지 않고 정직하게 보고하는가
  · 페이지 번호에 대응하는 이미지가 없으면 정직하게 보고하는가
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.ingest import reocr
from app.ingest.zip_parser import Page, ParsedDocument


class _FakeImage:
    @staticmethod
    def open(_buf):
        return "이미지"


def _make_zip(tmp_path: Path, entries: dict[str, bytes]) -> Path:
    p = tmp_path / "doc.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return p


def test_손상된_페이지만_재OCR_대상이_된다(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path, {
        "doc1/images/page_001.jpg": b"fake-jpeg-bytes",
        "doc1/images/page_002.jpg": b"fake-jpeg-bytes",
    })
    doc = ParsedDocument(
        doc_id="doc1", source_path=str(zpath),
        image_entries=["doc1/images/page_001.jpg", "doc1/images/page_002.jpg"],
        pages=[
            Page(1, "정상 문서 페이지 내용입니다 문제없음"),
            Page(2, "여여여 기기기 손상된 페이지 ????"),
        ],
    )

    calls = []

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            calls.append(1)
            return "재OCR로 복원된 정상 문장입니다"

    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])

    assert len(calls) == 1, "손상되지 않은 1쪽까지 재OCR을 시도하면 안 된다"
    assert report.pages_targeted == 1
    assert report.pages_adopted == 1
    assert doc.pages[1].text == "재OCR로 복원된 정상 문장입니다"
    assert doc.pages[0].text == "정상 문서 페이지 내용입니다 문제없음"


def test_재OCR_결과도_여전히_손상이면_원문을_유지한다(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path, {"doc1/images/page_001.jpg": b"x"})
    doc = ParsedDocument(
        doc_id="doc1", source_path=str(zpath),
        image_entries=["doc1/images/page_001.jpg"],
        pages=[Page(1, "여여여 기기기 손상 ????")],
    )

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            return "여전히 깨짐 ??????"

    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])
    assert report.pages_rejected == 1
    assert report.pages_adopted == 0
    assert doc.pages[0].text == "여여여 기기기 손상 ????"


def test_재OCR_결과가_원문보다_지나치게_짧으면_버린다(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path, {"doc1/images/page_001.jpg": b"x"})
    original = "여여여 손상 " + ("본문 " * 50)   # 충분히 긴 원문
    doc = ParsedDocument(
        doc_id="doc1", source_path=str(zpath),
        image_entries=["doc1/images/page_001.jpg"],
        pages=[Page(1, original)],
    )

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            return "짧음"   # 원문의 20% 미만

    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])
    assert report.pages_rejected == 1
    assert doc.pages[0].text == original


def test_엔진이_없으면_원문을_그대로_두고_정직하게_보고한다(monkeypatch):
    doc = ParsedDocument(
        doc_id="doc1", source_path="무관",
        image_entries=["p1.jpg"],
        pages=[Page(1, "여여여 손상 ????")],
    )
    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (None, None, "ModuleNotFoundError: pytesseract"))

    report = reocr.reocr_documents([doc])
    assert not report.engine_available
    assert report.pages_targeted == 0
    assert doc.pages[0].text == "여여여 손상 ????"


def test_이미지가_없는_zip_전용_아닌_문서는_건드리지_않는다(monkeypatch):
    """image_entries가 비어 있으면(zip이 아닌 형식) 애초에 대상에서 뺀다."""
    doc = ParsedDocument(
        doc_id="doc1", source_path="무관",
        image_entries=[],
        pages=[Page(1, "여여여 손상 ????")],
    )

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            raise AssertionError("이미지가 없는 문서에서 OCR을 호출하면 안 된다")

    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])
    assert report.pages_targeted == 0
    assert doc.pages[0].text == "여여여 손상 ????"


def test_페이지_번호에_대응하는_이미지가_없으면_이미지없음으로_집계된다(tmp_path, monkeypatch):
    zpath = _make_zip(tmp_path, {"doc1/images/page_009.jpg": b"x"})
    doc = ParsedDocument(
        doc_id="doc1", source_path=str(zpath),
        image_entries=["doc1/images/page_009.jpg"],   # page_no=9, 문서엔 1쪽만
        pages=[Page(1, "여여여 손상 ????")],
    )

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            return "안 씀"

    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])
    assert report.pages_no_image == 1
    assert doc.pages[0].text == "여여여 손상 ????"


def test_summary_문구가_상태를_반영한다(monkeypatch):
    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (None, None, "이유"))
    report = reocr.reocr_documents([])
    assert "엔진 없음" in report.summary()


# ── PDF 경로 (2026-09-05, 실물 코퍼스가 zip이 아니라 PDF였다) ────────
#
# 임베디드 이미지를 추측하지 않고 페이지를 통째로 렌더링한다
# (raster_pdf_page → pdftoppm). 여기서는 pdftoppm 실물 없이 그 함수
# 자체를 바꿔치기해 "PDF 소스면 렌더 경로를 탄다"는 배선만 검증한다.

def test_PDF_소스는_렌더_경로를_탄다(monkeypatch):
    doc = ParsedDocument(
        doc_id="pdf1", source_path="/무관/doc.pdf",
        pages=[Page(1, "여여여 손상 ????")],
    )

    rendered = []

    def _fake_raster(pdf_path, page_no, dpi=200):
        rendered.append((pdf_path, page_no))
        return b"fake-png-bytes"

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            return "재OCR로 복원된 정상 문장입니다"

    monkeypatch.setattr(reocr, "raster_pdf_page", _fake_raster)
    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])

    assert rendered == [("/무관/doc.pdf", 1)]
    assert report.pages_adopted == 1
    assert doc.pages[0].text == "재OCR로 복원된 정상 문장입니다"


def test_PDF_렌더가_실패하면_이미지없음으로_집계된다(monkeypatch):
    doc = ParsedDocument(
        doc_id="pdf1", source_path="/무관/doc.pdf",
        pages=[Page(1, "여여여 손상 ????")],
    )

    class _FakePT:
        def get_tesseract_version(self):
            return "5.0"

        def image_to_string(self, _img, lang="kor+eng", config=""):
            raise AssertionError("렌더가 실패했으면 OCR을 호출하면 안 된다")

    monkeypatch.setattr(reocr, "raster_pdf_page", lambda *a, **k: None)
    monkeypatch.setattr(reocr, "_safe_import_ocr",
                         lambda: (_FakePT(), _FakeImage(), ""))

    report = reocr.reocr_documents([doc])
    assert report.pages_no_image == 1
    assert doc.pages[0].text == "여여여 손상 ????"


def test_pdftoppm이_없으면_None을_반환한다(monkeypatch):
    monkeypatch.setattr(reocr.shutil, "which", lambda _name: None)
    assert reocr.raster_pdf_page("/무관/doc.pdf", 1) is None


# ── tessdata_best 모델 선택 (2026-09-05, apt fast 모델 정확도 실측 후) ──

def test_tessdata_best가_있으면_config에_포함된다(tmp_path, monkeypatch):
    best_dir = tmp_path / "tessdata_best"
    best_dir.mkdir()
    (best_dir / "kor.traineddata").write_bytes(b"fake")
    monkeypatch.setattr(reocr, "_TESSDATA_BEST_DIR", str(best_dir))

    cfg = reocr._tesseract_config()
    assert "--tessdata-dir" in cfg
    assert str(best_dir) in cfg


def test_tessdata_best가_없으면_config이_비어있다(tmp_path, monkeypatch):
    monkeypatch.setattr(reocr, "_TESSDATA_BEST_DIR", str(tmp_path / "없음"))
    assert reocr._tesseract_config() == ""
