"""인제스트·검색 테스트.

실제 제공 문서(zip)를 아직 받지 못해 mock zip으로 검증한다.
검증 대상은 문서 내용이 아니라 **파서·청킹·색인 로직**이다.
"""

from __future__ import annotations

import zipfile

import pytest

from app.ingest.build_index import ingest
from app.ingest.chunker import chunk_document
from app.ingest.make_mock_corpus import MOCK_CORPUS_DIR, build
from app.ingest.metadata import build_doc_metadata, extract_entities
from app.ingest.zip_parser import inspect, parse_zip
from app.retrieval.coarse import make_coarse_search
from app.retrieval.hybrid import make_retrieve_hybrid


@pytest.fixture(scope="module")
def corpus():
    build(MOCK_CORPUS_DIR)
    return MOCK_CORPUS_DIR


@pytest.fixture(scope="module")
def store(corpus):
    return ingest(corpus, "mock")


# ── zip 구조 ────────────────────────────────────────────────

def test_zip이지_pdf가_아니다(corpus):
    """제공 문서는 페이지별 JPEG + OCR 텍스트 zip이다."""
    p = corpus / "doc39.zip"
    assert zipfile.is_zipfile(p)
    info = inspect(p)
    assert ".jpg" in info["by_suffix"]
    assert ".txt" in info["by_suffix"]


@pytest.mark.parametrize("name,layout", [
    ("doc39", "A(디렉터리 분리)"),
    ("doc52", "B(평면)"),
    ("R2_KR5113420013", "C(JSON 통합)"),
])
def test_세가지_레이아웃을_모두_파싱한다(corpus, name, layout):
    """실제 zip 구조를 몰라 흔한 형태 3가지를 모두 흡수하도록 만들었다."""
    doc = parse_zip(corpus / f"{name}.zip")
    assert doc.layout == layout
    assert doc.page_count > 0
    assert not doc.warnings


def test_이미지는_읽지_않고_목록만_센다(corpus):
    doc = parse_zip(corpus / "doc39.zip")
    assert len(doc.image_entries) == doc.page_count


def test_zip이_아니면_경고를_남기고_넘어간다(tmp_path):
    bogus = tmp_path / "not_a_zip.zip"
    bogus.write_text("이건 zip이 아닙니다")
    doc = parse_zip(bogus)
    assert doc.page_count == 0
    assert doc.warnings


# ── 청킹 ────────────────────────────────────────────────────

def test_표는_쪼개지_않는다(corpus):
    """세율·공제 표가 잘리면 근거로서 무의미해진다."""
    doc = parse_zip(corpus / "doc52.zip")
    chunks = chunk_document(doc)
    table_chunks = [c for c in chunks if c.is_table]
    assert table_chunks

    joined = "\n".join(c.text for c in table_chunks)
    # 근속연수공제 표의 모든 구간이 같은 청크 안에 있어야 한다
    holder = next(c.text for c in chunks if "근속연수공제" in c.text and "5년 이하" in c.text)
    for row in ("5년 이하", "6년 ~ 10년", "11년 ~ 20년", "20년 초과"):
        assert row in holder, f"{row} 행이 표 청크에서 분리됨"
    assert joined


def test_청킹에서_본문이_유실되지_않는다(corpus):
    doc = parse_zip(corpus / "doc39.zip")
    chunks = chunk_document(doc)
    original = "".join(doc.full_text.split())
    rebuilt = "".join("".join(c.text for c in chunks).split())
    assert rebuilt == original


def test_locator가_각주용으로_추출된다(corpus):
    doc = parse_zip(corpus / "doc39.zip")
    chunks = chunk_document(doc)
    assert any(c.locator for c in chunks)


# ── 메타데이터 ──────────────────────────────────────────────

def test_구법_문서를_인제스트_시점에_탐지한다(store):
    meta = store.doc_meta("R2_KR514X450008")
    assert meta["legacy"]["is_legacy_suspect"] is True
    markers = {m["marker"] for m in meta["legacy"]["markers"]}
    assert "700만원" in markers


def test_현행_문서는_구법으로_잡히지_않는다(store):
    assert store.doc_meta("doc39")["legacy"]["is_legacy_suspect"] is False


def test_판매클래스_엔티티를_뽑는다():
    ent = extract_entities("C-P는 연금저축계좌를 통하여 가입한 자, C-Re는 퇴직연금 가입자")
    assert "C-P" in ent["fund_class"]


def test_여러_제도를_다루는_문서는_plan_type을_단정하지_않는다():
    """하나로 못 박으면 엔티티 충돌로 정상 근거가 걸러진다."""
    ent = extract_entities("연금저축과 IRP, DC형 퇴직연금 모두에 적용된다")
    assert "plan_type" not in ent


# ── 검색 ────────────────────────────────────────────────────

def test_L0_개략검색은_doc_id_text_score만_반환한다(store):
    """L0 결과가 답변 근거로 새어들지 않도록 최소 필드만 넘긴다."""
    coarse = make_coarse_search(store)
    hits = coarse("연금수령한도가 얼마인가요", 5)
    assert hits
    assert set(hits[0]) == {"doc_id", "text", "score"}


def test_L3_검색은_관련_근거를_상위로_올린다(store):
    retrieve = make_retrieve_hybrid(store)
    ev = retrieve({"query": "연금저축이랑 IRP 합쳐서 세액공제 얼마까지 되나요"})
    assert ev
    assert any("세액공제" in c.text for c in ev[:3])


def test_점수가_0에서_1로_정규화된다(store):
    """filter_irrelevant_evidence의 임계값(0.35)이 의미를 가지려면 필요하다."""
    retrieve = make_retrieve_hybrid(store)
    ev = retrieve({"query": "연금수령한도"})
    assert ev
    assert all(0.0 <= c.score <= 1.0 for c in ev)
    assert ev[0].score == pytest.approx(1.0)


def test_임베딩은_기본적으로_꺼져있다():
    """대회 제약 1이 확인되지 않아 BM25 단독이 기본 경로다."""
    from app.retrieval.embedding import embed_texts, embedding_enabled
    assert embedding_enabled() is False
    assert embed_texts(["아무거나"]) is None
