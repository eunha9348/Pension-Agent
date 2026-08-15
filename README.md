# 연금 Agent

제10회 미래에셋증권 AI Festival · 연금 Agent 트랙 출품작.
자연어 연금 질의에 대해 **제공 문서 근거로만** 조회·분석·설명하는 AI 에이전트.

> ⚠️ **현재 상태: CLOVA Studio 실연동 미완료 (mock 모드로 동작 중)**
> 개발 컨테이너에서 `clovastudio.stream.ntruss.com` 접속이 네트워크 정책으로
> 차단돼 실제 호출을 검증하지 못했습니다. `.env`에 `CLOVA_API_KEY`를 넣으면
> 코드 수정 없이 실제 호출로 전환됩니다. 자세한 내용은 [PROGRESS.md](PROGRESS.md).

---

## 1. 빠른 시작

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # ← CLOVA_API_KEY 를 여기에 넣으세요

python -m app.ingest.build_index      # 문서 인제스트 + 검색 인덱스 생성
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

평가 API 호출:

```bash
curl "http://localhost:8000/answer?question_id=Q-001&question=연금저축과 IRP 합쳐서 세액공제 얼마까지 되나요"
```

응답은 항상 5필드 JSON입니다 — `question_id`, `question`, `retrieved_context`,
`think_trace`, `answer`. 내부에서 어떤 예외가 나도 이 스키마는 깨지지 않습니다.

### Docker

```bash
docker build -t pension-agent .
docker run --rm -p 8000:8000 --env-file .env pension-agent
```

### 테스트

```bash
python -m pytest -q
```

---

## 2. CLOVA API 키 넣는 자리 (단 한 곳)

`.env` 파일의 `CLOVA_API_KEY` **한 곳만** 채우면 됩니다.

```
CLOVA_API_KEY=<여기에 발급받은 키>
CLOVA_ENDPOINT=https://clovastudio.stream.ntruss.com/testapp/v1/chat-completions/HCX-005
LLM_MODE=auto
```

- `LLM_MODE=auto`(기본) — 키가 있으면 실제 호출, 없으면 자동으로 mock
- 키를 넣은 뒤 **가장 먼저** 스모크 테스트로 응답 형식과 지연시간을 측정하세요:

```bash
python -m app.llm.smoke_test
```

이 스크립트는 요청/응답 원문과 지연시간을 출력하고, 측정된 지연을 근거로
단계별 타임아웃을 어떻게 재배분해야 하는지 함께 제안합니다.

mock 모드로 동작 중일 때는 API 응답의 `think_trace` 서두와 서버 기동 로그에
`[MOCK LLM]` 배너가 찍히므로, 실연동 여부를 눈으로 바로 확인할 수 있습니다.

---

## 3. 아키텍처

```
L0  사전 검색      app/retrieval/coarse.py + core/grounding_retrieval.py   LLM 없음
L1  질의 분석      app/analysis/query_spec.py                              HyperCLOVA X
1.5 계획 감사      core/supervisory_board.supervise_plan()                 LLM 없음
L2  함정 감지      core/trap_rules.py (26종)                               LLM 없음
L3  Exploration    app/retrieval/hybrid.py (BM25, 임베딩 경로는 격리)      LLM 없음
L4  Exploitation   app/pipeline.py _exploit()                              LLM 없음
L5  Prediction     core/pension_calc_functions.py (15종)                   LLM 없음
L5' Supervisor     app/generation/answer_prompt.py                         HyperCLOVA X
L6  감독 이사회    core/supervisory_board.supervise_hybrid()               HyperCLOVA X
```

LLM 호출은 **L1 · L5' · L6 세 곳뿐**입니다. 나머지는 전부 결정론적 코드입니다.

원칙 5가지는 [CLAUDE.md](CLAUDE.md)에 있으며, 구현 시 위반 금지입니다.
특히 **L6의 권한 계층**(LLM은 심각도를 올릴 수만 있음)은 리팩터링 시에도
`merge_supervision()`에서 반드시 보존해야 합니다.

---

## 4. 검색 백엔드

기본은 **파일 기반 인덱스 + 순수 파이썬 BM25**입니다. 외부 DB가 필요 없습니다.

- 인덱스 위치: `data/index/`
- 재빌드: `python -m app.ingest.build_index`

PostgreSQL(pgvector/tsvector)로 전환하려면 `sql/schema.sql`로 초기화하고
`.env`의 `DATABASE_URL`을 채우십시오. 검색 인터페이스가 동일하므로
`app/retrieval/hybrid.py`의 백엔드만 교체하면 됩니다.

### 임베딩 사용 여부

대회 제약 "LLM은 HyperCLOVA X만 사용"에 **임베딩 모델이 포함되는지 미확인**이라,
기본값은 **임베딩 없이 BM25 단독**입니다(가장 보수적인 가정).

허용이 확인되면 `.env`의 `USE_EMBEDDING=true`로 바꾸고
`app/retrieval/embedding.py`의 `embed_texts()` 한 함수만 구현하면 됩니다.
임베딩 의존 코드는 전부 그 파일 하나에 격리돼 있습니다.

---

## 5. 문서 인제스트

제공 문서는 **PDF가 아니라 페이지별 JPEG + OCR 텍스트가 담긴 zip**입니다.
일반 PDF 라이브러리로 열면 안 됩니다 — `zipfile`로 구조를 먼저 확인합니다.

```bash
python -m app.ingest.inspect_zip data/corpus/doc39.zip   # 구조만 덤프
python -m app.ingest.build_index                          # 전체 인제스트
```

실제 문서를 받으면 `data/corpus/`에 zip을 그대로 넣고 재빌드하면 됩니다.
현재는 구조를 흉내낸 **mock zip**(`python -m app.ingest.make_mock_corpus`)으로
파서를 검증한 상태입니다.

---

## 6. 디렉터리

```
app/
  config.py            환경 설정
  core/                이미 검증된 7개 모듈 (계산·함정·검증·감독·인용·L0·커버리지)
  llm/clova.py         CLOVA Studio 클라이언트 + mock 폴백
  ingest/              zip 파서 · 청킹 · 메타데이터 · 인덱스 빌드
  retrieval/           BM25 · L0 개략검색 · L3 하이브리드 검색
  analysis/            L1 질의 분석 · 슬롯 매칭 · 계산 인자 조립
  generation/          L5' 답변 프롬프트 · 근거 검증 래퍼
  pipeline.py          6계층 통합
  main.py              GET /answer · GET /health
sql/schema.sql         PostgreSQL 스키마 (선택)
tests/                 회귀 테스트
```
