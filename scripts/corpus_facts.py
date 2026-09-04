"""상품 팩트 추출 진단 — 실물 코퍼스에서 6축이 실제로 잡히는지 눈으로 본다.

    python -m scripts.corpus_facts                  # data/corpus 원문 전수
    python -m scripts.corpus_facts --index          # 배포된 인덱스 기준
    python -m scripts.corpus_facts --doc R2_KR51    # 특정 문서만
    python -m scripts.corpus_facts --misses         # 놓친 줄만 자세히
    python -m scripts.corpus_facts --json out.json  # 기계 판독용 저장

━━ 왜 필요한가 ━━
`product_facts.py`의 패턴은 **실물 코퍼스를 보지 못한 채** 표준 투자설명서
표기를 근거로 작성했다. mock 코퍼스에는 위험등급·수익률·시장잔고·자산유형이
등장 횟수 0회라 로컬에서는 검증 자체가 불가능하다 — CLAUDE.md가 반복해서
경고하는 "실물에서만 보이는" 계열이다.

그래서 이 도구는 세 가지를 함께 보고한다:

  ① 축별 커버리지    — 158문서 중 몇 건에서 잡혔는가
  ② 패턴별 발화 횟수 — 어느 패턴이 실제로 일하고 어느 것이 죽어 있는가
  ③ **놓친 줄**      — 키워드는 있는데 값이 안 뽑힌 줄 (가장 중요)

③이 핵심이다. 이게 없으면 "패턴이 맞아서 0건"인지 "패턴이 틀려서 0건"인지
구별할 수 없고, 그러면 패턴을 감으로 고치게 된다. 결정론 계층의 오탐은
되돌릴 수 없으므로 감으로 넓혀서는 안 된다(CLAUDE.md).

corpus_health.py가 "문턱 미달로 지나간 구간까지" 보고하는 것과 같은 취지다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app.analysis.product_facts import (ALL_AXES, extract_product_facts,
                                        near_misses)
from app.analysis.products import extract_class_expenses


def _iter_corpus(doc_filter: str = "", corpus_dir: str = ""):
    """원문 zip에서 (doc_id, full_text)를 낸다.

    corpus_health.py와 같은 규약 — 기본은 data/corpus 이고, 비어 있으면
    mock으로 넘어가지 **않는다.** mock에는 이 축들이 등장 횟수 0회라
    "패턴이 틀렸다"와 "자료가 없다"를 구별할 수 없기 때문이다.
    """
    from app.config import REPO_ROOT
    from app.ingest.loader import corpus_files, iter_documents

    target = Path(corpus_dir or (REPO_ROOT / "data" / "corpus"))
    if not corpus_files(target):
        return
    for doc in iter_documents(target):
        if doc_filter and doc_filter not in doc.doc_id:
            continue
        yield doc.doc_id, doc.full_text


def _iter_index(doc_filter: str = ""):
    """배포된 인덱스의 청크를 문서 단위로 합쳐 낸다.

    ⚠️ 원문과 다를 수 있다 — 인덱스는 청킹·복원을 거친 결과다. 실제 서빙이
       보는 텍스트가 이쪽이므로, 배포 후 진단에는 --index 가 정확하다.
    """
    from app.ingest.store import get_store

    store = get_store(reload=True)
    merged: dict[str, list[str]] = defaultdict(list)
    for c in store.all_chunks():
        if doc_filter and doc_filter not in c.doc_id:
            continue
        merged[c.doc_id].append(c.text)
    for doc_id, parts in merged.items():
        yield doc_id, "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="상품 팩트 추출 진단")
    ap.add_argument("--index", action="store_true",
                    help="원문 대신 배포된 인덱스를 본다")
    ap.add_argument("--doc", default="", help="문서 ID 부분일치 필터")
    ap.add_argument("--misses", action="store_true",
                    help="놓친 줄(near-miss)을 문서별로 자세히 출력")
    ap.add_argument("--json", default="", help="결과를 JSON으로 저장")
    ap.add_argument("--max-miss", type=int, default=3,
                    help="문서당 출력할 놓친 줄 수 (기본 3)")
    ap.add_argument("--corpus", default="",
                    help="원문 디렉터리 (기본 data/corpus)")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="문서 앞부분 N자를 그대로 출력한다. 패턴이 실물 표기와 "
                         "어긋났을 때 **실제 형태를 눈으로 보기 위한** 것이다.")
    a = ap.parse_args(argv)

    docs = (list(_iter_index(a.doc)) if a.index
            else list(_iter_corpus(a.doc, a.corpus)))
    if not docs:
        where = "인덱스" if a.index else (a.corpus or "data/corpus")
        print(f"문서를 찾지 못했습니다 ({where}).")
        print("실물 자료가 있는 서버에서 실행하십시오 — mock 코퍼스에는 "
              "위험등급·수익률·시장잔고·자산유형이 등장 횟수 0회라\n"
              "'패턴이 틀렸다'와 '자료가 없다'를 구별할 수 없습니다.")
        return 1

    axis_cov = Counter()
    pattern_hits = Counter()
    conflicts: list[str] = []
    warnings: list[str] = []
    miss_lines: dict[str, dict[str, list[str]]] = {}
    class_cov = 0
    per_doc: list[dict] = []

    for doc_id, text in docs:
        facts = extract_product_facts(text, doc_id)
        for axis in facts.found_axes:
            axis_cov[axis] += 1
        for hit in (facts.risk_grade, facts.asset_class, facts.aum):
            if hit:
                pattern_hits[f"{hit.axis}/{hit.pattern}"] += 1
                if hit.warning:
                    warnings.append(f"{doc_id} [{hit.axis}] {hit.warning}")
        for hit in facts.returns:
            pattern_hits[f"{hit.axis}/{hit.pattern}"] += 1
        for axis, why in facts.conflicts.items():
            conflicts.append(f"{doc_id} [{axis}] {why}")

        # 판매클래스·총보수는 기존 추출기가 담당한다. 같은 화면에서 봐야
        # "6축 중 무엇이 비었는지"를 한눈에 알 수 있다.
        from app.core.coverage_pipeline import EvidenceChunk
        cls = extract_class_expenses([EvidenceChunk(doc_id=doc_id, score=1.0,
                                                    text=text)])
        if cls:
            class_cov += 1

        misses = near_misses(text, facts)
        if misses:
            miss_lines[doc_id] = misses

        per_doc.append({
            "doc_id": doc_id, "chars": len(text),
            "facts": facts.as_dict(),
            "classes": len(cls),
            "missing_axes": [x for x in ALL_AXES if x not in facts.found_axes],
        })

    n = len(docs)
    print(f"\n{'=' * 66}")
    print(f"  상품 팩트 추출 진단 — 문서 {n}건 "
          f"({'인덱스' if a.index else '원문'})")
    print(f"{'=' * 66}\n")

    # ── 0. 텍스트가 도달하긴 했는가 ──────────────────────────
    #
    # ⚠️ 이 블록이 없으면 "패턴이 틀렸다"와 "텍스트가 아예 없다"를 구별할 수
    #    없다. 실제로 첫 실전 진단에서 전 축 0/158이 나왔는데, 원인이 둘 중
    #    무엇인지 알 수 없어 한 번을 헛돌았다. 진단 도구가 원인을 좁히지
    #    못하면 그건 진단이 아니다.
    lengths = [len(t) for _d, t in docs]
    empty = [d for d, t in docs if len(t) < 50]
    total_chars = sum(lengths)
    print("── 텍스트 도달 확인 ──────────────────────────────────")
    print(f"  총 {total_chars:,}자 · 문서당 평균 {total_chars // max(n, 1):,}자 "
          f"· 최소 {min(lengths) if lengths else 0:,} / 최대 "
          f"{max(lengths) if lengths else 0:,}")
    if empty:
        print(f"  ⚠️ 본문이 50자 미만인 문서 {len(empty)}건: "
              f"{', '.join(empty[:5])}{' …' if len(empty) > 5 else ''}")
        print("     → 추출 이전에 **판독 단계**를 먼저 확인하십시오 "
              "(python -m app.ingest.check_corpus)")
    print()

    # ── 0-b. 축 키워드가 코퍼스에 실재하는가 ─────────────────
    #
    # 값이 안 뽑힌 이유가 "패턴이 틀림"인지 "그 말 자체가 문서에 없음"인지
    # 가른다. 키워드가 0건이면 정규식을 아무리 고쳐도 소용없다.
    probe = ["총보수", "보수", "수수료", "위험등급", "등급", "수익률",
             "설정액", "순자산", "시장잔고", "집합투자기구", "종류", "클래스"]
    joined = "\n".join(t for _d, t in docs)
    print("── 축 키워드 실재 여부 (코퍼스 전체 등장 횟수) ────────")
    for kw in probe:
        cnt = joined.count(kw)
        ndocs = sum(1 for _d, t in docs if kw in t)
        mark = "  " if cnt else "❌"
        print(f"  {mark} {kw:10s} {cnt:6,}회  ({ndocs}/{n} 문서)")
    print()

    if a.sample:
        print("── 원문 표본 (패턴을 실물에 맞추기 위한 것) ───────────")
        for doc_id, text in docs[:3]:
            print(f"\n  [{doc_id}] {len(text):,}자")
            for line in text[:a.sample].splitlines():
                print(f"    | {line[:150]}")
        print()

    print("── 축별 커버리지 ──────────────────────────────────────")
    for axis in ALL_AXES:
        c = axis_cov[axis]
        bar = "█" * int(round(c / max(n, 1) * 30))
        print(f"  {axis:8s} {c:4d}/{n:<4d} ({c / max(n, 1):5.1%}) {bar}")
    print(f"  {'판매클래스·총보수':8s} {class_cov:4d}/{n:<4d} "
          f"({class_cov / max(n, 1):5.1%})   ← 기존 추출기(products.py)\n")

    print("── 패턴별 발화 ────────────────────────────────────────")
    if pattern_hits:
        for key, cnt in pattern_hits.most_common():
            print(f"  {cnt:5d}  {key}")
    else:
        print("  (발화한 패턴 없음 — 패턴이 실물 표기와 어긋났을 가능성이 큽니다)")
    print()

    if warnings:
        print("── ⚠️ 표준과 어긋난 값 ───────────────────────────────")
        for w in warnings[:15]:
            print(f"  {w}")
        if len(warnings) > 15:
            print(f"  … 외 {len(warnings) - 15}건")
        print()

    if conflicts:
        print("── 값이 갈려 확정하지 않은 축 ─────────────────────────")
        for c in conflicts[:15]:
            print(f"  {c}")
        if len(conflicts) > 15:
            print(f"  … 외 {len(conflicts) - 15}건")
        print()

    print("── ★ 놓친 줄 (키워드는 있는데 값이 안 뽑힘) ────────────")
    print("   이 줄들이 패턴을 고칠 근거입니다. 형태를 보고 정규식을 맞춥니다.\n")
    if not miss_lines:
        # ⚠️ "놓친 줄이 없다"에는 정반대의 두 사연이 있다. 하나로 뭉뚱그리면
        #    진단이 거짓말을 한다 — 실제로 전 축 0건인 상태에서 "전부 값을
        #    뽑았습니다"라고 출력해 원인 파악을 한 번 헛돌게 만들었다.
        if any(axis_cov[x] for x in ALL_AXES):
            print("  없음 — 키워드가 있는 문서에서는 전부 값을 뽑았습니다.\n")
        else:
            print("  없음. 다만 **뽑힌 값도 0건**입니다 — 놓친 줄이 없는 것이\n"
                  "  아니라 **축 키워드 자체가 문서에 없는** 것입니다.\n"
                  "  위 '축 키워드 실재 여부'를 보십시오. 전부 0회라면 정규식을\n"
                  "  고쳐도 소용없고, 판독 단계나 대상 문서를 의심해야 합니다.\n")
    else:
        shown = 0
        for doc_id, misses in miss_lines.items():
            if not a.misses and shown >= 12:
                print(f"  … 외 {len(miss_lines) - shown}개 문서 "
                      f"(--misses 로 전체 확인)\n")
                break
            print(f"  [{doc_id}]")
            for axis, lines in misses.items():
                for ln in lines[:a.max_miss]:
                    print(f"    {axis:8s} | {ln}")
            shown += 1
        print()

    if a.json:
        Path(a.json).write_text(json.dumps({
            "doc_count": n,
            "axis_coverage": dict(axis_cov),
            "class_coverage": class_cov,
            "pattern_hits": dict(pattern_hits),
            "conflicts": conflicts,
            "warnings": warnings,
            "near_misses": miss_lines,
            "documents": per_doc,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON 저장: {a.json}\n")

    # 커버리지가 0인 축이 있으면 종료코드로 알린다(CI·자동 점검용).
    dead = [x for x in ALL_AXES if axis_cov[x] == 0]
    if dead:
        print(f"⚠️ 한 건도 못 뽑은 축: {dead}")
        print("   → 위 '놓친 줄'을 보고 product_facts.py의 패턴을 조정하십시오.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
