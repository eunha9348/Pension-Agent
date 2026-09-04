"""replay_audit_300.py 결과(JSON)를 실제로 읽을 수 있게 요약한다.

298건을 사람이 전부 읽을 수는 없다. 이 스크립트는 판정을 대신하지
않는다 — 응답시간 분포, 축퇴 흔적, 이상 패턴을 걸러 **어디부터 사람이
읽어야 하는지**를 좁혀 준다.

    python3 scripts/analyze_replay.py replay_result_*.json
"""

from __future__ import annotations

import glob
import json
import statistics
import sys

# 답변이 이런 문구를 포함하면 HCX 생성이 아니라 결정론적 축퇴(템플릿)일
# 가능성이 높다 — CLAUDE.md의 "정직한 축퇴" 절 참조.
_DEGRADE_MARKERS = (
    "답변을 생성하지 못했습니다",
    "처리 과정 기록이 남지 않았습니다",
    "근거 문서 없음",
)
_TRACE_DEGRADE_MARKERS = (
    "생략", "축퇴", "기각", "실패",
)


def main() -> int:
    paths = sys.argv[1:] or sorted(glob.glob("replay_result_*.json"))
    if not paths:
        print("replay_result_*.json 을 못 찾았습니다. 경로를 인자로 주십시오.")
        return 1
    path = paths[-1]
    print(f"분석 대상: {path}\n")

    data = json.loads(open(path, encoding="utf-8").read())
    n = len(data)

    # ── 응답시간 ──
    times = [r["elapsed"] for r in data if isinstance(r.get("elapsed"), (int, float))]
    times_sorted = sorted(times)
    def pct(p):
        if not times_sorted:
            return None
        i = min(len(times_sorted) - 1, int(len(times_sorted) * p))
        return times_sorted[i]

    print("═" * 62)
    print(" 응답시간 (T3 · PIPELINE_BUDGET_SEC 근거)")
    print("═" * 62)
    if times:
        print(f" 평균 {statistics.mean(times):.1f}s · 중앙값 {statistics.median(times):.1f}s · "
              f"최대 {max(times):.1f}s")
        print(f" p50={pct(0.5):.1f}s  p90={pct(0.9):.1f}s  p95={pct(0.95):.1f}s  p99={pct(0.99):.1f}s")
        over55 = [r for r in data if (r.get("elapsed") or 0) > 55]
        print(f" 55초 초과: {len(over55)}건", end="")
        if over55:
            print(" → " + ", ".join(r["id"] for r in over55[:10])
                  + (" ..." if len(over55) > 10 else ""))
        else:
            print()
    else:
        print(" elapsed 필드 없음")

    # ── HTTP 오류 ──
    errors = [r for r in data if r.get("http_error")]
    print(f"\n HTTP 오류: {len(errors)}건", end="")
    if errors:
        print(" → " + ", ".join(f"{r['id']}({r['http_error']})" for r in errors[:10]))
    else:
        print()

    # ── 축퇴 흔적 ──
    print("\n" + "═" * 62)
    print(" 축퇴(HCX가 만들지 않은 답변) 의심 사례")
    print("═" * 62)
    degraded = []
    for r in data:
        ans = r.get("answer") or ""
        trace = r.get("think_trace") or ""
        reasons = []
        if any(m in ans for m in _DEGRADE_MARKERS):
            reasons.append("답변 텍스트에 축퇴 문구")
        if len(ans.strip()) < 20:
            reasons.append(f"답변이 비정상적으로 짧음({len(ans.strip())}자)")
        if any(m in trace for m in ("L6_재생성_생략", "SubAgent_구제_생략")):
            reasons.append("예산 부족으로 재생성 단계 생략")
        if reasons:
            degraded.append((r["id"], reasons))
    if degraded:
        for qid, reasons in degraded[:20]:
            print(f"  ⚠ {qid}: {'; '.join(reasons)}")
        if len(degraded) > 20:
            print(f"  ... 외 {len(degraded) - 20}건 더")
    else:
        print(" 없음 — 답변 전수가 최소 길이 이상이고 축퇴 문구가 없습니다")
    print(f"\n 합계: {len(degraded)}/{n}건 의심 (반드시 /health의 "
          f"llm_usage.degradation_total 과 함께 볼 것 — 그게 정본 집계입니다)")

    # ── REVISE/BLOCK/DOWNGRADE 흔적 (think_trace 안의 판정어) ──
    print("\n" + "═" * 62)
    print(" L6 감독 판정 흔적 (think_trace 내 등급 언급)")
    print("═" * 62)
    verdict_hits = {"REVISE": [], "DOWNGRADE": [], "BLOCK": []}
    for r in data:
        trace = r.get("think_trace") or ""
        for v in verdict_hits:
            if v in trace:
                verdict_hits[v].append(r["id"])
    for v, ids in verdict_hits.items():
        print(f" {v}: {len(ids)}건" + (f" → {', '.join(ids[:10])}" if ids else ""))

    # ── 카테고리별 자동일치 0인 것 중 표본 3건 미리보기 ──
    print("\n" + "═" * 62)
    print(" 자동일치 실패(auto_hit=False/None) 표본 5건 — 사람이 먼저 볼 후보")
    print("═" * 62)
    misses = [r for r in data if r.get("expected") and not r.get("auto_hit")]
    for r in misses[:5]:
        print(f"\n  [{r['id']}] {r['question'][:60]}")
        print(f"    기대 키워드: {r.get('expected')}")
        print(f"    답변 앞부분: {(r.get('answer') or '')[:150]!r}")
    print(f"\n 자동일치 실패 총 {len(misses)}건(기대 키워드가 설정된 문항 중) — "
          f"--out 파일에서 이 id들의 answer 원문을 사람이 읽고 최종 판정할 것")

    return 0


if __name__ == "__main__":
    sys.exit(main())
