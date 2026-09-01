"""코퍼스 사전 점검 — 인덱스를 만들기 **전에** 무엇이 들어가는지 확인한다.

    python -m app.ingest.check_corpus
    python -m app.ingest.check_corpus data/corpus

━━ 왜 필요한가 ━━
"파일을 넣었다"와 "그 파일이 검색된다"는 다른 얘기다. 스캔본 PDF처럼
텍스트 레이어가 없는 파일은 넣어도 내용이 0자다. 그걸 모르고 평가를 받으면
근거 없이 거절만 하는 에이전트가 된다.

이 명령은 파일별로 **실제로 몇 글자를 읽어냈는지**까지 보여준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.ingest.loader import (NEEDS_CONVERSION, corpus_files, is_ingestible,
                               load_file)
from app.ingest.ocr_repair import repair_documents

DEFAULT = REPO_ROOT / "data" / "corpus"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    target = Path(argv[0]) if argv else DEFAULT

    print("═" * 72)
    print(f" 코퍼스 점검: {target}")
    print("═" * 72)

    if not target.exists():
        print(f"\n❌ 디렉터리가 없습니다. 먼저 만드십시오:  mkdir -p {target}")
        return 1

    files = corpus_files(target)
    if not files:
        print("\n❌ 파일이 없습니다.")
        return 1

    ok, empty, unsupported = [], [], []
    total_chars = 0
    loaded = []          # OCR 복원 점검용 — 아래에서 코퍼스 전체로 대조한다

    for path in files:
        rel = path.relative_to(target)
        if not is_ingestible(path):
            reason = NEEDS_CONVERSION.get(path.suffix.lower(), "판독기 없음")
            unsupported.append((rel, reason))
            print(f"  ❌ {str(rel):45s} {reason}")
            continue

        doc = load_file(path)
        chars = len(doc.full_text.strip())
        total_chars += chars
        if chars == 0:
            empty.append((rel, "; ".join(doc.warnings) or "텍스트 0자"))
            print(f"  ⚠️  {str(rel):45s} 텍스트 0자 — "
                  f"{'; '.join(doc.warnings) or '내용 없음'}")
        else:
            ok.append((rel, chars))
            loaded.append(doc)
            print(f"  ✅ {str(rel):45s} {doc.page_count:4d}쪽 {chars:>8,}자 "
                  f"[{doc.layout}]")

    # ── OCR 판독 실패 구간 점검 ─────────────────────────────────
    # 인덱스를 만들기 전에 "원문이 얼마나 깨져 있고 그중 얼마나 복원되는지"를
    # 먼저 본다. 여기서 복원율이 낮으면 인용문에 (판독불가)가 많이 나간다.
    if loaded:
        repair = repair_documents(loaded)     # loaded는 사본이므로 안전
        if repair.runs_found:
            print("\n" + "─" * 72)
            print(f" [OCR 판독 실패] {repair.summary()}")
            for s_ in repair.samples:
                print(f"   · {s_}")
            if repair.runs_masked:
                print(f"   ⚠ 복원 실패 {repair.runs_masked}건은 '(판독불가)'로 "
                      f"표시됩니다. 원문 OCR이 깨진 것이며, '?'가 인용문에 "
                      f"그대로 나가지는 않습니다.")

    print("\n" + "─" * 72)
    print(f" 전체 {len(files)}건 → 판독 성공 {len(ok)}건 · "
          f"내용 없음 {len(empty)}건 · 미지원 {len(unsupported)}건")
    print(f" 총 텍스트 {total_chars:,}자")
    print("─" * 72)

    if unsupported:
        print("\n[미지원 형식 처리 방법]")
        print("  · 한글(.hwp) / 워드(.docx) → PDF 또는 텍스트로 변환")
        print("  · 낱장 이미지(.jpg/.png)   → OCR 텍스트 파일이 함께 있어야 함")

    if empty:
        print("\n[텍스트가 0자인 파일]")
        print("  스캔본 PDF일 가능성이 높습니다. 텍스트 레이어가 없으면")
        print("  검색·인용이 불가능하므로, OCR 결과 파일을 대신 넣으십시오.")

    if not ok:
        print("\n❌ 판독되는 문서가 하나도 없습니다. 이 상태로 인덱스를 만들면")
        print("   모든 질의에 '근거 없음'으로 거절하게 됩니다.")
        return 1

    print("\n다음 단계:  python -m app.ingest.build_index")
    return 0


if __name__ == "__main__":
    sys.exit(main())
