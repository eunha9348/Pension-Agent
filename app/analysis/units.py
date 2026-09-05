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

    ⚠️ 정수로 반올림하지 않는다 — 소수점 첫째 자리까지만 표시한다.
    예전에는 정수 반올림(76.56 → "77만원")을 썼는데, 검증 대조 집합에
    원본 값만 있으면 시스템이 스스로 표시한 값이 '근거 없는 수치'로
    잡히는 사고가 반복됐다(2026-09-06, 억 단위 분리표기에서도 같은
    사고 재현). numeric_verifier._flatten_numbers가 이 함수를 그대로
    호출해 허용 집합을 만들므로 표기 자체는 안전하지만, 반올림 폭을
    줄이면 원본과의 오차가 작아져 그 계열 사고의 여지가 준다.
    """
    if manwon is None:
        return "—"
    try:
        m = float(manwon)
    except (TypeError, ValueError):
        # 호출 측이 숫자를 보장해야 하지만, 여기서마저 죽으면 답변 생성
        # 전체가 죽는다. 표시 전용 함수이므로 조용히 물러나는 편이 낫다.
        return "—"
    if m >= 10_000:
        eok, rest = divmod(m, 10_000)
        if abs(rest) < 0.05:
            return f"{int(eok):,}억원"
        return f"{int(eok):,}억 {_fmt1(rest)}만원"
    return f"{_fmt1(m)}만원"


def _fmt1(v: float) -> str:
    """소수점 첫째 자리까지, 딱 떨어지면 정수로("500.0" 아닌 "500")."""
    s = f"{v:,.1f}"
    return s[:-2] if s.endswith(".0") else s


# ── 금액 파싱 ────────────────────────────────────────────────

# "1억 2천만원", "5,000만원", "300만 원", "50000000원", "1.5억"
_AMOUNT_TOKEN = re.compile(
    r'(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(억|천만|백만|십만|만|천)?\s*원?'
)


def parse_amount_expressions(text: str) -> list[tuple[int, int, float]]:
    """문자열에서 **금액 표현 단위**로 끊어 [(시작, 끝, 만원)] 를 반환.

    ━━ 왜 '표현 단위'로 끊어야 하는가 ━━
    예전에는 문자열 안의 모든 금액 토큰을 그냥 더했다. 그래서
    "총급여 4000만원인데 연금저축에 600만원" 이 4,600만원 하나로 합쳐졌고,
    연금저축 납입액이 4,600만원으로 잡혔다. 한도(600만원)에 걸려 결과가
    우연히 맞는 경우가 있어 테스트도 통과했지만,
    "총급여 5000만원 + 연금저축 600만원" 에서는 소득이 5,600만원이 되어
    세액공제율 구간이 16.5% → 13.2% 로 뒤집힌다. 즉 **자신 있게 틀린 숫자**가 나온다.

    반면 "1억 2천만원"은 두 토큰이지만 하나의 금액이므로 합쳐야 한다.
    그래서 기준은 '공백만으로 이어져 있으면 같은 금액, 다른 글자가 끼면 다른 금액'이다.
    """
    if not text:
        return []

    groups: list[tuple[int, int, float]] = []
    cur_start = cur_end = -1
    cur_won = 0.0

    for m in _AMOUNT_TOKEN.finditer(text):
        raw, unit = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit:
            won = value * _UNIT_MULTIPLIER[unit]
        elif "원" in m.group(0):
            won = value                       # "50,000,000원"
        else:
            continue                          # 단위 없는 맨 숫자는 금액이 아니다

        # 앞 표현과 공백만으로 이어져 있으면 같은 금액으로 본다 ("1억 2천만원")
        if cur_end >= 0 and text[cur_end:m.start()].strip() == "":
            cur_won += won
            cur_end = m.end()
        else:
            if cur_end >= 0:
                groups.append((cur_start, cur_end, round(won_to_manwon(cur_won), 4)))
            cur_start, cur_end, cur_won = m.start(), m.end(), won

    if cur_end >= 0:
        groups.append((cur_start, cur_end, round(won_to_manwon(cur_won), 4)))
    return groups


def parse_amount_to_manwon(text: str) -> Optional[float]:
    """자연어 금액 표현 → 만원 단위 float. **첫 번째 금액 표현**을 반환한다.

    "1억"        → 10000
    "1억 2천만원" → 12000   (한 표현)
    "5,000만원"   → 5000
    "50,000,000원" → 5000
    "300만원"     → 300

    ⚠️ 여러 금액이 있으면 합치지 않는다. 합치면 서로 다른 항목의 금액이
       뒤섞인다 (위 parse_amount_expressions 주석 참조).
       어느 금액이 어느 항목인지는 호출 측이 문맥으로 골라야 한다.

    ⚠️ 단위가 전혀 없는 맨 숫자(예: "1500")는 **금액으로 해석하지 않는다.**
       연금 도메인에서 맨 숫자는 연차·나이일 가능성이 높아 오해석 위험이 크다.
    """
    exprs = parse_amount_expressions(text)
    return exprs[0][2] if exprs else None


# ── 연령·연차·기간 파싱 ──────────────────────────────────────

# ⚠️ '살'을 빠뜨리면 안 된다. 문서는 '세'로 쓰지만 사람은 '살'로 말한다.
#    "24살이고 연금 계획 좀"이 나이 미확인으로 처리돼, 연령별 차등과세가
#    걸린 질의에서 조건이 통째로 비었다(2026-08-29 실측).
#    '개월'은 나이가 아니므로 뒤에 붙는 경우를 배제한다.
_AGE = re.compile(r'(?:만\s*)?(\d{1,3})\s*(?:세|살)(?!\s*짜리\s*자녀)')
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
