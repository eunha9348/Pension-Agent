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
    # 세법상 '부득이한 사유'는 근퇴법 중도인출 사유와 다른 개념이다.
    # A1의 요점이 그 차이이므로 별도 탐색어로 세법 쪽을 잡는다.
    "A1_세법": ("부득이한 사유",),
    # '개인형퇴직연금제도'는 정의·설정 조문만 잡았다. 인출·급여 쪽으로 좁힌다.
    "A2": ("개인형퇴직연금제도", "적립금"),
    "A7": ("연금외수령",),
    "B1": ("연금수령연차",),
    "B2": ("연금수령한도",),
    # '분리과세'만으로는 이자·배당·주택임대까지 끌려온다. 연금 쪽으로 좁힌다.
    "C1": ("분리과세", "연금소득"),
    # '사적연금소득'은 법령 용어가 아니라 후보가 0건이었다.
    # 법문에 실제로 쓰이는 표기로 바꾼다.
    "C2": ("분리과세연금소득",),
    # 한도 수치가 든 조문을 직접 노린다. 제59조의3 제2항·제5항은
    # '세액공제'라는 말은 있어도 수치가 없어서 앵커로 쓸 수 없다.
    "C4": ("연금계좌", "600만원"),
    "C5": ("연금계좌", "900만원"),
    "C6": ("이연퇴직소득",),
    "D1": ("연금수령",),
    # '퇴직연금제도'는 정의·부담금까지 잡았다. 전환·변경 쪽으로 좁힌다.
    "E5": ("퇴직연금제도", "변경"),
}

# 사람이 확인한 앵커만 등재한다. 형식: trap_id -> (조문참조, ...)
#
# ⚠️ 아래 항목은 전부 2026-08-28 실수집본(법제처 7,426건)의 --propose 출력에서
#    **조문 원문을 눈으로 확인하고** 옮긴 것이다. 기억으로 적은 것은 하나도 없다.
#    항번호는 원문이 ①로 싣지만 일반숫자로 적는다 — canon_ref의 NFKC가
#    ①→1로 정규화하므로 같은 조문을 가리킨다(테스트로 고정).
#    --verify 가 매번 실재를 재확인한다.
ANCHORS: dict[str, tuple[str, ...]] = {
    # 근퇴법상 중도인출 사유. "주택구입 등 대통령령으로 정하는 사유"가
    # 제22조에 그대로 있고, IRP는 제24조 제5항이 시행령으로 위임한다.
    # ⚠️ 세법상 '부득이한 사유'(기타소득세 16.5% vs 연금소득세) 쪽 조문은
    #    아직 못 찾았다 — A1의 핵심은 두 기준의 **차이**이므로, 세법 쪽이
    #    등재되기 전까지 이 앵커만으로는 절반이다.
    "A1": (
        "근로자퇴직급여 보장법 제22조",
        "근로자퇴직급여 보장법 제24조 제5항",
        "근로자퇴직급여 보장법 시행령 제18조 제2항",
        "근로자퇴직급여 보장법 시행령 제14조 제2항",
    ),

    # 연금외수령의 법적 정의. 제40조의2 제5항이 "연금수령한도를 초과하여
    # 인출하는 금액은 연금외수령하는 것으로 본다"고 못박는다.
    "A7": (
        "소득세법 시행령 제40조의2 제3항",
        "소득세법 시행령 제40조의2 제5항",
        "소득세법 제146조 제2항",
    ),

    # 연금수령연차 정의(시행령 제40조의2 제4항)가 B1의 핵심이다 —
    # "최초로 연금수령할 수 있는 날이 속하는 과세기간을 기산연차로 하여
    # 그 다음 과세기간을 누적 합산한 연차". 실제 인출 여부와 무관하다는
    # 것이 이 문장에서 직접 읽힌다.
    "B1": (
        "소득세법 시행령 제40조의2 제4항",
        "소득세법 시행령 제202조의2 제2항",
        "소득세법 시행령 제202조의2 제3항",
        "소득세법 제146조 제2항",
    ),

    # 연금수령한도와 그 초과분 처리. 제40조의3 제3항이 인출 순서
    # (연금수령분 먼저, 그 다음 연금외수령분)를 정한다.
    "B2": (
        "소득세법 시행령 제40조의2 제4항",
        "소득세법 시행령 제40조의2 제5항",
        "소득세법 시행령 제40조의3 제3항",
    ),

    # 이연퇴직소득세의 정의와 계산. 시행령 제202조의2가 계산식 전체를 담는다.
    "C6": (
        "소득세법 제146조의2 제1항",
        "소득세법 시행령 제202조의2 제1항",
        "소득세법 시행령 제202조의2 제2항",
        "소득세법 시행령 제202조의2 제3항",
        "소득세법 시행령 제202조의2 제4항",
    ),

    # 연금수령의 요건·개시·한도 전반.
    "D1": (
        "소득세법 시행령 제40조의2 제3항",
        "소득세법 시행령 제40조의2 제4항",
        "소득세법 시행령 제40조의2 제5항",
        "소득세법 시행령 제40조의3 제3항",
    ),

    # 세액공제 한도. 제59조의3 제1항이 두 한도를 한 문장에 담고 있다 —
    #   "연금저축계좌에 납입한 금액이 연 600만원을 초과하는 경우에는 그
    #    초과하는 금액은 없는 것으로 하고, 연금저축계좌에 납입한 금액 중
    #    600만원 이내의 금액과 퇴직연금계좌에 납입한 금액을 합한 금액이
    #    연 900만원을 초과하는 경우에는 그 초과하는 금액은 없는 것으로 한다"
    # 공제율(100분의 12, 4천500만원 이하는 100분의 15)도 같은 항에 있다.
    #
    # ⚠️ 조세특례제한법 제86조의4는 달지 않는다. 같은 600/900 수치를 담고
    #    있지만 "2022년 12월 31일까지"로 **일몰된 50세 이상 특례**다.
    #    앵커로 달면 HCX가 없어진 특례를 현행으로 인용할 수 있다.
    "C4": ("소득세법 제59조의3 제1항",),

    # 구법 수치 경고의 교정문이 "현행 기준"을 밝히므로, 그 현행 기준의
    # 근거가 앵커다. 제59조의3 제1항에 600·900과 함께 소득 기준
    # '4천 500만원'이 있어, C5가 말하는 "4천만원(→현행 4,500만원)"의
    # 현행 쪽을 직접 뒷받침한다.
    "C5": ("소득세법 제59조의3 제1항",),
}

# ── 앵커로 쓰면 안 되는 조문 (수치가 같아 섞이기 쉬운 것들) ──────
# 아래는 --grep 으로 실제 확인한 것이다. 테스트가 등재를 막는다.
#
# 소득세법 제47조의2 제1항 — 900만원이 나오지만 **연금소득공제** 한도다.
#   C4의 900만원(연금계좌세액공제)과 제도가 다르다. 수치가 같아 섞기 쉽다.
#
# 조세특례제한법 제86조의4 — 600/900 수치를 담지만 "2022년 12월 31일까지"로
#   일몰된 50세 이상 특례다. 여기 나오는 "총급여액 1억 2천만원 초과" 구간이
#   바로 C5가 '폐지됐다'고 경고하는 그 구간이다. 현행으로 인용되면 안 된다.
BLOCKED_ANCHORS: dict[str, str] = {
    "소득세법 제47조의2 제1항": "연금소득공제 한도(900만원) — 세액공제와 다른 제도",
    "조세특례제한법 제86조의4": "2022.12.31 일몰된 50세 이상 특례 — 현행 아님",
}

# ── 아직 등재하지 못한 규칙과 그 이유 ────────────────────────────
# 추측으로 채우지 않기 위해 남겨 둔다. 탐색어를 고쳐 재제안을 받아야 한다.
#
# C1 (1,500만원 초과 시 '초과분'이 아니라 '전액')
# C2 (1,500만원 산정에서 제외되는 소득)
#   --grep '분리과세연금소득'이 소득세법 제64조의4를 잡았고, 거기에
#   "분리과세연금소득 외의 연금소득에 100분의 15를 곱하여"까지는 있으나
#   **1,500만원 기준 자체가 발췌에 없었다.** 기준 금액이 두 규칙의 요점이라
#   그 문장을 확인하기 전에는 달 수 없다. 제20조의3 쪽을 더 봐야 한다.
#
# C3 (15% vs 16.5% 지방소득세 포함 여부)
#   제59조의3 제1항의 '100분의 12/15'는 **세액공제율**이지 분리과세율이
#   아니다. 맥락이 달라 앵커로 쓰면 안 된다. 분리과세 세율 조문이 따로 필요.
#
# A2 (IRP vs 연금저축 인출 규칙)
#   '개인형퇴직연금제도'가 정의·설정 조문만 잡았다. 인출 규칙 조문 필요.
#
# E5 (제도 전환 방향)
#   '퇴직연금제도'가 정의·부담금 조문만 잡았다. 전환 조문 필요.


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
    g.add_argument("--grep", nargs="+", metavar="용어",
                   help="주어진 용어가 **모두** 든 조문을 찾는다. "
                        "탐색어를 맞춰가며 앵커를 찾을 때 쓴다.")
    ap.add_argument("--show", type=int, default=400,
                    help="--grep 시 조문당 출력할 글자 수 (기본 400)")
    ap.add_argument("--limit", type=int, default=12,
                    help="--grep 시 최대 조문 수 (기본 12)")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = get_store(reload=True)

    if store.is_empty:
        print("법령 저장소가 비어 있습니다. 먼저 crawler를 실행하십시오:")
        print("  python -m app.law.crawler --oc <OC> --dry-run")
        return 1

    print(f"조문 {len(store)}건 · 법령 {', '.join(store.law_names)}\n")

    if a.grep:
        hits = store.search(*a.grep)
        print(f"'{' + '.join(a.grep)}' 이(가) 모두 든 조문: {len(hits)}건"
              f"{f' (앞 {a.limit}건만 표시)' if len(hits) > a.limit else ''}\n")
        for art in hits[:a.limit]:
            print(f"[{art.ref}]  시행 {art.effective_date}")
            print(f"    {art.text[:a.show]}\n")
        if not hits:
            print("  후보 없음 — 용어를 바꿔 보십시오. 법령은 구어와 표기가 다릅니다"
                  "(예: '사적연금소득'(X) → '분리과세연금소득'(O)).")
        return 0

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
