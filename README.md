# 연금 Agent

제10회 미래에셋증권 AI Festival · 연금 Agent 트랙 출품작.
자연어 연금 질의에 대해 **제공 문서 근거로만** 조회·분석·설명하는 AI 에이전트.

> ✅ **현재 상태: CLOVA Studio 실연동 확인 완료 (2026-08-18)**
> 실제 코퍼스(158문서)·실제 API 키로 L1·L5'·L6 세 곳 모두 HyperCLOVA X
> 실호출이 성공하는 것을 배포 환경에서 확인했습니다. 자세한 내용은
> [PROGRESS.md](PROGRESS.md).

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

### Docker (권장)

```bash
cp .env.example .env                              # CLOVA_API_KEY 입력
mkdir -p data/corpus && cp <제공 문서> data/corpus/

docker compose --profile tools run --rm smoke     # ① 키 검증
docker compose --profile tools run --rm check     # ② 문서 판독 확인
docker compose up -d --build                      # ③ 구동
```

문서는 볼륨으로 마운트되므로, 문서를 바꿔도 이미지를 다시 빌드할 필요가 없습니다
(`... run --rm reindex` 후 `docker compose restart`).
자세한 내용은 [docs/DEPLOY.md](docs/DEPLOY.md).

### 테스트

```bash
python -m pytest -q
```

---

## 2. CLOVA API 키 넣는 자리 (단 한 곳)

> **키는 GitHub에 올리지 않습니다.** `.env`는 `.gitignore`에 있어 추적되지 않고,
> 저장소에는 값이 빈 `.env.example`만 있습니다. 키는 **서버에서 직접** 넣습니다.

`.env` 파일의 `CLOVA_API_KEY` **한 곳만** 채우면 됩니다.

```
CLOVA_API_KEY=<여기에 발급받은 키>
CLOVA_ENDPOINT=https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005
LLM_MODE=auto
```

엔드포인트는 v3가 기본값입니다. v1(`/testapp/v1/...`)을 쓰면 요청 파라미터
이름이 다른데, **코드가 URL을 보고 자동으로 맞춥니다** — 경로만 바꾸면 됩니다.

### 키가 두 종류입니다 — `nv-`로 시작하지 않으면 엔드포인트도 바꿔야 합니다

CLOVA Studio 키는 발급 시기/방식에 따라 형식이 다릅니다.

| 키 형식 | 인증 방식 | 쓸 수 있는 엔드포인트 |
|---|---|---|
| `nv-`로 시작 | `Authorization: Bearer` | v3 (`/v3/chat-completions/HCX-005`) |
| 그 외 (구형 콘솔 키) | `X-NCP-CLOVASTUDIO-API-KEY` 헤더 | v1 — 콘솔에서 발급받은 **그 테스트앱/서비스앱의 실제 호출 URL** (`/testapp/v1/...` 또는 `/serviceapp/v1/...`) |

`app/llm/clova.py`가 키 접두사를 보고 인증 헤더를 자동으로 고릅니다 — 코드를
고칠 필요는 없습니다. 다만 **엔드포인트는 자동으로 못 바꿔줍니다.** v3는
Bearer(`nv-`) 키만 받기 때문에, 구형 키로 v3 URL을 그대로 두면 헤더를 아무리
맞춰도 401(`Invalid Key`)이 납니다 — 이 조합이면 클라이언트 생성 시점에
바로 에러 메시지로 알려줍니다. 이 경우 `CLOVA_ENDPOINT`를 콘솔에 표시된
해당 앱의 실제 요청 URL로 바꾸십시오. 콘솔에 `API Gateway Key`가 API Key와
별도로 함께 발급돼 있다면 `CLOVA_APIGW_KEY`에도 넣으십시오(신형 키에는 필요
없습니다).

배포(로컬·NCP 서버·Docker) 전체 절차는 **[docs/DEPLOY.md](docs/DEPLOY.md)** 참고.

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
L3  Exploration    app/retrieval/hybrid.py + rerank.py (BM25+벡터 RRF)     LLM 없음
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

### 임베딩 (주최측 확인 결과 사용 허용)

**CLOVA Studio 임베딩(bge-m3)만** 사용합니다. 네이버 클라우드가 제공하는
모델이라 "LLM은 HyperCLOVA X만" 제약 안에 확실히 들어옵니다. 외부 오픈소스
임베딩은 허용 여부가 다시 불확실해지므로 도입하지 않았습니다.

```bash
python -m app.ingest.build_index        # ① 인덱스
python -m app.ingest.build_embeddings   # ② 청크 벡터 (20~40분, 중단해도 이어서)
```

임베딩 API가 **텍스트 1건당 1회 호출**이라 인제스트와 분리된 별도 단계입니다.
청크 본문 해시가 같으면 다시 만들지 않으므로, 문서를 일부만 바꿔도 그 부분만
갱신됩니다. 벡터는 `data/index/vectors.bin`에 float32로 저장됩니다(numpy 불필요).

**벡터가 없으면 자동으로 BM25 단독으로 돌아갑니다** — 에러가 아니므로
급하면 임베딩 없이 먼저 띄워도 됩니다. 현재 상태는 `GET /health`의
`retrieval` 항목에서 확인하십시오.

### 검색 후처리

BM25/벡터 순위를 그대로 근거로 쓰지 않습니다(`app/retrieval/rerank.py`):

- **중복 제거** — 투자설명서 158건에 같은 조항이 반복돼, 안 하면 근거 8칸을
  한 문장이 독점합니다
- **문서 다양성** — 한 문서에서 최대 2청크 (후보가 모자라면 완화)
- **연혁·목차 강등** — 제거가 아니라 강등입니다. "언제 신설됐나요" 같은
  질의에서는 연혁이 정답이기 때문입니다
- **함정 유도 검색** — L2가 감지한 함정의 근거 문서를 별도로 훑어 슬롯을
  예약합니다. 함정 규칙은 자기 근거 문서를 알고 있고(`TrapRule.source`),
  L2가 L3보다 먼저 돌기 때문에 가능합니다

---

## 5. 문서 인제스트

제공 문서의 기본 형태는 **PDF가 아니라 페이지별 JPEG + OCR 텍스트가 담긴 zip**입니다.
일반 PDF 라이브러리로 열면 안 됩니다 — `zipfile`로 구조를 먼저 확인합니다.

### 실제 문서를 넣는 순서

```bash
mkdir -p data/corpus
cp <제공받은 파일들> data/corpus/          # 하위 디렉터리 그대로 넣어도 됩니다

python -m app.ingest.check_corpus          # ① 무엇이 읽히는지 먼저 확인 (필수)
python -m app.ingest.build_index           # ② 인덱스 생성
```

**`check_corpus`를 건너뛰지 마십시오.** 파일을 넣었다는 것과 그 내용이
검색된다는 것은 다릅니다. 스캔본 PDF처럼 텍스트 레이어가 없는 파일은
넣어도 0자로 읽히고, 그 상태로 평가를 받으면 근거 없이 거절만 하게 됩니다.

`data/corpus/`에 파일이 하나라도 있으면 **mock 코퍼스는 절대 사용되지 않습니다.**
판독 가능한 파일이 하나도 없으면 인덱스를 만들지 않고 실패합니다
(지어낸 문서로 답변하는 사고를 막기 위해서입니다).

### 형식별 지원

| 형식 | 지원 | 비고 |
|---|---|---|
| `.zip` (JPEG+OCR) | ✅ | 레이아웃 4종 자동 인식 |
| `.txt` `.md` `.csv` `.tsv` `.json` `.html` | ✅ | 표준 라이브러리만 사용 |
| `.xlsx` | ✅ | `openpyxl` 필요 (requirements에 포함) |
| `.pdf` | ✅ | `pypdf` 필요. **스캔본은 텍스트가 없어 OCR 결과가 별도로 필요** |
| `.hwp` `.docx` `.pptx` | ❌ | PDF/텍스트로 변환 후 넣으십시오 |

판독하지 못한 파일은 조용히 넘어가지 않고 빌드 로그와 `GET /health`의
`corpus.skipped_files`에 남습니다.

개발용 mock 코퍼스는 `python -m app.ingest.make_mock_corpus`로 만듭니다.

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
