"""zip 구조 덤프 CLI — 실제 문서를 받으면 **가장 먼저** 이걸 돌린다.

    python -m app.ingest.inspect_zip data/corpus/doc39.zip
    python -m app.ingest.inspect_zip data/corpus            # 디렉터리 전체

파싱하지 않고 목록만 본다. 감지된 레이아웃이 zip_parser의 규칙에 걸리는지
확인하는 것이 목적이다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.ingest.zip_parser import inspect, parse_zip


def _dump(path: Path) -> None:
    info = inspect(path)
    print("═" * 62)
    print(f" {path.name}")
    print("═" * 62)
    print(json.dumps(info, ensure_ascii=False, indent=2))

    if "error" in info:
        return

    doc = parse_zip(path)
    print(f"\n → 페이지 {doc.page_count}건 · 이미지 {len(doc.image_entries)}건 "
          f"· 레이아웃 {doc.layout}")
    for w in doc.warnings:
        print(f"   ⚠ {w}")
    if doc.pages:
        head = doc.pages[0]
        print(f"\n [1쪽 미리보기 · {head.source_entry}]")
        print("   " + "\n   ".join(head.text.strip().splitlines()[:8]))
    print()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    target = Path(sys.argv[1])
    if target.is_dir():
        zips = sorted(target.glob("*.zip"))
        if not zips:
            print(f"{target}에 zip이 없습니다.")
            return 1
        for z in zips:
            _dump(z)
    else:
        _dump(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
