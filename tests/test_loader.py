"""코퍼스 로더 · 코퍼스 선택 안전장치 테스트.

━━ 이 파일이 막는 사고 ━━
"실물 문서를 넣었는데 mock 문서로 답변하는" 상황. 예전에는 zip만 찾았기 때문에
data/corpus/ 에 PDF를 넣어 두면 조용히 mock 코퍼스로 인덱스를 만들었다.
평가 당일 이러면 지어낸 문서를 근거로 답하게 된다.
"""

from __future__ import annotations

import pytest

from app.ingest.build_index import MOCK_CORPUS, choose_corpus, ingest
from app.ingest.loader import corpus_files, is_ingestible, load_file


# ── 형식별 판독 ──────────────────────────────────────────────

def test_텍스트_파일을_읽는다(tmp_path):
    p = tmp_path / "제도안내.txt"
    p.write_text("연금저축 세액공제 한도는 연 600만원입니다.", encoding="utf-8")
    doc = load_file(p)
    assert doc.page_count == 1
    assert "600만원" in doc.full_text


def test_cp949_인코딩도_읽는다(tmp_path):
    """공공기관 문서는 CP949로 저장된 경우가 흔하다."""
    p = tmp_path / "구형.txt"
    p.write_bytes("연금저축 세액공제".encode("cp949"))
    assert "연금저축" in load_file(p).full_text


def test_csv는_표_형태를_유지한다(tmp_path):
    """청커가 '표'로 인식해 통째로 유지하도록 파이프 구분으로 바꾼다."""
    p = tmp_path / "세율표.csv"
    p.write_text("구분,세율\n만 80세 이상,3.3%\n", encoding="utf-8")
    text = load_file(p).full_text
    assert "만 80세 이상 | 3.3%" in text


def test_html_태그를_제거한다(tmp_path):
    p = tmp_path / "안내.html"
    p.write_text("<html><style>body{color:red}</style>"
                 "<p>연금수령한도는 <b>1,200만원</b>입니다</p></html>",
                 encoding="utf-8")
    text = load_file(p).full_text
    assert "1,200만원" in text
    assert "color:red" not in text          # style은 버린다
    assert "<p>" not in text


def test_json에서_텍스트를_회수한다(tmp_path):
    p = tmp_path / "ocr.json"
    p.write_text('{"pages":[{"text":"연금수령연차 기산 규칙"}]}', encoding="utf-8")
    assert "연금수령연차" in load_file(p).full_text


def test_xlsx를_시트별_표로_읽는다(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "보수표.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "총보수"
    ws.append(["종류", "총보수"])
    ws.append(["C-P", "0.5440%"])
    wb.save(p)
    text = load_file(p).full_text
    assert "C-P | 0.5440%" in text
    assert "【총보수】" in text


# ── 지원하지 않는 형식은 '조용히' 넘어가지 않는다 ────────────

@pytest.mark.parametrize("name", ["문서.hwp", "보고서.docx", "장표.pptx", "스캔.jpg"])
def test_미지원_형식은_사유를_남긴다(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"binary")
    doc = load_file(p)
    assert doc.page_count == 0
    assert doc.warnings, "지원하지 않으면 반드시 사유가 남아야 한다"
    assert is_ingestible(p) is False


def test_판독기가_깨져도_전체가_죽지_않는다(tmp_path, monkeypatch):
    """네이티브 확장이 깨지면 BaseException(PanicException)이 올라온다.
    실제로 이 환경의 pypdf가 그랬다 — 한 파일 때문에 코퍼스 전체를
    못 읽는 일이 없어야 한다."""
    import app.ingest.loader as loader

    def exploding(*a, **kw):
        raise BaseException("네이티브 확장 패닉")   # noqa: TRY002

    # 디스패치 표가 함수 객체를 직접 들고 있으므로 표 자체를 갈아끼운다
    monkeypatch.setattr(loader, "_LOADERS", [({".txt"}, exploding)])
    p = tmp_path / "깨짐.txt"
    p.write_text("내용", encoding="utf-8")
    doc = loader.load_file(p)                      # 예외가 밖으로 나오면 실패
    assert doc.warnings
    assert "네이티브 확장 패닉" in doc.warnings[0]


def test_zip이_아닌_zip확장자는_경고를_남긴다(tmp_path):
    p = tmp_path / "가짜.zip"
    p.write_text("이건 zip이 아님", encoding="utf-8")
    doc = load_file(p)
    assert doc.page_count == 0
    assert doc.warnings


# ── 코퍼스 선택 안전장치 ─────────────────────────────────────

def test_실물_디렉터리에_파일이_있으면_mock으로_떨어지지_않는다(tmp_path, monkeypatch):
    """★ 가장 중요한 회귀 테스트 ★

    zip이 아닌 파일만 있어도 '실물을 넣으려는 의도'로 보고 real을 유지해야 한다.
    mock으로 대체하면 지어낸 문서로 답변하게 된다."""
    import app.ingest.build_index as bi

    real = tmp_path / "corpus"
    real.mkdir()
    (real / "투자설명서.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(bi, "REAL_CORPUS", real)

    corpus_dir, kind = bi.choose_corpus()
    assert kind == "real"
    assert corpus_dir == real


def test_실물_디렉터리가_비면_mock을_쓴다(tmp_path, monkeypatch):
    import app.ingest.build_index as bi
    monkeypatch.setattr(bi, "REAL_CORPUS", tmp_path / "없음")
    _, kind = bi.choose_corpus()
    assert kind == "mock"


def test_판독_실패_파일이_store에_기록된다(tmp_path):
    """'넣었는데 검색이 안 된다'의 원인을 /health에서 볼 수 있어야 한다."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "정상.txt").write_text("연금저축 세액공제 한도", encoding="utf-8")
    (corpus / "한글문서.hwp").write_bytes(b"binary")

    store = ingest(corpus, "real")
    assert len(store.docs) == 1
    assert len(store.skipped_files) == 1
    assert "한글문서.hwp" in store.skipped_files[0]


def test_하위_디렉터리도_훑는다(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "세제" / "2024").mkdir(parents=True)
    (corpus / "세제" / "2024" / "안내.txt").write_text("연금소득세", encoding="utf-8")
    assert len(corpus_files(corpus)) == 1


def test_mock_코퍼스는_여전히_동작한다():
    """실물이 없을 때의 개발 경로가 깨지지 않았는지 확인."""
    store = ingest(MOCK_CORPUS, "mock")
    assert len(store.docs) == 7
    assert not store.skipped_files
