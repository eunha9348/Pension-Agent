FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성 레이어 분리 — 소스만 바뀌면 재설치하지 않는다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY sql/ ./sql/
COPY data/ ./data/

# 인덱스는 이미지 빌드 시점에 만들어 둔다(기동 지연 제거).
# 실제 문서 zip을 data/corpus/에 넣으면 그 문서로 다시 빌드된다.
RUN python -m app.ingest.build_index || echo "[warn] 인덱스 사전 빌드 실패 — 기동 시 재시도"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
