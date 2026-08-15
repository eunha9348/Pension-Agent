-- ══════════════════════════════════════════════════════════════
--  PostgreSQL 스키마 (선택 백엔드)
-- ══════════════════════════════════════════════════════════════
--  ⚠ 현재 기본 실행 경로는 파일 기반 인덱스(app/ingest/store.py)다.
--    이 스키마는 Phase 2 계획에 따라 정의만 해 둔 것으로,
--    개발 컨테이너에 PostgreSQL 서버가 없어 실행 검증을 하지 못했다.
--    운영 전환 시 이 파일로 초기화한 뒤 app/retrieval/hybrid.py의
--    백엔드만 교체하면 된다(검색 인터페이스는 동일).
--
--  임베딩 컬럼은 정의만 하고 사용하지 않는다 — 대회 제약 1(임베딩 모델
--  허용 여부 미확인)에 따라 기본 경로는 BM25 단독이다.

CREATE EXTENSION IF NOT EXISTS vector;

-- 문서 (zip 1개 = 문서 1건)
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    title        TEXT,
    doc_type     TEXT,                    -- 투자설명서 | 제도안내 | 세제안내 | 약관
    entities     JSONB DEFAULT '{}'::jsonb,
    page_count   INTEGER,
    is_legacy    BOOLEAN DEFAULT FALSE,   -- 구법 수치 포함 의심
    legacy_markers JSONB DEFAULT '[]'::jsonb,
    source_path  TEXT,
    ingested_at  TIMESTAMPTZ DEFAULT now()
);

-- 청크 (조항 단위 우선, 표는 통째로 유지)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    page_from    INTEGER,
    page_to      INTEGER,
    locator      TEXT,                    -- 제12조 / 【연금수령한도】
    is_table     BOOLEAN DEFAULT FALSE,
    text         TEXT NOT NULL,
    entities     JSONB DEFAULT '{}'::jsonb,
    is_legacy    BOOLEAN DEFAULT FALSE,
    tsv          TSVECTOR,
    -- 임베딩은 미사용(대회 제약 1 미확인). 허용 확인 시에만 채운다.
    embedding    VECTOR(1024)
);

-- BM25 대용 전문검색 인덱스.
-- ⚠ 한국어 형태소 분석기(mecab 등)가 설치돼 있지 않으면 'simple'
--   설정이 조사 분리를 못 한다. 파일 기반 인덱스는 자체 토크나이저로
--   조사·어미를 처리하므로(app/retrieval/bm25.py) 이 차이를 유의할 것.
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_entities_idx ON chunks USING GIN (entities);

-- 임베딩 인덱스 — 허용 확인 전까지 생성하지 않는다.
-- CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE FUNCTION chunks_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('simple', COALESCE(NEW.text, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS chunks_tsv_trigger ON chunks;
CREATE TRIGGER chunks_tsv_trigger
    BEFORE INSERT OR UPDATE ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_tsv_update();
