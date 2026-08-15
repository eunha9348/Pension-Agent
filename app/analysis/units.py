"""금액·연령·연차 파싱 및 단위 변환.

━━ 이 파일이 존재하는 이유 ━━
계산함수 15종은 전부 **만원 단위**다. 사용자 질의는 "5천만원", "1억",
"50,000,000원"처럼 제각각이다. 변환을 각 호출부에 흩어 놓으면
언젠가 1만배 오차가 난다 — 그래서 경계 변환을 이 파일 하나로 모은다.

규칙: 입출력 경계에서만 변환하고, 내부는 전부 만원으로 통일한다.
"""

from __future__ import annotations

import re
from typing import Optional

# 한글 수 단위 → 배수(원 기준)
_UNIT_MULTIPLIER = {
    "억": 100_000_000,
    "천만": 10_000_000,
    "백만": 1_000_000,
    "십만": 100_000,
    "만": 10_000,
    "천": 1_000,
}

MANWON = 10_000  # 1만원 = 10,000원


def won_to_manwon(won: float) -> float:
    """원 → 만원."""
    return won / MANWON


def manwon_to_won(manwon: float) -> float:
    """만원 → 원."""
    return manwon * MANWON


def format_manwon(manwon: float) -> str:
    """만원 단위 수치를 사람이 읽는 한국어 금액 표기로.

    답변 본문에 쓰는 표기이므로 계산 결과를 바꾸지 않는다(표시 전용).
    """
    if manwon is None:
        return "—"
    m = float(manwon)
    if m >= 10_000:
        eok, rest = divmod(m, 10_000)
        if abs(rest) < 0.5:
            return f"{int(eok):,}억원"
        return f"{int(eok):,}억 {rest:,.0f}만원"
    return f"{m:,.0f}만원"


# ── 금액 파싱 ────────────────────────────────────────────────

# "1억 2천만원", "5,000만원", "300만 원", "50000000원", "1.5억"
_AMOUNT_TOKEN = re.compile(
    r'(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(억|천만|백만|십만|만|천)?\s*원?'
)


def parse_amount_to_manwon(text: str) -> Optional[float]:
    """자연어 금액 표현 → 만원 단위 float.

    "1억"        → 10000
    "1억 2천만원" → 12000
    "5,000만원"   → 5000
    "50,000,000원" → 5000
    "300만원"     → 300

    ⚠️ 단위가 전혀 없는 맨 숫자(예: "1500")는 **원 단위로 해석하지 않는다.**
       연금 도메인에서 맨 숫자는 대개 만원 단위 관용 표기이거나
       연차·나이일 가능성이 높아, 오해석 위험이 크기 때문에 None을 준다.
       호출 측이 문맥(연차/나이/금액)을 알고 있을 때만 직접 지정할 것.
    """
    if not text:
        return None

    total_won = 0.0
    matched_any = False
    for m in _AMOUNT_TOKEN.finditer(text):
        raw, unit = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit:
            total_won += value * _UNIT_MULTIPLIER[unit]
            matched_any = True
        elif "원" in m.group(0):
            # "50,000,000원" 처럼 단위어 없이 '원'만 붙은 경우
            total_won += value
            matched_any = True

    if not matched_any:
        return None
    return round(won_to_manwon(total_won), 4)


# ── 연령·연차·기간 파싱 ──────────────────────────────────────

_AGE = re.compile(r'(?:만\s*)?(\d{1,3})\s*세')
_PENSION_YEAR = re.compile(r'(\d{1,2})\s*년\s*차')
_SERVICE_YEARS = re.compile(r'(?:근속|재직|다니|근무)\D{0,6}(\d{1,2})\s*년')
_PLAIN_YEARS = re.compile(r'(\d{1,2})\s*년(?!\s*차)')


def parse_age(text: str) -> Optional[int]:
    m = _AGE.search(text or "")
    return int(m.group(1)) if m else None


def parse_pension_year(text: str) -> Optional[int]:
    """'연금수령연차' 값. "10년차" → 10."""
    m = _PENSION_YEAR.search(text or "")
    return int(m.group(1)) if m else None


def parse_service_years(text: str) -> Optional[int]:
    """근속연수. "25년 근무", "근속 25년" 모두 인식."""
    t = text or ""
    m = _SERVICE_YEARS.search(t)
    if m:
        return int(m.group(1))
    # "25년 다녔는데" 처럼 앞에 붙는 형태
    m = re.search(r'(\d{1,2})\s*년\s*(?:간\s*)?(?:다|근무|재직|일)', t)
    return int(m.group(1)) if m else None


def parse_plain_years(text: str) -> Optional[int]:
    """연차 표기가 아닌 일반 '○년' (경과 연수 등)."""
    m = _PLAIN_YEARS.search(text or "")
    return int(m.group(1)) if m else None


# ── 세율 파싱 ────────────────────────────────────────────────

_RATE = re.compile(r'(\d{1,2}(?:\.\d+)?)\s*%')


def parse_rate(text: str) -> Optional[float]:
    """"16.5%" → 0.165. 소수 비율로 정규화해 반환."""
    m = _RATE.search(text or "")
    return round(float(m.group(1)) / 100, 6) if m else None
