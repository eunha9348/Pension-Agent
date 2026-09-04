# 연금 Agent

제10회 미래에셋증권 AI Festival · 연금 Agent 트랙 출품작.
자연어 연금 질의에 대해 **제공 문서 근거로만** 조회·분석·설명하는 AI 에이전트.

> ✅ **현재 상태: 실물 코퍼스 인덱싱 완료 (2026-09-04)**
> 실제 코퍼스 **158문서 · 8,173청크**로 인덱스를 구성했고, CLOVA Studio
> 실연동(HCX-005)으로 L1·L5'·L6 호출이 성공하는 것을 배포 환경에서
> 확인했습니다. 검색은 BM25 + 벡터 RRF 하이브리드입니다.
> 회귀 테스트 1,186건 통과. 자세한 내용은 [PROGRESS.md](PROGRESS.md).

---

## 0. 평가용 API End-point (필수 제출 정보)

> ⚠️ **09.07~09.20 운영 기간 시작 전, 서버를 배포한 뒤 아래 주소를 실제
> 값으로 반드시 교체할 것.** 주최측 공지("평가용 API End-point 제출 관련")에
> 따라 이 값이 곧 제출물이다 — 비워 두거나 localhost로 남겨 두면 평가
> 담당자가 접근할 수 없다.

```
GET http://<배포 서버의 공인 IP 또는 도메인>/answer?question_id={id}&question={질의}
```

- 경로는 `/answer` 고정 (변경 금지)
- 요청 헤더 없음(인증 헤더 포함 일체 불필요) — 코드도 헤더를 요구하지 않는다
- 기본 포트: HTTP 80 (`docker-compose.yml`의 `HOST_PORT` 기본값이 80으로
  맞춰져 있다 — `docker compose up -d`만으로 표준 포트 요건을 충족한다)
- HTTPS로 구성 시 443 + 자체 서명 인증서 가능(주최측 안내대로 허용됨).
  이 경우 위 주소를 `https://...`로 바꿔 적을 것
- 응답 지연: 파이프라인 예산 상한 `PIPELINE_BUDGET_SEC`(기본 55초)이
  주최측 타임아웃(300초)에 여유 있게 들어온다
- 주최측 발신 IP 대역이 공지되면, 필요 시 방화벽에서 해당 대역만
  허용하도록 구성 가능(선택 사항 — 코드 변경 불필요)

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
L0   사전 분류     core/grounding_retrieval.py            LLM 없음  (거절 권한 없음)
안전 게이트        analysis/refusal.py                    LLM 없음  (빈 질의·개인정보·인젝션)
L1   질의 분석     analysis/query_spec.py                 HyperCLOVA X
경로 분류          analysis/routing.py                    LLM 없음  GENERAL / ADVISORY
1.5  계획 감사     core/supervisory_board.supervise_plan() LLM 없음
L2   함정 감지     core/trap_rules.py (28종)              LLM 없음
L3 ∥ L4            retrieval/hybrid.py ∥ 가입자격 판정     LLM 없음  ThreadPoolExecutor
합류 barrier       pipeline._eligibility_barrier()        LLM 없음  자격 미달만 제외
L5   Prediction    core/pension_calc_functions.py (15종)  LLM 없음
L5'  Supervisor    generation/answer_prompt.py            HyperCLOVA X  [GENERAL]
L4-sub 상담 답변   generation/advisory.py                 HyperCLOVA X  [ADVISORY]
L6   감독 이사회   core/supervisory_board.py              HyperCLOVA X
                   결정론 5대 감사 + 의미 감사 + 답변–조문 저촉 판정
Sub-Agent          core/sub_agent.py                      이상 시에만 호출
```

정상 경로의 LLM 호출은 **L1 · (L5' 또는 L4-sub) · L6 세 곳뿐**입니다.
L5'와 L4-sub는 배타적이라 경로가 늘어도 호출 횟수는 늘지 않습니다.
나머지는 전부 결정론적 코드입니다.

원칙은 [CLAUDE.md](CLAUDE.md)에 있으며, 구현 시 위반 금지입니다.
특히 **L6의 권한 계층**(LLM은 심각도를 올릴 수만 있음)은 리팩터링 시에도
`merge_supervision()`에서 반드시 보존해야 합니다.

### 상품 팩트 6축

과제가 정의한 실적배당형 상품 데이터의 축은 여섯입니다. 두 곳에서 나눠
뽑되 답변에는 한 벌로 실립니다.

| 축 | 추출 위치 | 시점 |
|---|---|---|
| 판매클래스 · 총보수 | `analysis/products.py` | 검색 후 (근거 청크) |
| 상품분류 · 위험등급 · 수익률 · 시장잔고 | `analysis/product_facts.py` | **색인 시점 (문서 전문)** |

색인 시점에 뽑는 이유는 **검색이 표 청크를 놓쳐도 사실이 사라지지 않게**
하기 위해서입니다. 값마다 원문 스니펫을 함께 보관해 인용과 수치 검증에
그대로 씁니다. 진단은 `python -m scripts.corpus_facts`.

⚠️ **위험등급은 1등급이 가장 위험합니다.** 숫자만 쓰면 정반대 서술이
나오므로 원문 표기(`4등급(보통 위험)`)를 함께 싣습니다.

### 법령 계층 — 외부 자료는 보조입니다

법제처 OPEN API로 조문을 수집해 **내부 검증에만** 씁니다
(`retrieved_context`에 넣지 않습니다). 두 가지를 판정합니다.

| 갈래 | 묻는 것 | 반영 |
|---|---|---|
| 함정 판정 | 이 **함정**이 이 질의에 적용되는가 | `trap_ids` 조정 후 감사 재실행 |
| 저촉 판정 | 이 **답변**이 조문에 어긋나는가 | 검증 통과분을 REVISE로 상향만 |

⚠️ **제공 문서가 최종 근거이고 법령은 보조입니다**(과제 안내). 저촉 판정은
세 겹으로 막습니다 — 조문 인용 verbatim · 답변 문장 verbatim ·
**제공 문서가 그 문장을 뒷받침하지 않을 것**. 즉 저촉으로 채택되는 경우는
**제공 문서와도 맞지 않고 법령과도 맞지 않을 때뿐**입니다.

가동 여부는 `/health`의 `law.conflict_check_active`로 확인합니다.

---

## 4. 검색 백엔드

기본은 **파일 기반 인덱스 + 순수 파이썬 BM25**입니다. 외부 DB가 필요 없습니다.

- 인덱스 위치: `data/index/`
- 재빌드: `python -m app.ingest.build_index`

PostgreSQL(pgvector/tsvector)로 전환하려면 `sql/schema.sql`로 초기화하고
`.env`의 `DATABASE_URL`을 채우십시오. 검색 인터페이스가 동일하므로
`app/retrieval/hybrid.py`의 백엔드만 교체하면 됩니다.

### 임베딩 (타사 모델 사용 허용 · 2026-08-19 확인)

임베딩은 **검색 순위용 벡터**일 뿐 답변 문장을 만들지 않으므로 대회 제약
밖입니다. ⚠️ 답변을 만드는 **L1·L5'·L6 세 호출만은 HyperCLOVA X 전용**입니다.

```bash
python -m app.ingest.build_index        # ① 인덱스
python -m app.ingest.build_embeddings   # ② 청크 벡터
```

백엔드는 두 가지입니다(`EMBEDDING_BACKEND`):

| | 소요 | 제약 |
|---|---|---|
| **local** (기본) | 8천 청크 **몇 분** | 라이브러리 설치 필요(`requirements-embedding.txt`), 모델 ~470MB |
| clova | **2시간+** | 건당 1회 호출 + 속도 제한(429)으로 자주 끊김 |

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
python -m app.ingest.build_embeddings      # ③ 청크 벡터 (인덱스를 만든 뒤)
```

⚠️ **②를 다시 돌렸으면 ③도 다시 돌리십시오.** 재인덱싱은 청크 본문을
바꾸는데(OCR 복원·격리), 벡터는 예전 본문으로 만들어진 채 남습니다.
`build_embeddings`가 본문 해시를 대조해 **바뀐 청크만** 다시 임베딩하고
사라진 청크의 벡터를 정리합니다. `/health`의 `retrieval.vectors`와
`corpus.chunks`가 어긋나 있으면 ③이 밀린 상태입니다.

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

### OCR 판독 실패와 복원

제공 OCR 텍스트에는 판독 실패 구간이 있습니다. 형태는 두 가지입니다 —
`?????????` 처럼 물음표로 채워지거나, **같은 한글 음절이 반복**됩니다
(`퇴퇴 퇴 퇴퇴퇴퇴`). 우리 디코더의 대체문자는 `�`이지 `?`가 아니므로
**판독기를 바꿔서 고칠 수 있는 결함이 아닙니다.**

복원 수단은 재OCR이 아니라 **코퍼스의 중복**입니다. 투자설명서들이 같은
표를 거의 그대로 싣기 때문에, 깨진 자리의 앞뒤 12자를 앵커로 삼아 다른
문서에서 같은 구절을 찾아 메웁니다(`ingest/ocr_repair.py`). 지어내는 것이
아니라 코퍼스 안에 실재하는 텍스트를 가져오므로 결정론적입니다.

복원하지 못한 구간은 **`(판독불가)`로 격리**합니다. 깨진 글자가 답변
근거로 새어 나가는 것보다 "여기는 못 읽었다"가 정직하기 때문입니다.

결과는 `/health`의 `ocr_repair`에서 봅니다.

```
runs_found     판독 실패 구간 수
runs_repaired  교차 문서로 복원한 수
runs_masked    복원 실패 → (판독불가)로 격리한 수
  후보충돌     여러 문서가 서로 다른 값을 줌 → 복원하지 않음 (억지로 고르면 날조)
  대조실패     코퍼스 어디에도 같은 앵커가 없음
  앵커부족     앞뒤 문맥이 8자 미만이라 대조 불가
```

진단 도구는 `python -m scripts.corpus_health` 입니다 — 복원한 것뿐 아니라
**문턱에 미달해 놓친 구간(near-miss)까지** 보고하므로 문턱을 근거 있게
조정할 수 있습니다.

⚠️ 인덱스는 호스트 볼륨에 남고 entrypoint가 재사용합니다. 이미지를 새로
빌드해도 **예전 인덱스에 박힌 판독 실패 문자는 사라지지 않습니다.**
복원까지 받으려면 재인덱싱이 필요합니다(`FORCE_REINDEX=true` 또는
`docker compose run --rm reindex`).

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
