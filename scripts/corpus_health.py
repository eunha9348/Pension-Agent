"""코퍼스 텍스트 건전성 진단 — 실물에서만 보이는 결함을 눈으로 본다.

    python -m scripts.corpus_health                 # data/corpus 원문
    python -m scripts.corpus_health --index         # 배포된 인덱스(청크)
    python -m scripts.corpus_health --doc doc20     # 특정 문서만

━━ 왜 필요한가 ━━
mock 코퍼스는 우리가 만든 깨끗한 텍스트라, 실물 자료의 판독 결함이
**하나도 재현되지 않는다.** 실제로 두 결함이 실물에서만 드러났다:

  · OCR 판독 실패 '?'   — "납입금액부터 ?????????"
  · 겹쳐 그려진 글자     — "퇴퇴퇴직직직연연연" (2배·3배)

둘 다 자동 복원 로직이 있지만, **문턱을 넘지 못하면 조용히 통과한다.**
그래서 이 도구는 고친 것뿐 아니라 **문턱에 미달해 놓친 것(near-miss)**까지
같이 보고한다. 문턱을 조정하려면 근거가 있어야 하기 때문이다 —
결정론 계층의 오탐은 되돌릴 수 없으므로 감으로 낮춰서는 안 된다.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from app.config import REPO_ROOT
from app.ingest.loader import (_REPEAT_DETECT, corpus_files, detect_fold,
                               iter_documents, repair_doubled_glyphs)
from app.ingest.ocr_repair import looks_garbled

# 문턱과 무관하게 "같은 한글이 k회 연속 반복되는 묶음이 n번 이어지는" 구간을
# 전부 센다. 복원 문턱은 4회이므로, 2~3회 구간이 많으면 놓치고 있다는 뜻이다.
_NEAR = {k: re.compile(rf'(?:({_REPEAT_DETECT})\1{{{k - 1}}}){{2,}}') for k in (3, 2)}
_QMARK = re.compile(r'\?{2,}')


def _scan(label: str, text: str, acc: dict) -> None:
    acc["texts"] += 1
    acc["chars"] += len(text)

    if looks_garbled(text):
        acc["ocr_texts"] += 1
        for m in _QMARK.finditer(text):
            acc["ocr_runs"] += 1
            if len(acc["ocr_samples"]) < 8:
                s, e = m.start(), m.end()
                acc["ocr_samples"].append(
                    f"{label}: …{text[max(0, s - 30):s]}"
                    f"[{m.group(0)}]{text[e:e + 30]}…".replace("\n", " "))

    fold = detect_fold(text)
    if fold:
        acc["fold_texts"] += 1
        acc["folds"][fold] += 1
        fixed = repair_doubled_glyphs(text)
        if len(acc["fold_samples"]) < 8:
            m = _NEAR[fold].search(text)
            if m:
                s = max(0, m.start() - 20)
                acc["fold_samples"].append(
                    f"{label} ({fold}배): …{text[s:m.end() + 20]}…".replace("\n", " "))
        # 복원 뒤에도 반복이 남았는가 (배수가 섞여 있으면 남는다)
        if detect_fold(fixed):
            acc["fold_residual"] += 1
    else:
        # ── 문턱 미달 — 복원 로직이 그냥 지나친 구간 ──────────
        for k in (3, 2):
            hits = _NEAR[k].findall(text)
            if hits:
                acc["near"][k] += len(hits)
                if len(acc["near_samples"]) < 8:
                    m = _NEAR[k].search(text)
                    s = max(0, m.start() - 20)
                    acc["near_samples"].append(
                        f"{label} ({k}배 문턱미달): "
                        f"…{text[s:m.end() + 20]}…".replace("\n", " "))
                break


def _report(acc: dict) -> None:
    print("\n" + "─" * 72)
    print(f" 검사 대상 {acc['texts']}건 · 총 {acc['chars']:,}자")
    print("─" * 72)

    print(f"\n[OCR 판독 실패 '?']  오염 {acc['ocr_texts']}건 · 구간 {acc['ocr_runs']}개")
    for s in acc["ocr_samples"]:
        print(f"   · {s}")

    print(f"\n[겹쳐 그려진 글자]  오염 {acc['fold_texts']}건 "
          f"(2배 {acc['folds'][2]}건 · 3배 {acc['folds'][3]}건)")
    for s in acc["fold_samples"]:
        print(f"   · {s}")
    if acc["fold_residual"]:
        print(f"   ⚠ 복원 후에도 반복이 남은 건 {acc['fold_residual']}건 "
              f"— 한 텍스트에 배수가 섞여 있을 수 있습니다")

    near_total = acc["near"][2] + acc["near"][3]
    print(f"\n[문턱 미달 — 복원되지 않고 지나간 구간]  "
          f"2배 {acc['near'][2]}개 · 3배 {acc['near'][3]}개")
    for s in acc["near_samples"]:
        print(f"   · {s}")
    if near_total:
        print("   ※ 이 구간들은 '4회 연속' 문턱에 미달해 손대지 않은 것입니다.")
        print("     실제 깨짐으로 보이면 문턱을 낮출 근거가 되고,")
        print("     정상 표기로 보이면 문턱이 옳다는 근거가 됩니다.")

    ok = not (acc["ocr_runs"] or acc["fold_texts"] or near_total)
    print("\n" + ("✅ 판독 결함이 발견되지 않았습니다."
                  if ok else "⚠️  위 내용을 확인하십시오."))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="코퍼스 텍스트 건전성 진단")
    ap.add_argument("--index", action="store_true",
                    help="원문 대신 배포된 인덱스의 청크를 검사")
    ap.add_argument("--corpus", default=str(REPO_ROOT / "data" / "corpus"))
    ap.add_argument("--doc", help="이 문서 ID만 검사")
    args = ap.parse_args(argv)

    acc = {"texts": 0, "chars": 0, "ocr_texts": 0, "ocr_runs": 0,
           "ocr_samples": [], "fold_texts": 0, "fold_residual": 0,
           "folds": Counter(), "fold_samples": [],
           "near": Counter(), "near_samples": []}

    if args.index:
        from app.ingest.store import get_store
        store = get_store()
        print(f"인덱스 검사 — 청크 {len(store.chunks)}건 "
              f"(코퍼스: {store.corpus_kind})")
        if store.corpus_kind == "mock":
            print("⚠️  mock 코퍼스입니다 — 실물 자료의 판독 결함은 재현되지 않습니다.")
        for c in store.all_chunks():
            if args.doc and c.doc_id != args.doc:
                continue
            _scan(c.chunk_id, c.text, acc)
    else:
        target = Path(args.corpus)
        if not corpus_files(target):
            print(f"❌ {target} 가 비어 있습니다.")
            return 1
        print(f"원문 검사 — {target}")
        for doc in iter_documents(target):
            if args.doc and doc.doc_id != args.doc:
                continue
            for pg in doc.pages:
                _scan(f"{doc.doc_id} p{pg.page_no}", pg.text, acc)

    _report(acc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
