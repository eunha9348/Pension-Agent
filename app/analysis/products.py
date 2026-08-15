"""근거 문서에서 판매 클래스·총보수 표를 뽑아 비교 후보를 만든다.

`총보수_비교` 계산함수는 candidates 리스트를 인자로 받는데, 그 값은
사용자가 주는 게 아니라 **문서에서 와야 한다.** 그 연결이 여기다.

━━ 왜 문서에서 뽑아야 하는가 ━━
총보수를 LLM이 기억으로 채우면 그 숫자는 근거가 없다. 표에서 파싱하면
근거 문서에 실재하는 값이므로 수치 대조 검증도 통과한다.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.core.coverage_pipeline import EvidenceChunk
from app.core.pension_calc_functions import (CLASS_ACCOUNT_REQUIREMENT,
                                             RESTRICTED_CLASSES)

_KNOWN_CLASSES = sorted(set(CLASS_ACCOUNT_REQUIREMENT) | set(RESTRICTED_CLASSES),
                        key=len, reverse=True)

# "C-P     | 0.5440%"  /  "C-Pe 0.4390 %"  /  "C-R | 연 0.5440%"
_ROW = re.compile(
    r'(?<![A-Za-z0-9-])(' + "|".join(re.escape(c) for c in _KNOWN_CLASSES) +
    r')(?![A-Za-z0-9-])[^\n%]{0,30}?(\d+\.\d+)\s*%')


def extract_class_expenses(evidence: Iterable[EvidenceChunk]) -> list[dict]:
    """근거 청크에서 (판매 클래스, 총보수) 후보를 수집.

    같은 클래스가 여러 문서에 나오면 먼저 나온 것을 쓰고 출처를 함께 남긴다.
    (문서마다 총보수가 다르면 상품이 다른 것이므로, 상위에서 엔티티로 걸러진다)
    """
    found: dict[str, dict] = {}
    for chunk in evidence:
        # 보수 표가 아닌 문맥에서 잡히는 것을 줄이기 위해 최소한의 신호를 요구
        if not any(k in chunk.text for k in ("보수", "수수료", "총보수")):
            continue
        for fund_class, rate in _ROW.findall(chunk.text):
            if fund_class in found:
                continue
            found[fund_class] = {
                "fund_class": fund_class,
                "total_expense": float(rate),
                "name": (chunk.entities or {}).get("product_name", ""),
                "source": chunk.doc_id,
            }
    return list(found.values())
