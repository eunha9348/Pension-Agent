"""함정 규칙 → 근거 조문 앵커.

⚠️ 이 파일의 설계 원칙 — **조문 번호를 사람이나 모델의 기억으로 적지 않는다.**

   "C4의 근거는 소득세법 제△△조" 같은 서술은 그럴듯하지만, 틀렸을 때
   틀린 줄 알 방법이 없다. 그래서 이 모듈은 조문 번호를 **주장하지 않고**,
   각 규칙에 '어떤 말이 들어간 조문을 찾아야 하는가'(search_terms)만 둔다.
   실제 조문 번호는 수집한 원문을 검색해서 얻고, 사람이 확인한 것만
   ANCHORS에 등재된다.

   등재되기 전까지 그 규칙은 law_backed=False다. 시스템은 "법령 근거가
   있다"고 말하지 않는다 — 없는 근거를 있다고 하는 것이 가장 나쁘다.

작업 순서
--------
  1. python -m app.law.crawler --oc <OC> --out data/law/articles.json
  2. python -m app.law.anchors --propose    ← 후보 조문 제안
  3. 사람이 확인 → ANCHORS에 등재
  4. python -m app.law.anchors --verify     ← 등재분이 실재하는지 재확인
"""

from __future__ import annotations

import argparse
import logging

from app.law.store import LawStore, get_store

log = logging.getLogger(__name__)

# 규칙별 조문 탐색어. 조문 **번호가 아니라** 조문 본문에 등장할 말이다.
# 여기 적힌 것은 사실 주장이 아니라 검색 질의이므로, 틀려도 후보가 안 나올 뿐
# 잘못된 근거를 만들어내지는 않는다.
SEARCH_TERMS: dict[str, tuple[str, ...]] = {
    "A1": ("중도인출",),
    "A2": ("개인형퇴직연금제도",),
    "A7": ("연금외수령",),
    "B1": ("연금외수령", "퇴직소득"),
    "B2": ("연금수령한도",),
    "C1": ("분리과세",),
    "C2": ("사적연금소득",),
    "C4": ("연금계좌", "세액공제"),
    "C5": ("연금계좌", "세액공제"),
    "C6": ("이연퇴직소득",),
    "D1": ("연금수령",),
    "E5": ("퇴직연금제도",),
}

# 사람이 확인한 앵커만 등재한다. 형식: trap_id -> (조문참조, ...)
#
# ⚠️ 비어 있는 것이 정상 초기 상태다. 수집·확인 전에 채우면 안 된다.
#    채워진 항목은 --verify 가 매번 실재를 재확인한다.
ANCHORS: dict[str, tuple[str, ...]] = {}


def law_backed(trap_id: str) -> bool:
    """이 규칙에 확인된 법령 근거가 있는가."""
    return bool(ANCHORS.get(trap_id))


def anchors_for(trap_id: str, store: LawStore | None = None) -> list:
    """등재된 앵커 조문을 실제 객체로. 저장소에 없으면 조용히 빠진다."""
    store = store or get_store()
    out = []
    for ref in ANCHORS.get(trap_id, ()):
        if (a := store.get(ref)) is not None:
            out.append(a)
    return out


def propose(store: LawStore | None = None,
            limit_per_rule: int = 5) -> dict[str, list[tuple[str, str]]]:
    """탐색어로 후보 조문을 뽑는다. 사람이 고를 재료일 뿐 확정이 아니다."""
    store = store or get_store()
    out: dict[str, list[tuple[str, str]]] = {}
    for tid, terms in SEARCH_TERMS.items():
        hits = store.search(*terms)[:limit_per_rule]
        out[tid] = [(a.ref, a.text[:120].replace("\n", " ")) for a in hits]
    return out


def verify(store: LawStore | None = None) -> tuple[list[str], list[str]]:
    """등재된 앵커가 전부 실재하는지 확인. (정상, 문제) 를 돌려준다."""
    store = store or get_store()
    ok, bad = [], []
    for tid, refs in ANCHORS.items():
        for ref in refs:
            if store.get(ref) is None:
                bad.append(f"{tid}: 저장소에 없는 조문 '{ref}'")
            else:
                ok.append(f"{tid}: {ref}")
    return ok, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="함정 규칙의 법령 앵커 제안·검증")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--propose", action="store_true", help="후보 조문 제안")
    g.add_argument("--verify", action="store_true", help="등재분 실재 확인")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = get_store(reload=True)

    if store.is_empty:
        print("법령 저장소가 비어 있습니다. 먼저 crawler를 실행하십시오:")
        print("  python -m app.law.crawler --oc <OC> --dry-run")
        return 1

    print(f"조문 {len(store)}건 · 법령 {', '.join(store.law_names)}\n")

    if a.propose:
        for tid, cands in propose(store).items():
            mark = "✓등재" if law_backed(tid) else "미등재"
            print(f"── {tid} [{mark}]  탐색어={SEARCH_TERMS[tid]}")
            if not cands:
                print("     후보 없음 — 탐색어를 조정하거나 수집 범위를 넓히십시오")
            for ref, head in cands:
                print(f"     {ref}\n        {head}")
            print()
        print("확인한 것만 ANCHORS에 등재하십시오. 추측으로 채우지 말 것.")
        return 0

    ok, bad = verify(store)
    if not ANCHORS:
        print("등재된 앵커가 없습니다 (초기 상태). --propose 로 후보를 보십시오.")
        return 0
    for line in ok:
        print(f"  ✓ {line}")
    for line in bad:
        print(f"  ✗ {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
