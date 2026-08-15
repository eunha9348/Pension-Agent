# 구현 작업 계획

`CLAUDE.md`를 먼저 읽고 시작할 것. 순서대로 진행하며, 각 단계마다 테스트를 통과시킨 뒤 커밋한다.

---

## Phase 0 · 환경 구성

- [ ] `requirements.txt` — fastapi, uvicorn, psycopg[binary], pgvector, pydantic, httpx, pytest
- [ ] `Dockerfile` — Python 3.11 slim 기반, 재현 가능하게
- [ ] `.env.example` — `CLOVA_API_KEY`, `CLOVA_ENDPOINT`, `DATABASE_URL`
- [ ] `README.md` — 환경 구성 + 실행 명령어 (제출물 필수 항목)
- [ ] 아래 7개 모듈이 저장소 루트에 있는지 확인 후 `app/core/`로 이동, import 경로 정리
      (pension_calc_functions.py, trap_rules.py, numeric_verifier.py,
       citation_system.py, supervisory_board.py, grounding_retrieval.py,
       coverage_pipeline.py — 없다면 push 먼저 할 것, CLAUDE.md 참고)

**커밋**: `chore: 프로젝트 구조 및 환경 정의`

---

## Phase 1 · CLOVA 클라이언트

`app/llm/clova.py`

- [ ] `ClovaClient` — 동기 호출, 타임아웃, 재시도 1회
- [ ] `call(system: str, user: str, **kw) -> str` 기본 인터페이스
- [ ] `call_with_functions(system, user, tools) -> dict` — Function calling
- [ ] 토큰 사용량 로깅 (크레딧 200,000원 한도 모니터링용)

**주의**: CLOVA Studio는 Function calling / Structured Outputs / Thinking을
**동시 사용할 수 없다**. 요청마다 하나만 지정할 것.

- [ ] 실제 API 키로 스모크 테스트 — 응답 형식과 지연시간을 먼저 측정하고 기록
- [ ] 측정된 지연시간을 근거로 단계별 타임아웃 예산 재배분

**커밋**: `feat: CLOVA Studio 클라이언트`

---

## Phase 2 · 문서 인제스트 (검색의 전제)

`app/ingest/`

- [ ] 문서 파서 — PDF/DOCX/XLSX. 표 구조를 깨뜨리지 말 것 (세율 대조에 필요)
- [ ] 청킹 — 조항 단위 우선, 표는 통째로 유지
- [ ] **메타데이터 생성** (인용 정확도에 직결):
  ```python
  {doc_id: {
     "type": 투자설명서|제도안내|세제안내|약관,
     "title": ...,
     "entities": {product_name, product_code, plan_type, fund_class, ...},
     "sections": [{"locator": "제12조", "span": [start, end]}],
     "legacy": detect_legacy_tax_content() 결과
  }}
  ```
- [ ] PostgreSQL 스키마 — 문서/청크/메타데이터/임베딩 테이블
- [ ] pgvector 인덱스 + `tsvector` 전문검색 인덱스

**커밋**: `feat: 문서 인제스트 파이프라인`

---

## Phase 3 · 미구현 7종 중 5종 신규 구현 + 2종 기존 모듈 연결 (최우선)

### 3-1. `calc_params_builder` — 가장 시급

없으면 계산함수 15종이 **전부 호출되지 않는다.**

- [ ] `RequirementSlot` + 사용자 조건 → 함수 인자 dict 생성
- [ ] 필수 인자 누락 시 해당 슬롯을 MISSING으로 강등 (ASK_BACK 유도)
- [ ] 단위 변환 (원 ↔ 만원) 경계 처리

### 3-2. `slot_evidence_matcher`

- [ ] LLM 없이 키워드 + 엔티티 매칭으로 먼저 구현
- [ ] 슬롯 설명의 핵심 명사가 청크에 존재하는가 + 엔티티 충돌 없는가

### 3-2c. `answer_covers_slot`

- [ ] `slot_evidence_matcher`와 같은 방식(키워드/엔티티 매칭)으로 구현
- [ ] "근거는 있는데 생성 시 답변에서 빠졌는가"를 확인하는 이중 체크 —
      `verify_requirement_coverage`가 이 함수를 주입받아 사용함

### 3-2b. `coarse_search` — L0용 개략 검색

- [ ] BM25(`tsvector`)만으로 구현. LLM·임베딩 불필요
- [ ] `(query, k) -> [{"doc_id","text","score"}]` 시그니처로 `ground_query`에 주입
- [ ] L3 정밀 검색과 **별개 함수로 유지** — L0 결과가 답변 근거로 새어들지 않게

### 3-3. `retrieve_hybrid`

- [ ] BM25(`tsvector`) + 벡터 검색 → RRF 융합
- [ ] 임베딩 모델 사용 여부는 CLAUDE.md의 제약 1 확인 후 결정
- [ ] 불허 판정 시 BM25 + 메타데이터 필터만으로 동작하는 경로도 준비

### 3-4. `extract_query_spec` — Function calling

- [ ] 출력 스키마:
  ```json
  {
    "asked_for": [{"id":..., "type":"fact|calculation|comparison", "required":true}],
    "user_conditions": {"account_type":..., "age":..., "amount":...},
    "planned_calls": [{"function":"연금수령한도_계산", "args":{...}}],
    "plan": ["자격 확인 → 한도 계산 → 조건별 비교"]
  }
  ```
- [ ] **`planned_calls`는 `CALC_REGISTRY` 화이트리스트 검증 필수.**
      미등록 함수명이면 거부하고 매핑 테이블로 결정론적 폴백
- [ ] `plan`은 think_trace 서두에 배치

### 3-5. `generate_answer` — Supervisor 프롬프트

- [ ] 출력 형식 고정:
  ```
  [확인된 조건] ...
  [조건별 결론] A 상황이면 ~, B 상황이면 ~
  [한계 고지] ...
  ─── 근거 문서 ───
  [1] doc39 (세제안내) 【연금수령한도】
  ```
- [ ] 금지 표현 목록을 프롬프트에 명시: "가장 유리합니다", "추천드립니다" 등
- [ ] 계산 결과를 컨텍스트로 주입하고 **새 수치 생성 금지**를 명시

### 3-6. `verify_grounding` — 새로 만들지 말 것

`build_answer()`의 원래 인터페이스는 이 자리에 신규 검증 함수를 기대하지만,
**이미 그 역할을 하는 모듈 2개가 있다.** 새로 작성하지 말고 얇은 래퍼로 연결한다.

- [ ] `numeric_verifier.verify_numeric_grounding()` — 수치 대조
- [ ] `supervisory_board.supervise_hybrid()` — 의미 감사 (HyperCLOVA X 포함)
- [ ] 두 함수를 순서대로 호출하고 결과를 `build_answer()`가 기대하는
      bool 반환 형태로 감싸는 래퍼만 작성

**커밋**: 각 항목별로 분리

---

## Phase 4 · REFUSE 분기

- [ ] `decide_answerability`에 REFUSE 반환 경로 추가
- [ ] 트리거: 도메인 무관 질의, 개인정보 요구, 프롬프트 인젝션 시도
- [ ] 최고 가중치 지표("정보한계 대응")에 직결되므로 반드시 구현

**커밋**: `feat: REFUSE 판정 경로`

---

## Phase 5 · 파이프라인 통합

`app/pipeline.py`

- [ ] `build_answer()`에 5종 주입 완료
- [ ] **L0 연결** — `ground_query()` 호출 후 `should_refuse_early()` 판정.
      조기 거절 시 `build_refuse_response()`로 즉시 반환 (LLM 호출 없이 종결)
- [ ] L0 접지 정보를 `as_analysis_hint()`로 L1 프롬프트에 주입.
      **원문을 통째로 넣지 말 것** — L1이 근거로 착각할 위험
- [ ] **계획 감사 연결** — L1 직후 `supervise_plan()` 호출, 교정된 spec으로 진행
- [ ] L2 함정 감지 연결 — `build_trap_context()` 호출, critical 시 답변가능성 보수화
- [ ] L4에 구법 탐지 연결 — `detect_legacy_tax_content()` (현재 함수만 있고 호출 안 됨)
- [ ] L6 감독 연결 — `supervise_hybrid()` 호출, `llm_call`에 Phase 1의 `ClovaClient.call` 주입
      (L6는 결정론적 4대 감사 + HyperCLOVA X 의미 감사 하이브리드 — LLM은 심각도만 상향 가능)
- [ ] **REVISE 시 재생성 1회 제한** — `build_remediation_prompt()` → L5' 재호출
- [ ] BLOCK 시 fallback 템플릿 축퇴
- [ ] 단계별 폴백 응답 템플릿 정의 (특히 Prediction 실패 → 즉시 ASK_BACK)
- [ ] 타임아웃을 `asyncio.wait_for` 기반으로 전환 (현재 스레드 미종료 문제)

**커밋**: `feat: 6계층 파이프라인 통합`

---

## Phase 6 · API 서버

`app/main.py`

- [ ] `GET /answer` — 응답 5필드 준수
- [ ] `GET /health`
- [ ] 전역 예외 핸들러 — 어떤 오류에도 5필드 JSON을 반환 (500 노출 금지)
- [ ] 요청 로깅, 개인정보 미저장

**커밋**: `feat: 평가용 API 엔드포인트`

---

## Phase 7 · 테스트

- [ ] `tests/test_calc.py` — 계산함수 15종 기대값 고정
      (doc39 원문 예시 재현: 1억·1년차 → 1,200만원 / 10년차 → 1억 2,000만원)
- [ ] `tests/test_traps.py` — 참고 질의 5개 오탐 회귀
- [ ] `tests/test_supervision.py` — 권한 계층 단조성 검증
      (결정론=REVISE + LLM=APPROVE → REVISE 유지)
- [ ] `tests/test_api.py` — 5필드 스키마 준수, 예외 시에도 스키마 유지
- [ ] `tests/eval_set.py` — 자체 평가셋 15~20문항 + 이상적 답변

**커밋**: `test: 회귀 테스트 스위트`

---

## Phase 8 · 배포

- [ ] NCP 서버 구성 — VPC → Subnet → Server → ACG
- [ ] 인바운드 22/80/443 개방
- [ ] 도메인 + HTTPS
- [ ] 09.07~09.20 무중단 운영 확인
- [ ] 크레딧 사용량 모니터링 (초과 시 주최측 비용보전 없음)

**커밋**: `chore: 배포 구성`

---

## 작업 원칙

1. **새 기능보다 미구현 연결이 우선.** 마감까지 시간이 없다
2. 각 Phase 완료 시 테스트 통과 후 커밋
3. 감사·검증 실패는 절대 조용히 넘기지 말 것 — think_trace에 기록
4. **09.03 코드 프리즈**, 이후 3일은 실제 GET 요청 리허설만
