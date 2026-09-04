FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 재OCR 진단 도구(app/ingest/reocr.py · scripts/reocr_probe.py) — 교차
# 문서 대조로 못 메운 판독 실패 페이지를 원본 이미지에서 다시 읽어 볼 때
# 쓴다. poppler-utils(pdftoppm)는 PDF 페이지를 실제 화면 그대로 렌더링해
# "어떤 임베디드 이미지가 그 페이지인지" 추측하지 않게 한다. 없어도 기동은
# 되고 재OCR/진단만 건너뛴다(정직하게 "엔진 없음"으로 보고한다) — apt
# 설치가 막힌 환경에서도 서비스 전체가 죽지 않게 하기 위해서다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-kor poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# apt의 tesseract-ocr-kor는 속도 우선(fast) LSTM 모델이라 정확도가 낮다.
# 실측(2026-09-05)에서 재OCR 결과가 거의 전부 라틴 문자·숫자 잡음으로
# 나온 원인 중 하나로 의심돼, 공식 고정밀(tessdata_best) 한국어 모델을
# 따로 받아 둔다. `reocr.py`가 이 디렉터리가 있으면 우선 쓰고, 없으면
# (네트워크 문제 등으로 다운로드 실패해도) apt 기본 모델로 조용히
# 대체한다 — 이것 때문에 빌드 자체가 실패하면 안 된다.
RUN mkdir -p /opt/tessdata_best && \
    (python3 -c "\
import urllib.request; \
base='https://github.com/tesseract-ocr/tessdata_best/raw/main/'; \
[urllib.request.urlretrieve(base+n, '/opt/tessdata_best/'+n) for n in ('kor.traineddata','eng.traineddata')]" \
     && echo '✅ tessdata_best 다운로드 완료' \
     || (rm -rf /opt/tessdata_best && echo '⚠️ tessdata_best 다운로드 실패 — apt 기본 모델로 대체됩니다'))

# 의존성 레이어 분리 — 소스만 바뀌면 재설치하지 않는다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 로컬 임베딩 백엔드(선택). WITH_EMBEDDING=true 로 빌드할 때만 설치한다.
#   docker compose build --build-arg WITH_EMBEDDING=true
# CPU 전용 휠을 받는다 — GPU 없는 인스턴스에서 CUDA 빌드는 수 GB 낭비다.
ARG WITH_EMBEDDING=false
COPY requirements-embedding.txt .
RUN if [ "$WITH_EMBEDDING" = "true" ]; then \
        pip install --no-cache-dir -r requirements-embedding.txt \
            --extra-index-url https://download.pytorch.org/whl/cpu ; \
    fi

# 모델을 컨테이너 안에 캐시하면 재시작할 때마다 다시 받는다 —
# 호스트 볼륨(/app/data/models)에 두고 재사용한다(docker-compose.yml 참고).
ENV HF_HOME=/app/data/models \
    SENTENCE_TRANSFORMERS_HOME=/app/data/models

COPY app/ ./app/
COPY sql/ ./sql/
# 평가셋도 넣는다 — 배포한 그 환경에서 실물 코퍼스로 품질을 재기 위해서다.
# (`docker compose --profile tools run --rm eval`)
# 로컬에서만 돌리면 실물 문서 기준 점수를 영영 알 수 없다.
COPY tests/ ./tests/
# 운영 진단 스크립트도 넣는다. 예전에는 entrypoint 하나만 복사해서,
# 배포 환경에서만 재현되는 문제(L1 function calling 400 등)를 진단하려면
# 그때마다 이미지를 고쳐야 했다 — 진단 도구가 현장에 없으면 소용이 없다.
COPY scripts/ ./scripts/
RUN cp scripts/docker-entrypoint.sh /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

# ⚠️ 인덱스를 **빌드 시점에 만들지 않는다.**
#    이미지 안에는 코퍼스가 없으므로, 여기서 build_index를 돌리면
#    mock 문서를 생성해 인덱스를 지어 이미지에 구워 넣게 된다.
#    그 이미지로 평가를 받으면 '지어낸 문서'를 근거로 답변한다.
#    → 코퍼스는 볼륨으로 마운트하고, 인덱스는 기동 시 entrypoint가 만든다.
RUN mkdir -p data/corpus data/index

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
