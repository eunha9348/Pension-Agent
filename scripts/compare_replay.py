"""두 replay_audit_300.py 실행 결과를 비교한다.

합계 점수만 보면 "26 → 26"처럼 아무 변화가 없어 보여도, 실제로는
몇 건이 새로 통과하고 몇 건이 새로 틀려서 우연히 합이 같은 경우와
정말로 아무것도 안 바뀐 경우를 구분하지 못한다. 이 스크립트는 그 둘을
가른다 — 케이스ID 단위로 O/X가 어느 쪽으로 뒤집혔는지 보여준다.

사용법
------
    python -m scripts.compare_replay before.json after.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "results" in data:
        data = data["results"]           # 구버전 포맷 대비
    return {r["id"]: r for r in data}


def _mark(hit) -> str:
    return "?" if hit is None else ("O" if hit else "X")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    a = ap.parse_args(argv)

    before = _load(a.before)
    after = _load(a.after)

    ids = sorted(set(before) | set(after))
    missing_before = [i for i in ids if i not in before]
    missing_after = [i for i in ids if i not in after]
    if missing_before:
        print(f"⚠️  이전 결과에 없는 ID: {missing_before}")
    if missing_after:
        print(f"⚠️  이후 결과에 없는 ID: {missing_after}")

    improved, regressed, still_hit, still_miss, err_flip = [], [], [], [], []
    for i in ids:
        if i not in before or i not in after:
            continue
        b, af = before[i], after[i]
        bm, am = _mark(b.get("auto_hit")), _mark(af.get("auto_hit"))
        b_err, a_err = bool(b.get("http_error")), bool(af.get("http_error"))

        if b_err != a_err:
            err_flip.append((i, b_err, a_err))
        if bm == "X" and am == "O":
            improved.append(i)
        elif bm == "O" and am == "X":
            regressed.append(i)
        elif bm == "O" and am == "O":
            still_hit.append(i)
        elif bm == "X" and am == "X":
            still_miss.append(i)

    print(f"\n비교: {a.before} → {a.after}")
    print(f"  전체 {len(ids)}건")
    print(f"  새로 통과(X→O): {len(improved)}건  {improved}")
    print(f"  새로 실패(O→X): {len(regressed)}건  {regressed}")
    print(f"  계속 통과(O→O): {len(still_hit)}건")
    print(f"  계속 실패(X→X): {len(still_miss)}건")
    if err_flip:
        print(f"  http_error 상태 변화: {len(err_flip)}건")
        for i, b_err, a_err in err_flip:
            print(f"    {i}: 오류 {b_err} → {a_err}")

    total_before_hit = sum(1 for r in before.values() if r.get("auto_hit"))
    total_after_hit = sum(1 for r in after.values() if r.get("auto_hit"))
    print(f"\n  합계: {total_before_hit} → {total_after_hit}")

    if not improved and not regressed:
        print("\n  ★ X→O도 O→X도 0건 — 정말로 같은 26건이 그대로 통과/실패했다는 뜻이다.")
        print("    (합계가 우연히 같은 게 아니라, 판정이 바뀐 케이스가 아예 없다)")
    elif improved and regressed:
        net = len(improved) - len(regressed)
        print(f"\n  ★ {len(improved)}건 개선 + {len(regressed)}건 퇴행 = 순변화 {net:+d}."
              f" 합계만 보면 이 상쇄가 안 보인다.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
