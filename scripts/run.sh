#!/usr/bin/env bash
# 로컬 원클릭 기동 — 의존성 · 키 · 인덱스를 점검한 뒤 서버를 띄운다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "⚠️  .env 가 없습니다. 만들고 CLOVA_API_KEY 를 채우십시오:"
    echo "      cp .env.example .env && nano .env"
    exit 1
fi

if ! grep -qE '^CLOVA_API_KEY=.+' .env; then
    echo "⚠️  CLOVA_API_KEY 가 비어 있습니다 — mock 모드로 뜹니다."
    echo "    실연동하려면 .env 를 채운 뒤 다시 실행하십시오."
    echo
fi

if [ ! -f data/index/chunks.json ]; then
    echo "인덱스가 없어 새로 만듭니다..."
    python -m app.ingest.build_index
fi

PORT="${PORT:-8000}"
echo "→ http://0.0.0.0:${PORT}  (상태 확인: /health)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
