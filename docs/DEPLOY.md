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

---

## 2. 키를 넣는 자리 (파일 1개, 줄 1개)

```bash
cp .env.example .env
```

`.env` 파일을 열어 **11번째 줄**을 채웁니다:

```bash
CLOVA_API_KEY=nv-xxxxxxxxxxxxxxxxxxxxxxxxxxxx     # ← ★ 여기 ★
CLOVA_REQUEST_ID=                                  # 선택 (콘솔에서 발급)
CLOVA_ENDPOINT=https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005
LLM_MODE=auto
```

이게 전부입니다. 코드 어디도 고칠 필요가 없습니다.

- `LLM_MODE=auto` — 키가 있으면 실호출, 없으면 자동으로 mock
- 키를 읽는 곳은 `app/config.py:73` 한 곳이고,
  실제 헤더에 실리는 곳은 `app/llm/clova.py:97` 한 곳입니다

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
| 401 / 403 | 키 종류(테스트/서비스)와 엔드포인트가 맞는 조합인지. `app/llm/clova.py`의 `_headers()` |
| 404 | `CLOVA_ENDPOINT`의 버전 경로와 모델명 |
| toolCalls 파싱 실패 | 응답 원문을 보고 `call_with_functions()` 파싱부를 실제 스키마에 맞출 것 |
| 타임아웃 | `.env`의 `CLOVA_TIMEOUT_SEC` 상향 |

> Function calling은 `maxTokens`가 1024보다 커야 동작합니다.
> 코드에서 최소 1100으로 보장하고 있으니 낮추지 마십시오.

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
git checkout claude/claude-implementation-plan-hpz25z

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

### 5-A-6. 키 테스트 (컨테이너를 세우기 전에 먼저)

이미지를 한 번 빌드해 두면, 서버를 안 띄우고도 키만 빠르게 검증할 수 있습니다.

```bash
docker build -t pension-agent .
docker run --rm --env-file .env pension-agent python -m app.llm.smoke_test
```

401/403이면 키·헤더 형식(2절 참고), 404면 `CLOVA_ENDPOINT` 경로를 확인하십시오.

### 5-A-7. 서버 구동 (재부팅에도 살아남게)

```bash
docker run -d --name pension-agent --restart=always \
  -p 80:8000 --env-file .env \
  pension-agent
```

`--restart=always`가 있으면 EC2가 재부팅돼도 컨테이너가 자동으로 다시 뜹니다.
태블릿 창을 닫아도 상관없습니다 — 컨테이너는 EC2 안에서 독립적으로 돌아갑니다.

### 5-A-8. 확인

```bash
curl -s http://localhost/health | python3 -m json.tool
docker logs pension-agent --tail 30
```

외부에서: `http://<EC2 퍼블릭 IP>/health`

문서를 나중에 추가하면 이미지를 다시 빌드해야 합니다:

```bash
docker stop pension-agent && docker rm pension-agent
docker build -t pension-agent .        # data/corpus 최신 내용으로 인덱스 재생성
docker run -d --name pension-agent --restart=always -p 80:8000 --env-file .env pension-agent
```

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
git checkout claude/claude-implementation-plan-hpz25z

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

---

## 참고

- [CLOVA Studio 개요](https://api.ncloud-docs.com/docs/ai-naver-clovastudio-summary)
- [Chat Completions v3](https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3)
- [Function calling](https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3-fc)
- [CLOVA Studio 상품 페이지](https://www.ncloud.com/product/aiService/clovaStudio)
