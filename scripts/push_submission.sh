#!/usr/bin/env bash
# 주최측 제출용 push 스크립트 — CLAUDE.md를 제외하고 push한다.
#
# ━━ 왜 필요한가 ━━
# CLAUDE.md는 이 저장소의 개발용 컨텍스트 파일(내부 함정·결함 이력·
# 설계 원칙)이다. 우리 개발 브랜치(claude/claude-implementation-plan-hpz25z,
# Pension-Agent-of-ADC)에는 그대로 남아 있어야 하지만, 주최측이 지정한
# Github Organization의 Private Repository에는 이 파일이 넘어가면 안 된다.
#
# git push는 브랜치 트리 전체를 그대로 옮기므로 .gitignore(추적 중인
# 파일에는 효과 없음)·.gitattributes export-ignore(git archive에만
# 적용, push에는 무관) 어느 쪽도 push 자체를 막지 못한다. 그래서 이
# 스크립트는 "제출용 스냅숏 브랜치"를 로컬에 만든다 — 현재 개발
# 브랜치의 트리를 그대로 복사하되 CLAUDE.md만 삭제한 커밋 1개를 얹고,
# 그 브랜치를 주최측 원격에 push한다. 개발 브랜치 자체는 전혀 건드리지
# 않는다.
#
# ━━ 사용법 ━━
#   scripts/push_submission.sh <주최측-원격-URL> [푸시할-브랜치명]
#
#   예:
#     scripts/push_submission.sh git@github.com:someorg/pension-agent-eval.git main
#     scripts/push_submission.sh https://github.com/someorg/pension-agent-eval.git submission
#
#   브랜치명을 생략하면 "main"으로 push한다.
#
# ━━ 무엇을 하는가 (순서) ━━
#   1. 현재 브랜치가 깨끗한지 확인한다(커밋 안 된 변경이 있으면 중단).
#   2. 로컬 "submission" 브랜치를 현재 브랜치의 최신 커밋으로 재생성한다
#      (이미 있으면 강제로 다시 만든다 — 매번 최신 개발 브랜치를 그대로
#      반영하기 위해서다. 이 브랜치는 순수 파생물이라 그 자체의 이력을
#      보존할 이유가 없다).
#   3. CLAUDE.md를 그 브랜치에서만 삭제하고 커밋한다.
#   4. 사용자에게 최종 확인을 받은 뒤(--yes 없이는 여기서 멈춘다),
#      지정한 원격 URL의 지정한 브랜치로 강제 push한다.
#
# ━━ 안전장치 ━━
#   · --yes를 주지 않으면 실제 push는 하지 않고 "무엇을 할 것인지"만
#     보여주고 종료한다(dry-run 기본값).
#   · 원격 URL을 매번 인자로 받는다 — 이 저장소의 git remote에는
#     주최측 원격을 등록해 두지 않는다(잘못 눌러 개발 브랜치를 그쪽에
#     밀어 넣는 사고를 원천 차단).
#   · CLAUDE.md 외의 다른 파일은 절대 건드리지 않는다.
set -euo pipefail

usage() {
  echo "사용법: $0 <주최측-원격-URL> [푸시할-브랜치명=main] [--yes]" >&2
  exit 1
}

[ $# -ge 1 ] || usage

REMOTE_URL="$1"
shift

PUSH_BRANCH="main"
CONFIRM=0
for arg in "$@"; do
  case "$arg" in
    --yes) CONFIRM=1 ;;
    -*) usage ;;
    *) PUSH_BRANCH="$arg" ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [ ! -f "CLAUDE.md" ]; then
  echo "⚠️  CLAUDE.md가 저장소 루트에 없습니다. 저장소 상태를 확인하세요." >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  커밋되지 않은 변경사항이 있습니다. 먼저 커밋하거나 stash 하세요." >&2
  git status --short
  exit 1
fi

SRC_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
SRC_COMMIT="$(git rev-parse HEAD)"

echo "── 제출용 브랜치 준비 ──────────────────────────────"
echo "원본 브랜치 : $SRC_BRANCH ($SRC_COMMIT)"
echo "제출 브랜치 : submission (로컬, CLAUDE.md 제외)"
echo "대상 원격   : $REMOTE_URL"
echo "대상 브랜치 : $PUSH_BRANCH"
echo ""

# 로컬 submission 브랜치를 현재 브랜치 최신 상태로 강제 재생성한다.
# 이 브랜치는 매번 새로 만드는 파생물이라 이전 이력을 보존하지 않는다.
git branch -f submission "$SRC_COMMIT"

git -c advice.detachedHead=false worktree list >/dev/null 2>&1 || true
WORKTREE_DIR="$(mktemp -d)"
trap 'git worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true; rm -rf "$WORKTREE_DIR"' EXIT

git worktree add -f "$WORKTREE_DIR" submission >/dev/null

(
  cd "$WORKTREE_DIR"
  git rm --cached --quiet CLAUDE.md
  rm -f CLAUDE.md
  git commit -q -m "chore: 제출용 스냅숏 — CLAUDE.md 제외 (원본 $SRC_COMMIT)"
)

echo "✅ 로컬 submission 브랜치 준비 완료. 포함 여부 확인:"
if git show submission:CLAUDE.md >/dev/null 2>&1; then
  echo "❌ CLAUDE.md가 여전히 포함돼 있습니다 — push를 중단합니다." >&2
  exit 1
else
  echo "   CLAUDE.md 없음 확인됨."
fi
echo ""
echo "submission 브랜치와 $SRC_BRANCH 의 차이 (CLAUDE.md 외 파일):"
git diff --stat "$SRC_BRANCH" submission -- . ':!CLAUDE.md' | tail -5 || true

if [ "$CONFIRM" -ne 1 ]; then
  echo ""
  echo "── dry-run 종료 ──────────────────────────────────"
  echo "실제로 push하려면 --yes를 붙여 다시 실행하세요:"
  echo "  $0 \"$REMOTE_URL\" \"$PUSH_BRANCH\" --yes"
  exit 0
fi

echo ""
echo "── 실제 push 시작 ────────────────────────────────"
git push "$REMOTE_URL" "submission:$PUSH_BRANCH"
echo "✅ push 완료: $REMOTE_URL ($PUSH_BRANCH)"
