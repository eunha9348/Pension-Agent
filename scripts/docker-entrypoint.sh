#!/usr/bin/env sh
# 컨테이너 기동 시 인덱스를 준비한 뒤 서버를 띄운다.
#
# ━━ 왜 빌드 시점이 아니라 기동 시점인가 ━━
# 코퍼스를 볼륨으로 마운트하면 문서를 추가할 때마다 이미지를 다시 빌드하지
# 않아도 된다. 대신 인덱스는 기동할 때 만들어야 한다.
#
# ━━ mock 코퍼스를 절대 자동 생성하지 않는다 ━━
# build_index는 data/corpus가 비어 있으면 mock 문서를 만들어 인덱스를 짓는다.
# 개발 중에는 편하지만 운영 컨테이너에서 그러면 **지어낸 문서로 답변**하게 된다.
# 그래서 여기서는 코퍼스가 비어 있으면 인덱스를 만들지 않고 넘어간다.
# 인덱스가 없으면 서버는 모든 질의에 "근거 문서 없음"으로 거절한다 —
# 거절은 정직하지만, 가짜 근거로 답하는 것은 그렇지 않다.
set -e

CORPUS_DIR="${CORPUS_DIR:-/app/data/corpus}"
INDEX_DIR="${INDEX_PATH:-/app/data/index}"
ALLOW_MOCK_CORPUS="${ALLOW_MOCK_CORPUS:-false}"
FORCE_REINDEX="${FORCE_REINDEX:-false}"

echo "──────────────────────────────────────────────────────────"
echo " 연금 Agent 기동"
echo "──────────────────────────────────────────────────────────"

# ── LLM 키 점검 ──
# 키가 없으면 mock 모드로 뜬다. 그대로 평가를 받으면 안 되므로 크게 알린다.
if [ -z "${CLOVA_API_KEY}" ] && [ "${LLM_MODE}" != "mock" ]; then
    echo ""
    echo " ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★"
    echo " ★ CLOVA_API_KEY 가 비어 있습니다 — mock 모드로 뜹니다."
    echo " ★ HyperCLOVA X 를 호출하지 않고 결정론적 대역으로 답합니다."
    echo " ★"
    echo " ★ 해결: 호스트에서"
    echo " ★        cp .env.example .env  &&  nano .env"
    echo " ★        docker compose up -d"
    echo " ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★"
    echo ""
else
    echo " LLM 키: 설정됨"
fi

corpus_count=0
if [ -d "$CORPUS_DIR" ]; then
    corpus_count=$(find "$CORPUS_DIR" -type f ! -name '.*' 2>/dev/null | wc -l)
fi
echo " 코퍼스 파일: ${corpus_count}건  (${CORPUS_DIR})"

# 기존 인덱스가 mock으로 만들어진 것인지 확인한다.
# ⚠️ 개발 중 만든 mock 인덱스가 남아 있는 상태에서 실제 문서를 넣으면,
#    '인덱스가 이미 있다'는 이유로 그 mock 인덱스를 그대로 쓰게 된다.
#    실물을 넣었다고 믿으면서 지어낸 문서로 답변하는 최악의 조합이다.
index_kind=""
if [ -f "$INDEX_DIR/docs.json" ]; then
    index_kind=$(python - "$INDEX_DIR/docs.json" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("corpus_kind", ""))
except Exception:
    print("")
PY
)
fi

if [ -f "$INDEX_DIR/chunks.json" ] && [ "$corpus_count" -gt 0 ] \
        && [ "$index_kind" != "real" ]; then
    echo " ⚠️  기존 인덱스가 실물 코퍼스로 만들어진 것이 아닙니다 (kind=${index_kind:-불명})."
    echo "     실제 문서가 있으므로 인덱스를 다시 만듭니다."
    python -m app.ingest.build_index || {
        echo " ⚠️  인덱스 재생성 실패 — check_corpus 로 파일을 점검하십시오."
    }
elif [ -f "$INDEX_DIR/chunks.json" ] && [ "$FORCE_REINDEX" != "true" ]; then
    echo " 인덱스: 기존 것 사용 (kind=${index_kind:-불명}, 다시 만들려면 FORCE_REINDEX=true)"
elif [ "$corpus_count" -gt 0 ]; then
    echo " 인덱스: 새로 생성합니다..."
    python -m app.ingest.build_index || {
        echo " ⚠️  인덱스 생성 실패 — 근거 없이 기동합니다."
        echo "     'python -m app.ingest.check_corpus' 로 파일을 점검하십시오."
    }
elif [ "$ALLOW_MOCK_CORPUS" = "true" ]; then
    echo " ⚠️⚠️⚠️  코퍼스가 비어 mock 문서로 인덱스를 만듭니다."
    echo " ⚠️⚠️⚠️  실제 제공 자료가 아닙니다 — 개발용으로만 쓰십시오."
    python -m app.ingest.build_index || true
else
    echo ""
    echo " ❌ 코퍼스가 비어 있어 인덱스를 만들지 않았습니다."
    echo "    mock 문서로 대체하지 않습니다 (지어낸 근거로 답변하게 되므로)."
    echo ""
    echo "    해결: 호스트의 ./data/corpus/ 에 제공 문서를 넣고 재시작하십시오."
    echo "          docker compose restart"
    echo ""
    echo "    이 상태로도 서버는 뜨지만, 모든 질의에 '근거 문서 없음'으로"
    echo "    거절합니다. GET /health 로 상태를 확인할 수 있습니다."
    echo ""
fi

echo "──────────────────────────────────────────────────────────"
exec "$@"
