"""코퍼스에 어떤 주제가 실재하는지 조회한다.

    python -m scripts.corpus_probe 코스피 인덱스 리츠
    python -m scripts.corpus_probe --preset bridge

━━ 왜 필요한가 ━━
"도메인 밖 질의를 거절하는 대신, 문서에 있는 관련 상품으로 연결한다"는 설계는
**연결 대상이 실재할 때만** 성립한다. 없는 상품을 연결하면 그건 환각이고,
거절보다 훨씬 나쁘다.

그래서 설계를 확정하기 전에 "그 주제가 제공 자료에 정말 있는가"를 먼저 센다.
추정으로 만들면 안 되는 종류의 결정이기 때문이다.

출력은 용어별 청크 수·문서 수와 실제 문장 예시다. 문서 수가 0이면
그 주제로는 연결하지 않는다(= 그냥 거절한다).
"""

from __future__ import annotations

import argparse
import re
import sys

# 연결(BRIDGE) 설계 검토용 기본 세트.
# 평가에서 거절 대상이 된 질의들이 무엇으로 연결될 수 있는지 본다.
PRESETS = {
    "bridge": [
        "코스피", "KOSPI", "인덱스", "지수", "ETF",
        "리츠", "REITs", "부동산",
        "TDF", "타겟데이트", "생애주기",
        "채권형", "주식형", "혼합형",
    ],
}

MAX_SAMPLES = 3
SAMPLE_CHARS = 110


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="코퍼스 주제 실재 여부 조회")
    ap.add_argument("terms", nargs="*", help="찾을 용어들")
    ap.add_argument("--preset", choices=sorted(PRESETS),
                    help=f"미리 정의된 용어 묶음 ({', '.join(sorted(PRESETS))})")
    ap.add_argument("--samples", type=int, default=MAX_SAMPLES,
                    help=f"용어당 보여줄 문장 예시 수 (기본 {MAX_SAMPLES})")
    args = ap.parse_args(argv)

    terms = list(args.terms)
    if args.preset:
        terms = PRESETS[args.preset] + terms
    if not terms:
        ap.error("찾을 용어를 지정하거나 --preset 을 쓰십시오.")

    from app.ingest.store import get_store
    store = get_store()
    chunks = store.all_chunks()
    if not chunks:
        print("❌ 인덱스가 비어 있습니다. 먼저 build_index 를 실행하십시오.")
        return 1

    docs = {c.doc_id for c in chunks}
    print("═" * 66)
    print(" 코퍼스 주제 조회")
    print("═" * 66)
    print(f" 문서 {len(docs)}건 · 청크 {len(chunks)}건")
    print()

    width = max(len(t) for t in terms) + 2
    found: list[str] = []
    for term in terms:
        pat = re.compile(re.escape(term), re.IGNORECASE)
        hit_chunks = [c for c in chunks if pat.search(c.text)]
        hit_docs = sorted({c.doc_id for c in hit_chunks})
        mark = "✅" if hit_docs else "· "
        print(f" {mark} {term:<{width}} 청크 {len(hit_chunks):>4}건 · "
              f"문서 {len(hit_docs):>3}건", end="")
        if hit_docs:
            found.append(term)
            shown = ", ".join(hit_docs[:5])
            more = f" 외 {len(hit_docs) - 5}건" if len(hit_docs) > 5 else ""
            print(f"  [{shown}{more}]")
            for c in hit_chunks[:args.samples]:
                m = pat.search(c.text)
                lo = max(m.start() - SAMPLE_CHARS // 2, 0)
                snippet = re.sub(r'\s+', ' ', c.text[lo:lo + SAMPLE_CHARS]).strip()
                print(f"      {c.doc_id}: …{snippet}…")
        else:
            print()
        print()

    print("═" * 66)
    if found:
        print(f" 실재하는 주제 {len(found)}종: {', '.join(found)}")
        print(" → 이 주제로는 '거절 대신 연결'이 성립합니다(근거가 실재하므로).")
    else:
        print(" 실재하는 주제 없음 → 연결하지 말고 거절해야 합니다.")
    print("═" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
