# 실행 · 배포 가이드

API 키를 어디에 넣고, 어떻게 띄우는가. 로컬 → NCP 서버 순서로 정리했습니다.

---

## 0. ⚠️ 먼저 — API 키는 GitHub에 올리지 않습니다

**키를 넣는 파일은 `.env` 단 하나이고, 이 파일은 저장소에 올라가지 않습니다.**

- `.env` 는 `.gitignore` 에 등록돼 있습니다 (추적되지 않음을 확인함)
- 저장소에 있는 건 `.env.example` — **값이 비어 있는 서식 파일**입니다
- GitHub에 올려야 하는 건 코드뿐이고, 키는 **서버에서 직접** 입력합니다

키가 실수로 올라가면 즉시 폐기하고 재발급해야 합니다. 확인 방법:

```bash
git ls-files | grep -x ".env"      # 아무것도 안 나와야 정상
git log --all -p -- .env | head    # 과거 커밋에도 없어야 정상
```

---

## 1. CLOVA Studio에서 키 발급

CLOVA Studio 콘솔에서 API 키를 발급합니다. 키는 두 종류입니다.

| 종류 | 발급 위치 | 용도 |
|---|---|---|
| **테스트 API 키** | 테스트 탭 | 개발·실험용. 서비스 앱을 제외한 API 호출 |
| **서비스 API 키** | 서비스 탭 | 서비스 앱 포함 **모든** API 호출 |

각각 계정당 최대 10개까지 생성됩니다.

> **발급 팝업을 닫으면 키를 다시 볼 수 없습니다.** 발급 즉시 안전한 곳에 보관하십시오.

**대회 운영 관점**: 09.07~09.20 무중단 평가라면 호출량·안정성 조건을 확인한 뒤
서비스 앱/서비스 키로 가는 것이 안전합니다. 테스트 앱은 실험용이라 제약이 있을 수
있으니, 콘솔에서 본인 계정의 한도를 직접 확인하십시오.

### ⚠️ 키 형식이 두 가지입니다 — 발급 시기에 따라 다릅니다

| 형식 | 인증 방식 | 짝이 되는 엔드포인트 |
|---|---|---|
| `nv-`로 시작 (신형) | `Authorization: Bearer nv-...` | v3 — `/v3/chat-completions/{model}` |
| 그 외 (예: `ncp`류 접두, 구형 콘솔 키) | `X-NCP-CLOVASTUDIO-API-KEY: ...` 전용 헤더 | v1 — 그 테스트앱/서비스앱을 **만들 때 콘솔에 표시된 실제 요청 URL** (`/testapp/v1/...` 또는 `/serviceapp/v1/...`) |

두 형식은 섞어 쓸 수 없습니다. `nv-`가 아닌 키로 v3 URL을 호출하면
`Invalid Key - Please use new API Key that starts with 'nv-*'`(HTTP 401)가
납니다 — "키가 틀렸다"는 뜻이 아니라 "이 키는 이 엔드포인트 형식이 아니다"는
뜻입니다. 어느 쪽인지는 아래 2절에서 코드가 자동으로 헤더를 골라 주지만,
**엔드포인트 URL만은 직접 맞는 것으로 넣어야** 합니다.

---

## 2. 키를 넣는 자리 (파일 1개, 줄 1개)

```bash
cp .env.example .env
```

`.env` 파일을 열어 채웁니다. **키가 `nv-`로 시작하면** 그대로 v3 엔드포인트를
쓰면 됩니다:

```bash
CLOVA_API_KEY=nv-xxxxxxxxxxxxxxxxxxxxxxxxxxxx     # ← ★ 여기 ★
CLOVA_APIGW_KEY=                                   # nv- 키는 비워둠
CLOVA_REQUEST_ID=                                  # 선택 (콘솔에서 발급)
CLOVA_ENDPOINT=https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005
LLM_MODE=auto
```

**키가 `nv-`로 시작하지 않으면**(구형 콘솔 키) 엔드포인트를 콘솔에 표시된
그 앱의 실제 URL로 바꿔야 합니다. 콘솔에 API Key와 별도로 "API Gateway Key"가
함께 발급돼 있으면 `CLOVA_APIGW_KEY`에도 넣으십시오:

```bash
CLOVA_API_KEY=ncpXXXXXXXXXXXXXXXXXXXXXXXX          # ← ★ 여기 ★
CLOVA_APIGW_KEY=                                    # 콘솔에 별도 키가 있으면 여기
CLOVA_REQUEST_ID=
CLOVA_ENDPOINT=https://clovastudio.stream.ntruss.com/testapp/v1/chat-completions/<앱ID>
LLM_MODE=auto
```

이게 전부입니다. 코드 어디도 고칠 필요가 없습니다 — 인증 헤더는 키 접두사를
보고 자동으로 골라집니다. (구형 키로 v3 URL을 그대로 두면 `python -m
app.llm.smoke_test` 실행 시 클라이언트 생성 단계에서 바로 에러 메시지로
알려줍니다 — 401을 받고 나서야 원인을 찾지 않도록 한 것입니다.)

- `LLM_MODE=auto` — 키가 있으면 실호출, 없으면 자동으로 mock
- 키를 읽는 곳은 `app/config.py`의 `get_settings()` 한 곳이고,
  헤더를 자동 판별해 싣는 곳은 `app/llm/clova.py`의 `ClovaClient._headers()`
  한 곳입니다

### 엔드포인트 버전

| 경로 | 파라미터 이름 |
|---|---|
| `/v3/chat-completions/{model}` (기본값) | `repetitionPenalty`, `stop` |
| `/testapp/v1/chat-completions/{model}` | `repeatPenalty`, `stopBefore`, `includeAiFilters` |

**코드가 URL에 `/v3/`가 있는지 보고 자동으로 맞춥니다.** 경로만 바꾸면 됩니다.
모델을 바꾸려면 끝의 모델명만 교체하십시오 (`HCX-005` → `HCX-DASH-002` 등).

---

## 3. 키를 넣은 직후 — 스모크 테스트 (필수)

```bash
python -m app.llm.smoke_test
```

이 스크립트가 확인하는 것:

1. **인증이 되는가** — 헤더 형식이 맞는지
2. **응답 구조가 파서와 맞는가** — 원문을 그대로 출력
3. **지연시간** — 3회 호출해 중앙값·최댓값 측정
4. **Function calling 응답 형태** — `toolCalls` 파싱 여부

실패 시 확인 순서:

| 증상 | 확인할 것 |
|---|---|
| 401 / 403 | 키 형식(`nv-` vs 구형)과 엔드포인트가 맞는 조합인지 (위 "키 형식이 두 가지" 참고). 구형 키+v3 조합은 클라이언트 생성 시점에 이미 걸러지므로, 여기까지 왔다면 엔드포인트의 앱ID나 CLOVA_APIGW_KEY를 다시 확인 |
| 404 | `CLOVA_ENDPOINT`의 버전 경로와 모델명 |
| toolCalls 파싱 실패 | 응답 원문을 보고 `call_with_functions()` 파싱부를 실제 스키마에 맞출 것 |
| 타임아웃 | `.env`의 `CLOVA_TIMEOUT_SEC` 상향 |

### 3-B. 청크 임베딩 만들기 (검색 품질)

임베딩은 **타사 모델을 써도 됩니다**(2026-08-19 확인). 검색 순위용 벡터일 뿐
답변 문장을 만들지 않기 때문입니다.
⚠️ 답변을 만드는 **L1·L5'·L6 세 호출만은 HyperCLOVA X 전용**입니다 (실격 사유).

**권장: 로컬 모델 (기본값)**

CLOVA 임베딩 API는 건당 1회 호출 + 속도 제한(429) 때문에 8천 청크에 2시간이
넘게 걸리고 자주 끊깁니다. 로컬 모델은 배치로 **몇 분**이면 끝나고 호출
제한도 API 키도 없습니다.

```bash
# ① 이미지에 임베딩 라이브러리를 포함해 빌드 (한 번만)
WITH_EMBEDDING=true docker compose build agent embed

# ② 벡터 생성 — 몇 분
docker compose --profile tools run --rm embed

# ③ 서버 재시작
docker compose up -d
```

- 모델은 첫 실행 때 자동으로 내려받아 `data/models/`에 캐시됩니다(~470MB).
  컨테이너를 지워도 다시 받지 않습니다
- 디스크가 넉넉하면 `.env`의 `LOCAL_EMBEDDING_MODEL=BAAI/bge-m3`로 품질을
  더 올릴 수 있습니다(~2.2GB)
- **인덱스를 만든 뒤에** 실행합니다 (`build_index` → `build_embeddings` 순서)
- 문서를 바꿔도 **내용이 그대로인 청크는 다시 만들지 않습니다**(본문 해시 비교)
- 얼마나 걸릴지 먼저 보려면: `--dry-run` (API·모델 호출 없이 즉시 계산)

**CLOVA 임베딩을 굳이 쓰려면** `.env`에 `EMBEDDING_BACKEND=clova`를 두십시오.

- **중단해도 안전합니다.** 100건마다 저장하므로 Ctrl+C로 끊거나 네트워크가
  끊겨도, 다시 실행하면 남은 것부터 이어서 만듭니다
- **429(호출 속도 제한)가 나면** 자동으로 몇 초씩 쉬며 재시도하고, 요청 간격도
  스스로 늘립니다. 그래도 계속 걸리면 그 자리에서 멈추니 몇 분 뒤
  `--pace 2.0` 처럼 간격을 키워 다시 실행하십시오
- 오래 걸리므로 백그라운드로 돌리십시오 — 터미널을 닫아도 계속됩니다:

```bash
./scripts/embed-bg.sh 1.0      # 시작
./scripts/embed-bg.sh status   # 진행 상황
./scripts/embed-bg.sh stop     # 중단(여기까지 저장)
```

만든 뒤 `.env`에서 `USE_EMBEDDING=true`로 두고 재시작하면 BM25와 벡터 검색이
RRF로 합쳐집니다. **벡터가 없으면 자동으로 BM25 단독으로 돌아가므로**
(에러가 아닙니다) 급하면 임베딩 없이 먼저 띄워도 됩니다.

상태 확인은 `GET /health`의 `retrieval` 항목을 보십시오:

```json
"retrieval": { "embedding_enabled": true, "vectors": 8172,
               "mode": "BM25 + 벡터 RRF", "warning": "" }
```

`warning`이 비어 있지 않으면 임베딩이 켜져 있는데 실제로는 안 쓰이는
상태입니다 — 그 문구가 원인과 해결 방법을 알려 줍니다.

Docker를 쓴다면:

```bash
docker compose --profile tools run --rm embed
```

---

## 3-C. 평가셋으로 품질 확인

```bash
python -m tests.eval_set
```

42문항(함정 26종 + 실배포 오답 2건 + 거절·되묻기)을 돌려 통과율을 냅니다.
**실물 코퍼스에서 돌려야 의미가 있습니다** — mock에서는 일부 문항을
건너뜁니다. 개선 전후 통과율 비교가 발표 자료의 근거가 됩니다.

> `smoke_test`가 진단용으로 Function calling도 한 번 찔러 봅니다(`maxTokens`가
> 1024보다 커야 동작 — 코드에서 최소 1100 보장). ⚠️ **실제 답변 파이프라인은
> Function calling을 쓰지 않습니다** — HCX-005가 tools 페이로드를 간헐적으로
> 거부해(400·40009) L1은 평범한 텍스트 호출로 JSON을 받습니다(CLAUDE.md
> 참고). 여기서 toolCalls 파싱이 실패해도 정상 답변 경로에는 영향이 없습니다.

측정된 지연이 나오면 `app/pipeline.py`의 `BUDGET_*` 상수를 그 값으로 갱신하십시오
(현재 값은 실측 없이 잡은 추정치입니다).

---

## 4. 로컬 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # 키 입력
mkdir -p data/corpus          # 제공 문서를 여기에
python -m app.ingest.check_corpus     # 무엇이 읽히는지 확인
python -m app.ingest.build_index      # 인덱스 생성

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

한 번에 하려면:

```bash
./scripts/run.sh
```

확인:

```bash
curl "http://localhost:8000/health" | python3 -m json.tool
curl "http://localhost:8000/answer?question_id=Q-001&question=연금저축 세액공제 한도"
```

`/health`의 `llm.is_mock`이 `false`여야 실연동 상태입니다.

---

## 5. AWS EC2 + Docker 배포 (태블릿·공용PC 등 로컬 저장이 안 되는 환경)

**핵심 아이디어**: 로컬 기기(태블릿/사지방 PC)에는 아무것도 설치·저장하지 않습니다.
전부 **EC2 인스턴스 안에서** 진행하고, 접속은 브라우저 기반 **EC2 Instance
Connect**로 합니다 — SSH 클라이언트 설치가 필요 없어 태블릿에서도 됩니다.
로컬 기기를 로그아웃해도 EC2 인스턴스 자체는 계속 살아 있습니다.

### 5-A-1. EC2 인스턴스 생성 (AWS 콘솔, 브라우저)

- AMI: **Ubuntu 22.04/24.04 LTS** (또는 Amazon Linux 2023)
- 인스턴스 타입: `t3.small` 이상 권장 (인덱스 빌드 + uvicorn 구동)
- 보안 그룹(인바운드):

| 포트 | 용도 | 소스 |
|---|---|---|
| 22 | SSH (Instance Connect가 사용) | 가능하면 내 IP로 제한 |
| 80 | 평가 요청 수신 | 0.0.0.0/0 |

- **아웃바운드는 기본값(전체 허용) 그대로 두십시오.** CLOVA 도메인과 Docker Hub에
  나가야 합니다 — 이 개발 컨테이너에서 막혔던 제약은 EC2에는 없습니다.

### 5-A-2. 브라우저로 접속

콘솔 → EC2 → 인스턴스 선택 → **연결(Connect)** → **EC2 Instance Connect** 탭 →
연결. 새 탭에 터미널이 뜹니다. 이후 전부 이 터미널 안에서 진행합니다.

### 5-A-3. Docker 설치

```bash
sudo apt-get update
sudo apt-get install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker              # 그룹 반영 (또는 재접속)
```

### 5-A-4. 코드 받기 + 키 입력

```bash
git clone https://github.com/eunha9348/Pension-Agent.git
cd Pension-Agent
# 기본 브랜치가 이미 최신 작업 브랜치입니다(clone만으로 충분).
# 특정 브랜치를 명시하려면: git checkout claude/claude-implementation-plan-hpz25z

cp .env.example .env
nano .env          # CLOVA_API_KEY=발급받은키   ← 이 터미널은 EC2 안이라
                    #   태블릿을 로그아웃해도 지워지지 않습니다
```

### 5-A-5. 실제 문서 넣기

태블릿에는 파일을 저장 못 하므로, 문서를 EC2로 직접 들여오는 방법이 필요합니다.

- **다운로드 링크가 있는 경우**: EC2 터미널에서 바로 받습니다
  ```bash
  mkdir -p data/corpus
  wget -P data/corpus/ "<제공받은 다운로드 URL>"
  ```
- **파일만 있고 링크가 없는 경우**: AWS 콘솔의 **S3**에 브라우저로 업로드(드래그 앤
  드롭)한 뒤, EC2에서 받습니다
  ```bash
  aws s3 cp s3://<버킷>/<파일> data/corpus/ --recursive
  ```
  (S3 접근 권한이 필요하면 EC2에 IAM 역할을 붙이거나 `aws configure`로 자격 증명 입력)

넣은 뒤 반드시 확인:

```bash
python3 -m venv /tmp/chk && source /tmp/chk/bin/activate
pip install -q -r requirements.txt
python -m app.ingest.check_corpus     # 몇 글자 읽혔는지 확인
deactivate
```

### 5-A-6. docker compose로 구동

`docker-compose.yml`이 준비돼 있습니다. 명령 세 개면 끝납니다.

```bash
# ① 키가 통하는지 먼저 확인 (서버를 띄우기 전에)
docker compose --profile tools run --rm smoke

# ② 넣은 문서가 실제로 읽히는지 확인
docker compose --profile tools run --rm check

# ③ 서버 구동
docker compose up -d --build
```

`①`이 실패하면 401/403은 키·헤더 형식(2절), 404는 `CLOVA_ENDPOINT` 경로 문제입니다.
`②`에서 "텍스트 0자"가 나오는 파일은 스캔본이라 OCR 결과가 따로 필요합니다.

### 5-A-7. 구조 — 무엇이 어디에 있는가

| 항목 | 위치 | 이유 |
|---|---|---|
| API 키 | 호스트 `.env` → `env_file`로 주입 | 이미지·저장소에 남지 않음 |
| 제공 문서 | 호스트 `./data/corpus` → 읽기 전용 마운트 | 문서를 바꿔도 **이미지 재빌드 불필요** |
| 검색 인덱스 | 호스트 `./data/index` → 마운트 | 재시작 시 재사용, 내용 직접 확인 가능 |

**인덱스는 컨테이너가 뜰 때 만들어집니다**(이미 있으면 재사용).
이미지에 굽지 않습니다 — 이미지 안에는 문서가 없어서, 빌드 시점에 인덱스를
만들면 **mock 문서를 생성해 구워버리기** 때문입니다.

안전장치 두 개가 들어 있습니다:

- 코퍼스가 비어 있으면 **mock 인덱스를 만들지 않습니다.** 인덱스 없이 뜨고,
  모든 질의를 "근거 문서 없음"으로 거절합니다 (지어낸 근거로 답하는 것보다 낫습니다)
- 기존 인덱스가 mock으로 만들어졌는데 실제 문서가 들어와 있으면,
  **자동으로 다시 만듭니다** (개발용 mock 인덱스가 남아 실물을 가리는 사고 방지)

### 5-A-8. 확인

```bash
curl -s http://localhost/health | python3 -m json.tool
docker compose logs -f          # 기동 로그 (mock 경고가 있는지 확인)
```

외부에서: `http://<EC2 퍼블릭 IP>/health`

### 5-A-9. 문서를 나중에 추가·교체할 때

이미지를 다시 빌드할 필요가 없습니다.

```bash
# 호스트의 ./data/corpus/ 에 파일을 넣거나 바꾼 뒤
docker compose --profile tools run --rm reindex   # 인덱스만 다시 생성
docker compose restart                            # 서버가 새 인덱스를 읽게
```

코드를 바꿨을 때만 재빌드합니다:

```bash
git pull
docker compose up -d --build --force-recreate
```

⚠️ **`--force-recreate`를 빼지 마십시오.** compose는 서비스 설정(포트·볼륨·
환경변수 등)이 그대로면 이미지를 새로 빌드해도 **컨테이너를 재생성하지
않을 때가 있습니다** — 그러면 새 이미지가 준비돼 있어도 실행 중인
컨테이너는 여전히 옛 코드로 돕니다(2026-09-05 실측: `git pull` +
`up -d --build`까지 했는데 `/health`가 갱신 전 수치를 그대로 보였다).
추출 로직(예: `analysis/product_facts.py`)이 바뀐 배포라면 재인덱싱도
함께 필요합니다: `FORCE_REINDEX=true docker compose up -d --force-recreate agent`.

### 5-A-10. 자주 쓰는 명령

| 목적 | 명령 |
|---|---|
| 시작 | `docker compose up -d` |
| 중지 | `docker compose down` |
| 로그 | `docker compose logs -f` |
| 재시작 | `docker compose restart` |
| 키 검증 | `docker compose --profile tools run --rm smoke` |
| 문서 점검 | `docker compose --profile tools run --rm check` |
| 인덱스 재생성 | `docker compose --profile tools run --rm reindex` |
| 다른 포트로 | `HOST_PORT=8080 docker compose up -d` |

---

## 5-B. NCP 서버 배포 (참고 — VM 직접 구동 방식)

### 5-1. 어떤 환경을 쓸 것인가

두 가지를 구분해야 합니다.

| | 용도 | 이 프로젝트에서 |
|---|---|---|
| **CLOVA Studio (플레이그라운드/익스플로러)** | 프롬프트를 콘솔에서 실험 | L5'·L6 프롬프트를 손으로 다듬을 때 유용 |
| **NCP Server (VM)** | 우리 FastAPI 서버를 상시 구동 | **평가 API는 여기서 떠야 합니다** |

평가는 `GET /answer`로 **우리 서버에 직접** 들어옵니다. CLOVA Studio는 우리가
호출하는 대상일 뿐이라, 그것만으로는 제출 요건을 못 맞춥니다. 서버가 따로 필요합니다.

> 콘솔 메뉴 구성과 사용 가능한 상품은 계정·리전에 따라 다르고 자주 바뀝니다.
> 아래는 일반적인 순서이며, 실제 화면은 콘솔에서 확인하십시오.

### 5-2. 서버 구성 순서

```
VPC 생성 → Subnet(public) 생성 → Server 생성 → ACG 규칙 → 공인 IP 할당
```

ACG(방화벽) 인바운드 규칙:

| 포트 | 용도 | 접근 소스 |
|---|---|---|
| 22 | SSH | **본인 IP만** (0.0.0.0/0 금지) |
| 80 / 443 | 평가 요청 수신 | 0.0.0.0/0 |

아웃바운드에서 `clovastudio.stream.ntruss.com:443`이 나가야 합니다.
(이 개발 컨테이너에서 막혔던 게 정확히 이 부분입니다)

### 5-3. 서버에서 실행

```bash
# 1) 코드 받기 (키는 포함되지 않음)
git clone https://github.com/eunha9348/Pension-Agent.git
cd Pension-Agent
# 기본 브랜치가 이미 최신 작업 브랜치입니다(clone만으로 충분).
# 특정 브랜치를 명시하려면: git checkout claude/claude-implementation-plan-hpz25z

# 2) 의존성
sudo apt-get update && sudo apt-get install -y python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3) ★ 키 입력 — 서버에서 직접 ★
cp .env.example .env
nano .env                    # CLOVA_API_KEY 채우기
chmod 600 .env               # 다른 사용자가 못 읽게

# 4) 실연동 확인
python -m app.llm.smoke_test

# 5) 문서 넣고 인덱스 생성 (git에 올리지 않았다면 scp로 전송)
#    로컬에서:  scp -r ./문서폴더 root@<서버IP>:~/Pension-Agent/data/corpus/
python -m app.ingest.check_corpus
python -m app.ingest.build_index

# 6) 상시 구동
sudo cp scripts/pension-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pension-agent
sudo systemctl status pension-agent
```

로그 확인:

```bash
sudo journalctl -u pension-agent -f
```

기동 로그에 `★ mock 모드` 배너가 보이면 키가 안 잡힌 것입니다.

### 5-4. 80/443 노출

평가 요청이 8000 포트로 오지 않는다면 앞단이 필요합니다.

```bash
sudo apt-get install -y nginx
sudo cp scripts/nginx-pension-agent.conf /etc/nginx/sites-available/pension-agent
sudo ln -sf /etc/nginx/sites-available/pension-agent /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

HTTPS는 도메인 연결 후:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <도메인>
```

### 5-5. Docker로 하려면

```bash
docker build -t pension-agent .
docker run -d --restart=always -p 80:8000 \
  --env-file .env \
  -v $(pwd)/data/corpus:/app/data/corpus \
  --name pension-agent pension-agent
```

`--env-file`로 키를 주입하므로 이미지에는 키가 들어가지 않습니다.
코퍼스는 볼륨으로 마운트하면 이미지 크기를 키우지 않습니다.

---

## 6. 운영 점검

```bash
curl -s http://<서버>/health | python3 -m json.tool
```

| 필드 | 정상값 |
|---|---|
| `llm.is_mock` | `false` ← 실연동 확인 |
| `corpus.kind` | `real` ← mock 문서가 아님 |
| `corpus.documents` | 0보다 큼 |
| `corpus.skipped_count` | 0에 가까울수록 좋음 |
| `index_ready` | `true` |
| `llm_usage.total_tokens` | 크레딧 소모 추적 |

무중단 운영 중 확인할 것:

- **크레딧** — `llm_usage`로 누적 토큰을 보고 남은 크레딧을 역산하십시오.
  초과분은 주최측이 보전하지 않습니다.
- **재시작 복구** — `systemctl enable`로 부팅 시 자동 기동됩니다.
- **인덱스** — `data/index/`는 git에 없으므로, 서버를 새로 만들면
  `build_index`를 다시 돌려야 합니다.

---

## 7. 자주 나오는 실수

| 증상 | 원인 | 해결 |
|---|---|---|
| 답변이 딱딱하고 템플릿 같다 | mock 모드 | `/health`의 `is_mock` 확인 → `.env` 키 |
| 모든 질의에 "근거를 찾지 못했습니다" | 인덱스가 비었거나 문서 판독 실패 | `check_corpus` 실행 |
| 지어낸 내용으로 답한다 | mock 코퍼스 사용 중 | `corpus.kind`가 `real`인지 확인 |
| 502 / 연결 거부 | 서버 미기동 또는 ACG 차단 | `systemctl status`, ACG 인바운드 |
| CLOVA 호출만 실패 | 아웃바운드 차단 | 서버에서 `curl -I https://clovastudio.stream.ntruss.com` |
| `git pull` + `up -d --build` 했는데 `/health`가 그대로 | compose가 컨테이너를 재생성 안 함 | `--force-recreate` 추가 (§5-A-9 참고) |
| 추출·검증 로직을 바꿨는데 결과가 그대로 | 인덱스가 호스트 볼륨에 남아 재사용됨 | `FORCE_REINDEX=true`로 한 번 재기동 |

---

## 참고

- [CLOVA Studio 개요](https://api.ncloud-docs.com/docs/ai-naver-clovastudio-summary)
- [Chat Completions v3](https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3)
- [Function calling](https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3-fc)
- [CLOVA Studio 상품 페이지](https://www.ncloud.com/product/aiService/clovaStudio)
