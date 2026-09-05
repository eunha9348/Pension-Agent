# 연금 Agent

제10회 미래에셋증권 AI Festival · 연금 Agent 트랙 출품작.
자연어 연금 질의를 **제공 문서 근거로만** 조회·분석·설명하는 AI 에이전트.

> ✅ **현재 상태: 실물 코퍼스 인덱싱 완료 (2026-09-05)**
> 실제 코퍼스 **158문서(PDF 156 · xlsx 2) · 8,172청크**로 인덱스를 구성했고,
> CLOVA Studio 실연동(HCX-005)으로 L1·L5'·L6 호출이 성공하는 것을 배포
> 환경에서 확인했습니다. 검색은 BM25 + 벡터 RRF 하이브리드입니다.
> 회귀 테스트 1,257건 통과. 자세한 내용은 [PROGRESS.md](PROGRESS.md).

---

## 제출 정보

> ### ⬇️ 제출 전 이 줄을 채우세요
> **API End-point: `http://[[IP를 여기에 입력]]/answer`**
>
> 위 줄의 `[[IP를 여기에 입력]]`을 배포 서버의 실제 공인 IP로 바꾸고
> (예: `http://123.45.67.89/answer`) 커밋·푸시하십시오. 아래 표의
> 같은 자리도 함께 바뀝니다 — 문자열이 같으므로 검색/치환 한 번이면 됩니다.

| 항목 | 내용 |
|---|---|
| 제출 채널 | 주최 측 GitHub Organization 내 Private Repository Push |
| 제출물 | ① 소스코드 + Dockerfile/requirements.txt + 본 README &nbsp;·&nbsp; ② 기술제안서 &nbsp;·&nbsp; ③ 평가용 API End-point URL |
| **API End-point** | `http://[[IP를 여기에 입력]]/answer` ← **배포 후 실제 값으로 교체 필수** |
| End-point 제출 | **README 명시 + 구글폼 제출 둘 다 필수** — 연금 주제 폼: <https://forms.gle/JY33gvdFAncAvYCSA> |
| **제출 마감** | **09.06(일) 23:59** — 이후 결과물 변경 시 **실격**(코드 검증이 평가 과정에서 진행될 수 있음) |
| 서버 운영 기간 | **09.07(월) 10:00 ~ 09.11(금) 15:00** — 기간 중 API 상시 활성화 유지 |

- 경로 `/answer` 고정, 요청 헤더(인증 포함) 불필요
- 표준 포트: HTTP 80 / HTTPS 443(자체 서명 인증서 가능) — `docker compose up -d`만으로 80 포트 충족(`HOST_PORT`로 변경 가능)

### 평가 호출 규격 (주최측 공지)

| 항목 | 값 |
|---|---|
| 문항당 타임아웃 | **300초** (초과·5xx 시 최대 2회 재시도) |
| 동시성 | **순차 1건씩 · 동시 요청 없음** |
| 응답 필드 | 5개 전부 **문자열** · `application/json` |
| `retrieved_context` 구분 형식 | 자율 (평가 대상 아님) — 본 구현은 `\n---\n` 사용 |
| 주최측 발신 IP | **34.47.115.128** (평가 호출·헬스체크 동일) |

서버 아웃바운드 점검(평가와 무관, 자율):

```bash
curl -s http://34.47.115.128/health     # 우리 서버 안에서 실행
```

파이프라인 총 예산은 `PIPELINE_BUDGET_SEC`(기본 **240초** = 300초 - 여유 60초)로
조정합니다. 예산이 모자라면 LLM 단계가 생략되며, 그 사유와 남은 시간이
`think_trace`에 기록됩니다.

---

## API 명세

```
GET /answer?question_id={id}&question={질의}
```

```bash
curl -G "http://<end-point>/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=연금저축과 IRP 합쳐서 세액공제 얼마까지 되나요"
```

응답 (200 · `application/json` · 5필드 전부 문자열):

```json
{
  "question_id": "Q-001",
  "question": "평가 질의 원문",
  "retrieved_context": "답변 생성에 참고한 검색 문서",
  "think_trace": "사고·추론·도구 사용 과정",
  "answer": "최종 생성 답변"
}
```

내부에서 어떤 예외가 나도 이 스키마와 200 OK는 깨지지 않습니다.

---

## 빠른 시작

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                  # CLOVA_API_KEY 입력
python -m app.ingest.build_index      # 인덱스 생성
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker (권장)

```bash
cp .env.example .env
mkdir -p data/corpus && cp <제공 문서> data/corpus/

docker compose --profile tools run --rm smoke   # ① 키 검증 — 건너뛰지 말 것
docker compose --profile tools run --rm check   # ② 문서 판독 확인
docker compose up -d --build                    # ③ 구동 (컨테이너 8000 → 호스트 80)
```

문서는 볼륨 마운트라 교체해도 이미지 재빌드 불필요(`run --rm reindex` 후 재시작).
전체 배포 절차는 [docs/DEPLOY.md](docs/DEPLOY.md).

### 테스트

```bash
python -m pytest -q
```

---

## CLOVA API 키

`.env`의 `CLOVA_API_KEY` **한 곳만** 채우면 됩니다. 키는 저장소에 올라가지 않습니다
(`.env`는 `.gitignore`, 저장소엔 빈 `.env.example`만 존재).

| 키 형식 | 인증 방식 | 엔드포인트 |
|---|---|---|
| `nv-`로 시작 (신형) | `Authorization: Bearer` | v3 `.../v3/chat-completions/HCX-005` (기본값) |
| 그 외 (구형 콘솔 키) | `X-NCP-CLOVASTUDIO-API-KEY` | v1 — 콘솔에 표시된 테스트앱/서비스앱 실제 URL |

`app/llm/clova.py`가 키 접두사로 인증 헤더를 자동 판별합니다. 엔드포인트만 키
형식에 맞게 `.env`에서 바꾸면 됩니다(구형 키로 v3 URL을 두면 기동 시 즉시 에러).

키를 넣은 뒤 가장 먼저 실행:

```bash
python -m app.llm.smoke_test
```

mock으로 동작 중이면 응답 `think_trace`와 서버 로그에 `[MOCK LLM]` 배너가 찍힙니다.
`LLM_MODE=auto`(기본)는 키가 없으면 조용히 mock으로 전환 — 운영 배포(`docker-compose.yml`)는
`LLM_MODE=real`로 고정해 이 경로를 막아 둡니다.

---

## 아키텍처

정상 경로 LLM 호출은 **L1 · 생성(L5'/L4-sub) · L6 세 곳뿐**. 나머지는 결정론적 코드입니다.

| 계층 | 역할 | LLM | 구현 |
|---|---|---|---|
| L0 | 사전 분류 — 영역·용어 수집, 거절 권한 없음 | – | `core/grounding_retrieval.py` |
| 안전 게이트 | 빈 질의·개인정보조회·프롬프트 인젝션만 차단 | – | `analysis/refusal.py` |
| L1 | 질의 분석 | HyperCLOVA X | `analysis/query_spec.py` |
| 경로 분류 | GENERAL(계산) / ADVISORY(상담) 결정 | – | `analysis/routing.py` |
| 1.5 | 계획 감사 — 화이트리스트 검증 | – | `core/supervisory_board.py` |
| L2 | 함정 감지 (28종) | – | `core/trap_rules.py` |
| L3 ∥ L4 | 하이브리드 검색 ∥ 가입자격 판정 (ThreadPoolExecutor 병렬) | – | `retrieval/hybrid.py` |
| 합류 barrier | 자격 미달 후보만 제외, 미상은 통과 | – | `pipeline._eligibility_barrier()` |
| L5 | 계산 (15종, CALC_REGISTRY) | – | `core/pension_calc_functions.py` |
| L5' / L4-sub | 조건부 설명 생성 / 상담 답변 | HyperCLOVA X | `generation/answer_prompt.py`, `advisory.py` |
| L6 | 감독 이사회 — 결정론 5대 감사 + 의미 감사 + 답변–조문 저촉 판정 | HyperCLOVA X | `core/supervisory_board.py` |
| Sub-Agent | 전 구간 이상 감지, 트리거 시에만 구제 재작성 | 이상 시만 | `core/sub_agent.py` |

L5'와 L4-sub는 배타적이라 경로가 늘어도 호출 횟수는 늘지 않습니다. L6 권한 계층
(LLM은 심각도만 올릴 수 있음, 단조성)은 `merge_supervision()`에서 리팩터링 시에도
반드시 보존합니다. 전체 설계 원칙은 [CLAUDE.md](CLAUDE.md) 참고.

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

## 검색 백엔드

기본은 **파일 인덱스 + 순수 파이썬 BM25**(외부 DB 불필요). 재빌드는
`python -m app.ingest.build_index`.

임베딩(타사 모델 허용 · 답변 생성엔 미사용이라 제약 밖)을 켜면 BM25+벡터 RRF로 융합됩니다:

```bash
python -m app.ingest.build_embeddings
```

- 로컬 백엔드(기본): `requirements-embedding.txt` 필요. Docker는
  `WITH_EMBEDDING=true docker compose build agent embed`로 이미지에 포함
- 벡터가 없으면 자동으로 BM25 단독 축퇴(에러 아님) — `GET /health`의 `retrieval`에서 확인
- PostgreSQL/pgvector 전환 시 `sql/schema.sql` + `.env`의 `DATABASE_URL`

---

## 문서 인제스트

제공 문서는 **PDF(스캔본, OCR 텍스트 레이어 포함) 156건 + xlsx 2건**입니다.
`zip_parser.py`는 "페이지별 JPEG + OCR 텍스트 zip" 레이아웃도 함께 지원하지만,
이는 실물을 받기 전에 대비해 둔 형식이고 실제로 쓰이는 것은 PDF입니다.

```bash
mkdir -p data/corpus && cp <제공 문서> data/corpus/
python -m app.ingest.check_corpus          # ① 판독 확인 (필수 — 건너뛰지 말 것)
python -m app.ingest.build_index           # ② 인덱스 생성
python -m app.ingest.build_embeddings      # ③ 청크 벡터 (②를 다시 돌렸으면 ③도 다시)
```

`data/corpus/`에 파일이 있으면 mock 코퍼스는 쓰이지 않습니다. 판독 가능한 파일이
하나도 없으면 인덱스를 만들지 않고 실패합니다(지어낸 문서로 답변하는 사고 방지).

| 형식 | 지원 | 비고 |
|---|---|---|
| `.pdf` | ✅ | **제공 문서 156건이 이 형식.** `pypdf` 필요, 텍스트 레이어(OCR 결과) 필수 — 텍스트 레이어가 없는 순수 스캔 이미지는 판독 불가 |
| `.xlsx` | ✅ | `openpyxl` 필요. 제공 문서 2건 |
| `.zip`(JPEG+OCR) | ✅ | 페이지별 JPEG+텍스트 레이아웃 4종 자동 인식 — 대비용, 이번 제공 문서에는 없음 |
| `.txt/.md/.csv/.tsv/.json/.html` | ✅ | 표준 라이브러리만 사용 |
| `.hwp/.docx/.pptx` | ❌ | 변환 후 투입 |

판독 실패 파일은 `GET /health`의 `corpus.skipped_files`에 남습니다.

### OCR 판독 실패 복원

제공 OCR 텍스트의 판독 실패 구간은 `?????????`이거나 **같은 한글 음절이 반복**됩니다
(`퇴퇴 퇴 퇴퇴퇴퇴`). 재OCR 대신 **코퍼스 자체의 중복**으로 복원합니다 —
투자설명서들이 같은 표를 거의 그대로 싣기 때문에, 깨진 자리의 앞뒤 문맥을
앵커로 다른 문서에서 같은 구절을 찾아 메웁니다(`ingest/ocr_repair.py`, 지어내지
않고 코퍼스 안의 실재 텍스트만 사용). 복원 못 한 구간은 `(판독불가)`로 격리합니다.

`GET /health`의 `ocr_repair`에서 확인: `runs_found`(판독 실패 구간) ·
`runs_repaired`(교차 문서 복원) · `runs_masked`(격리). 진단은
`python -m scripts.corpus_health`(문턱 미달로 놓친 구간까지 보고).

⚠️ 인덱스는 호스트 볼륨에 남아 재사용되므로, 복원을 반영하려면 재인덱싱이
필요합니다(`FORCE_REINDEX=true` 또는 `docker compose run --rm reindex`).

개발용 mock 코퍼스는 `python -m app.ingest.make_mock_corpus`로 만듭니다.

---

## 디렉터리

```
app/
  config.py       환경 설정 (.env 로더)
  core/           계산·함정·검증·감독·인용·L0·Sub-Agent (8개 모듈)
  llm/clova.py    CLOVA Studio 클라이언트 + mock 폴백
  ingest/         zip 파서 · 청킹 · 메타데이터 · 인덱스 빌드
  retrieval/      BM25 · L0 개략검색 · L3 하이브리드 검색
  analysis/       L1 질의분석 · 라우팅 · 슬롯 매칭 · 계산 인자 조립
  generation/     L5'/L4-sub 답변 생성 · 근거 검증 래퍼
  law/            법령 수집 · 앵커 · 인용 검증
  pipeline.py     L0~L6 통합
  main.py         GET /answer · GET /health
sql/schema.sql    PostgreSQL 스키마 (선택)
tests/            회귀 테스트 1,257건 + 자체 평가셋 42문항
```

## 의존성

- 런타임(`requirements.txt`): fastapi, uvicorn, pydantic, httpx — 검증·검색 계층은
  외부 모델 의존 없음(대회 제약)
- 선택: `pypdf`/`openpyxl`(문서 판독), `psycopg`/`pgvector`(PostgreSQL 백엔드),
  `pytest`(테스트)
- 로컬 임베딩(선택, `requirements-embedding.txt`): `sentence-transformers`, `torch`(CPU 휠)
