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
COPY scripts/docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

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
