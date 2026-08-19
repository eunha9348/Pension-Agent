#!/usr/bin/env bash
# 임베딩 생성을 백그라운드로 돌린다 — 터미널을 닫아도 계속된다.
#
# ━━ 왜 필요한가 ━━
# 8천 건 임베딩은 속도 제한 때문에 1~2시간이 걸린다. 그동안 SSH 창을 열어
# 두고 지켜볼 이유가 없다. 노트북·태블릿을 닫아도 EC2에서 계속 돌아야 한다.
#
#   ./scripts/embed-bg.sh            시작 (기본 간격)
#   ./scripts/embed-bg.sh 2.0        429가 잦으면 간격을 늘려서 시작
#   ./scripts/embed-bg.sh status     진행 상황 보기
#   ./scripts/embed-bg.sh stop       중단 (여기까지 저장됨 — 다시 시작하면 이어서)
set -euo pipefail

cd "$(dirname "$0")/.."
LOG="embed.log"
PIDFILE=".embed.pid"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-start}" in
  status)
    if running; then
      echo "▶ 진행 중 (PID $(cat "$PIDFILE"))"
    else
      echo "■ 실행 중이 아닙니다"
    fi
    echo "── 최근 로그 ──────────────────────────────"
    tail -n 15 "$LOG" 2>/dev/null || echo "(로그 없음)"
    echo "───────────────────────────────────────────"
    echo "실시간으로 보려면:  tail -f $LOG   (Ctrl+C 로 보기만 종료 — 작업은 계속됨)"
    ;;

  stop)
    if running; then
      kill -INT "$(cat "$PIDFILE")"      # INT로 보내야 여기까지 저장하고 끝난다
      echo "중단 요청을 보냈습니다. 여기까지 만든 벡터는 저장됩니다."
      echo "다시 시작하면 이어서 만듭니다."
    else
      echo "실행 중이 아닙니다."
    fi
    rm -f "$PIDFILE"
    ;;

  *)
    if running; then
      echo "이미 실행 중입니다 (PID $(cat "$PIDFILE")). 상태를 보려면:"
      echo "  ./scripts/embed-bg.sh status"
      exit 1
    fi
    PACE="${1:-0.3}"
    echo "임베딩 생성을 백그라운드로 시작합니다 (요청 간격 ${PACE}초)"
    nohup docker compose --profile tools run --rm embed \
        python -m app.ingest.build_embeddings --pace "$PACE" \
        > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "  PID $(cat "$PIDFILE")  ·  로그: $LOG"
    echo ""
    echo "이제 터미널을 닫으셔도 됩니다. 나중에 확인:"
    echo "  ./scripts/embed-bg.sh status"
    ;;
esac
