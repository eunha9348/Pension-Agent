"""상품 팩트 추출 — 투자설명서에서 6축을 색인 시점에 전수 파싱한다.

━━ 왜 이 모듈이 필요한가 (2026-09-04) ━━
과제 안내가 정의한 실적배당형 상품 데이터의 축은 여섯이다:

    상품분류(자산유형) · 위험등급 · 판매클래스 · 총보수 · 수익률 · 시장잔고

그런데 구현돼 있던 것은 **판매클래스와 총보수 둘뿐**이었고(products.py),
나머지 넷은 추출기도 계산함수도 없었다. 위험등급은 trap_rules D2가
"운용사 간 직접 비교 주의"라는 **경고 문구만** 끼워 넣을 뿐, 정작 등급
값 자체를 문서에서 뽑아 대조하는 장치가 없었다.

━━ 왜 색인 시점인가 (기존 products.py와의 차이) ━━
`products.py::extract_class_expenses`는 **검색된 근거 청크만** 본다.
그래서 검색이 표 청크를 못 건지면 총보수가 통째로 사라진다. 검색 순위에
사실의 존재 여부가 걸려 있는 셈이다.

이 모듈은 색인 시점에 **문서 전문(full_text)**을 훑어 doc_meta에 남긴다.
검색이 무엇을 건지든 팩트는 사라지지 않는다. 검색을 대체하는 것이
아니라, 하이브리드 검색이 찾아온 결과 위에 정확한 수치를 얹는 것이다.

━━ 왜 LLM/MRC가 아니라 정규식인가 ━━
투자설명서는 【제목】 + 파이프 구분 표라는 매우 규칙적인 형태다.
"이 상품의 위험등급이 몇 등급인가"는 **판단**이지 생성이 아니므로
CLAUDE.md의 "판단은 코드, 문장은 LLM" 원칙이 그대로 적용된다.
span 추출 모델(MRC)은 확률적이라 틀린 구간을 높은 확신으로 반환해도
결정론적으로 기각할 방법이 없고, torch 모델이 하나 더 상주한다.

━━ 지키는 규칙 셋 ━━
① **원문 스니펫을 반드시 함께 남긴다.** 값만 남기면 인용도 검증도 못 한다.
   수치 검증기가 이 스니펫을 근거로 삼아야 답변의 숫자가 통과한다.
② **후보가 갈리면 값을 비우고 conflict로 남긴다.** 억지로 하나를 고르면
   그 순간 날조다(ocr_repair의 "후보가 갈리면 복원하지 않는다"와 같다).
③ **패턴 이름을 값에 붙인다.** 실물 코퍼스를 보지 못한 채 쓴 패턴이므로,
   어느 패턴이 실제로 발화하는지 알아야 근거 있게 추릴 수 있다
   (scripts/corpus_facts.py가 이 이름으로 보고한다).

⚠️ 위험등급 방향에 주의할 것 — **1등급이 가장 위험하다.**
   "위험등급이 낮은 상품"은 숫자가 큰 쪽이다. 그래서 숫자만 남기지 않고
   원문 표기(label)를 함께 싣는다. "1등급"만 보고 안전하다고 서술하는
   사고를 막는 유일한 방법은 "1등급(매우 높은 위험)" 원문을 보여주는 것이다.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# ── 축 이름 (진단·저장 키로 공유한다) ──────────────────────────
AXIS_ASSET_CLASS = "상품분류"
AXIS_RISK_GRADE = "위험등급"
AXIS_RETURNS = "수익률"
AXIS_AUM = "시장잔고"
AXIS_GUARANTEED_RATE = "확정금리"

ALL_AXES = (AXIS_ASSET_CLASS, AXIS_RISK_GRADE, AXIS_RETURNS, AXIS_AUM,
            AXIS_GUARANTEED_RATE)

# 금융투자협회 6단계 위험등급의 표준 의미.
# 원문 표기가 이와 어긋나면(예: "1등급(매우 낮은 위험)") 구형 5단계이거나
# OCR 오류이므로, 값을 버리지는 않되 **경고를 남긴다.**
_RISK_STANDARD: dict[int, str] = {
    1: "매우 높은 위험",
    2: "높은 위험",
    3: "다소 높은 위험",
    4: "보통 위험",
    5: "낮은 위험",
    6: "매우 낮은 위험",
}

# 상품분류(자산유형) — 자본시장법상 집합투자기구 종류 + 실무 표기.
# 긴 것부터 봐야 "혼합채권형"이 "채권형"으로 잘리지 않는다.
_ASSET_CLASSES: tuple[str, ...] = (
    "혼합자산", "혼합채권형", "혼합주식형", "재간접형", "재간접",
    "단기금융", "파생형", "부동산", "특별자산", "실물자산",
    "주식형", "채권형", "증권형", "MMF", "TDF",
)


@dataclass
class FactHit:
    """축 하나의 추출 결과. 값과 **근거**를 함께 들고 다닌다."""

    axis: str
    value: object                 # 숫자 또는 문자열 (축마다 다름)
    label: str = ""               # 원문 표기 그대로 ("3등급(다소 높은 위험)")
    snippet: str = ""             # 대조·인용용 원문 한 줄
    pattern: str = ""             # 어느 패턴이 잡았는지 (진단용)
    warning: str = ""             # 표준과 어긋남 등

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductFacts:
    """문서 1건의 상품 팩트. doc_meta["product_facts"]에 그대로 실린다."""

    doc_id: str = ""
    asset_class: Optional[FactHit] = None
    risk_grade: Optional[FactHit] = None
    aum: Optional[FactHit] = None
    returns: list[FactHit] = field(default_factory=list)
    guaranteed_rate: Optional[FactHit] = None
    # 값이 갈려서 채우지 못한 축과 그 사유. 조용히 비우면 "문서에 없음"과
    # "여러 값이 충돌함"을 구별할 수 없다.
    conflicts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        out: dict = {"doc_id": self.doc_id}
        for key, hit in (("asset_class", self.asset_class),
                         ("risk_grade", self.risk_grade),
                         ("aum", self.aum),
                         ("guaranteed_rate", self.guaranteed_rate)):
            if hit is not None:
                out[key] = hit.as_dict()
        if self.returns:
            out["returns"] = [h.as_dict() for h in self.returns]
        if self.conflicts:
            out["conflicts"] = dict(self.conflicts)
        return out

    @property
    def found_axes(self) -> list[str]:
        axes = []
        if self.asset_class:     axes.append(AXIS_ASSET_CLASS)
        if self.risk_grade:      axes.append(AXIS_RISK_GRADE)
        if self.returns:         axes.append(AXIS_RETURNS)
        if self.aum:             axes.append(AXIS_AUM)
        if self.guaranteed_rate: axes.append(AXIS_GUARANTEED_RATE)
        return axes


# ════════════════════════════════════════════════════════════════
# 공통 도구
# ════════════════════════════════════════════════════════════════

def _line_of(text: str, pos: int, width: int = 160) -> str:
    """매칭 위치가 속한 줄을 잘라낸다 — 인용·대조에 쓸 원문이다."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end < 0:
        end = len(text)
    line = text[start:end].strip()
    return line[:width]


def _pick_one(hits: list[FactHit], axis: str,
              facts: ProductFacts) -> Optional[FactHit]:
    """같은 축의 후보 중 하나를 고른다. **갈리면 고르지 않는다.**

    투자설명서는 같은 값을 여러 번 반복하므로 중복 자체는 정상이다.
    문제는 서로 **다른 값**이 나오는 경우인데, 그때 하나를 고르면
    근거 없는 단정이 된다. 비우고 사유를 남긴다.
    """
    if not hits:
        return None
    distinct = {h.value for h in hits}
    if len(distinct) > 1:
        facts.conflicts[axis] = (
            f"서로 다른 값 {sorted(map(str, distinct))} 가 발견돼 확정하지 "
            f"않음 (한 문서에 여러 상품이 실렸을 수 있음)")
        return None
    return hits[0]


# ════════════════════════════════════════════════════════════════
# 축 1 · 위험등급
# ════════════════════════════════════════════════════════════════
#
# ⚠️ 아래 패턴은 실물 코퍼스를 보지 못한 채 표준 투자설명서 표기를 근거로
#    작성했다. scripts/corpus_facts.py 가 어느 패턴이 발화했는지와
#    **키워드는 있는데 안 잡힌 줄**을 함께 보고하므로, 실측 후 추린다.

_RISK_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # ★ 최우선 — 펀드가 **스스로를 몇 등급으로 분류했는지** 밝히는 문장.
    #
    # 실물 실측(158문서): "…실제 수익률 변동성을 감안하여 3등급으로
    # 분류하였습니다." 가 표준 문형이다. 이 문장은 **그 펀드 자신의 등급**만
    # 말하므로 모호함이 없다.
    #
    # ⚠️ 이 패턴을 맨 앞에 두는 것이 핵심이다. 투자설명서에는 1~6등급을
    #    전부 나열한 **등급 체계 설명표**가 함께 실리는데, 아래 라벨 패턴이
    #    그것까지 긁어 서로 다른 값이 잡히고 결국 '충돌'로 버려졌다.
    #    실측에서 35건이 그렇게 사라졌다(R2_KR5110501016 → ['4','5','6'] 등).
    ("자기분류", re.compile(r'([1-6])\s*등급\s*으로\s*분류()')),
    # "투자위험등급" 표제 **다음 줄**에 등급이 오는 형태.
    # 실물: "투자위험등급\n1등급[매우 높은 위험]"
    # ⚠️ 줄바꿈을 허용해야 한다 — [^\n]으로 막으면 이 형태를 통째로 놓친다.
    ("표제_다음줄", re.compile(
        r'(?:투자)?위험\s*등급\s*[\r\n]+\s*제?\s*([1-6])\s*등급\s*'
        r'[\[(（]\s*([^\])）\n]{2,20}?)\s*[\])）]')),
    # "위험등급: 3등급(다소 높은 위험)" / "투자위험등급 제3등급 [다소 높은 위험]"
    ("등급_괄호라벨", re.compile(
        r'(?:투자)?위험\s*등급[^\n]{0,20}?제?\s*([1-6])\s*등급\s*'
        r'[\[(（]\s*([^\])）\n]{2,20}?)\s*[\])）]')),
    # "위험등급 | 3등급"  (표 형태)
    ("표_등급", re.compile(
        r'(?:투자)?위험\s*등급\s*[|:｜]\s*제?\s*([1-6])\s*등급()')),
    # "위험등급: 3등급" / "위험등급은 3등급입니다"
    ("등급_평문", re.compile(
        r'(?:투자)?위험\s*등급[^\n]{0,20}?제?\s*([1-6])\s*등급()')),
    # "3등급(다소 높은 위험)" — '위험등급' 표제 없이 등급만 있는 경우.
    # 표제어가 없으므로 **괄호 라벨이 반드시 있어야** 채택한다
    # (연 3등급·신용등급 등 다른 등급과 섞이는 것을 막는다).
    ("라벨만", re.compile(
        r'제?\s*([1-6])\s*등급\s*[\[(（]\s*((?:매우\s*)?(?:높은|낮은|보통)'
        r'[^\])）\n]{0,10}위험)\s*[\])）]')),
)


def extract_risk_grade(text: str, facts: ProductFacts) -> Optional[FactHit]:
    hits: list[FactHit] = []
    for name, pat in _RISK_PATTERNS:
        for m in pat.finditer(text):
            grade = int(m.group(1))
            label_raw = (m.group(2) or "").strip()
            label = f"{grade}등급" + (f"({label_raw})" if label_raw else "")

            # 표준 방향과 어긋나는지 확인한다. 값을 버리지는 않는다 —
            # 구형 5단계 문서이거나 OCR 오류일 수 있고, 둘 다 "틀렸다"고
            # 단정할 근거가 없기 때문이다. 대신 반드시 눈에 보이게 남긴다.
            warning = ""
            if label_raw:
                expected = _RISK_STANDARD.get(grade, "")
                norm = re.sub(r'\s+', '', label_raw)
                if expected and re.sub(r'\s+', '', expected) != norm:
                    warning = (f"표준 6단계에서 {grade}등급은 '{expected}'인데 "
                               f"원문은 '{label_raw}' — 구형 등급체계이거나 "
                               f"판독 오류일 수 있음")
            hits.append(FactHit(
                axis=AXIS_RISK_GRADE, value=grade, label=label,
                snippet=_line_of(text, m.start()), pattern=name,
                warning=warning))
        if hits:
            # 앞선 패턴일수록 근거가 강하다(표제어 + 라벨). 하나라도 잡히면
            # 뒤의 느슨한 패턴은 보지 않는다 — 느슨한 쪽이 끼어들면
            # 멀쩡한 값이 '충돌'로 버려진다.
            break
    return _pick_one(hits, AXIS_RISK_GRADE, facts)


# ════════════════════════════════════════════════════════════════
# 축 2 · 상품분류(자산유형)
# ════════════════════════════════════════════════════════════════

_ASSET_ALT = "|".join(re.escape(a) for a in _ASSET_CLASSES)

_ASSET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # "집합투자기구의 종류: 증권(채권형)" / "투자신탁의 종류 | 증권집합투자기구(채권형)"
    ("종류_표제", re.compile(
        r'(?:집합투자기구|투자신탁|펀드)\s*의?\s*종류[^\n]{0,40}?'
        rf'({_ASSET_ALT})')),
    # "상품분류 | 채권형" / "자산유형: 주식형"
    ("분류_표제", re.compile(
        rf'(?:상품\s*분류|자산\s*유형|투자\s*대상)[^\n]{{0,20}}?({_ASSET_ALT})')),
    # "[채권형]" — 상품명 옆 대괄호 표기
    ("대괄호", re.compile(rf'[\[【]\s*({_ASSET_ALT})\s*[\]】]')),
)


def extract_asset_class(text: str, facts: ProductFacts) -> Optional[FactHit]:
    hits: list[FactHit] = []
    for name, pat in _ASSET_PATTERNS:
        for m in pat.finditer(text):
            val = m.group(1)
            hits.append(FactHit(
                axis=AXIS_ASSET_CLASS, value=val, label=val,
                snippet=_line_of(text, m.start()), pattern=name))
        if hits:
            break
    return _pick_one(hits, AXIS_ASSET_CLASS, facts)


# ════════════════════════════════════════════════════════════════
# 축 3 · 수익률
# ════════════════════════════════════════════════════════════════
#
# 수익률은 **여러 개가 정상**이다(1년/3년/설정 이후, 클래스별). 그래서
# _pick_one 을 쓰지 않고 목록으로 남긴다. 다만 무한정 담지는 않는다 —
# 투자설명서에는 벤치마크·비교지수 수익률도 함께 실려서, 표제어 없이
# 퍼센트만 긁으면 총보수·세율까지 딸려 온다.

_PERIOD = r'(최근\s*)?(\d+\s*(?:년|개월)|설정\s*이후|누적)'

_RETURN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # "최근 1년 수익률 5.23%" / "1년 수익률: -2.10%"
    ("기간_수익률", re.compile(
        rf'{_PERIOD}\s*수익률[^\n\d%-]{{0,10}}(-?\d+\.\d+)\s*%')),
    # "수익률 | 1년 | 5.23%"  (표에서 기간이 앞에 오는 형태)
    ("수익률_표", re.compile(
        rf'수익률[^\n]{{0,10}}[|｜]\s*{_PERIOD}\s*[|｜]\s*(-?\d+\.\d+)\s*%')),
)

# 수익률로 오인하기 쉬운 문맥 — 이 말이 같은 줄에 있으면 담지 않는다.
#
# ⚠️ 실측(158문서)에서 '수익률'은 6,972회 나오지만 **대부분 실적이 아니다.**
#    실제로 확인된 세 가지 오인 문맥:
#      · "실제 수익률 변동성을 감안하여 3등급으로 분류" → 위험등급 산정 설명
#      · "연간 투자수익률은 5%로 **가정**하였습니다"     → 보수 예시용 가정치
#      · "수익률 **추종**을 목표로 하는"                → 지수 추종 서술
#    특히 두 번째가 위험하다. 5%는 그 펀드의 실적이 아니라 비용 예시를 위한
#    가정인데, 이걸 실적으로 답하면 명백한 오답이다. 그래서 '가정·변동성·
#    추종·목표'를 차단어에 넣는다.
#
#    실측 커버리지가 0/158인 것은 패턴 실패이기도 하지만, **지금 잡히는
#    후보가 전부 실적이 아니었다**는 뜻이기도 하다. 억지로 넓혀 가정치를
#    실적으로 내보내는 것보다 0건이 낫다 — 못 뽑은 것은 한계 고지로
#    처리되지만, 잘못 뽑은 것은 사용자가 그대로 믿는다.
_RETURN_BLOCKLIST = ("보수", "수수료", "세율", "과세", "공제", "한도",
                     "가정", "변동성", "추종", "목표", "등급")


def extract_returns(text: str) -> list[FactHit]:
    out: list[FactHit] = []
    seen: set[tuple] = set()
    for name, pat in _RETURN_PATTERNS:
        for m in pat.finditer(text):
            line = _line_of(text, m.start())
            if any(b in line for b in _RETURN_BLOCKLIST):
                continue
            groups = m.groups()
            period = re.sub(r'\s+', '', groups[1] or "")
            rate = float(groups[-1])
            key = (period, rate)
            if key in seen:
                continue
            seen.add(key)
            out.append(FactHit(
                axis=AXIS_RETURNS, value=rate, label=f"{period} {rate}%",
                snippet=line, pattern=name))
    return out


# ════════════════════════════════════════════════════════════════
# 축 4 · 시장잔고 (AUM)
# ════════════════════════════════════════════════════════════════
#
# 단위가 섞여 있다(억원 / 백만원 / 원). **억원으로 통일해 저장**하고
# 원문 표기는 label에 남긴다. 단위를 통일하지 않으면 비교가 불가능하고,
# 원문을 버리면 인용이 불가능하다.

# ⚠️ 실측(158문서): **'시장잔고'라는 말은 코퍼스에 0회 등장한다.**
#    그건 과제 안내가 쓴 축 이름이고, 문서는 '설정액'(211회)·'순자산'(752회)·
#    '운용규모'로 쓴다. 축 이름을 그대로 검색어로 삼으면 안 되는 이유다.
_AUM_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("표제_잔고", re.compile(
        r'(?:시장\s*잔고|설정\s*액|순자산\s*총?액|운용\s*규모|펀드\s*규모)'
        r'[^\n\d]{0,15}([\d,]+(?:\.\d+)?)\s*(억|백만|조)?\s*원')),
)

_AUM_UNIT_TO_EOK = {"조": 10000.0, "억": 1.0, "백만": 0.01, None: 1e-8, "": 1e-8}

# ⚠️ 규제 기준선을 실제 잔고로 오인하지 않기 위한 차단어.
#
# 실측(158문서)에서 여러 문서가 똑같이 ['15.0', '50.0'] 억원으로 충돌했다.
# 서로 다른 펀드가 같은 값을 갖는 건 우연이 아니다 — "설정액 50억원 미만인
# 소규모 집합투자기구" 같은 **제도상 기준선**을 긁고 있었다는 뜻이다.
# 기준선을 그 펀드의 잔고로 답하면 명백한 오답이므로, 같은 줄에 이 말이
# 있으면 담지 않는다.
_AUM_BLOCKLIST = ("미만", "이상", "초과", "이하", "소규모", "기준",
                  "요건", "해지", "해산")


def extract_aum(text: str, facts: ProductFacts) -> Optional[FactHit]:
    hits: list[FactHit] = []
    for name, pat in _AUM_PATTERNS:
        for m in pat.finditer(text):
            line = _line_of(text, m.start())
            if any(b in line for b in _AUM_BLOCKLIST):
                continue
            raw, unit = m.group(1), m.group(2)
            try:
                amount = float(raw.replace(",", ""))
            except ValueError:
                continue
            factor = _AUM_UNIT_TO_EOK.get(unit, 1e-8)
            eok = round(amount * factor, 4)
            hits.append(FactHit(
                axis=AXIS_AUM, value=eok,
                label=f"{raw}{unit or ''}원",
                snippet=_line_of(text, m.start()), pattern=name))
        if hits:
            break
    return _pick_one(hits, AXIS_AUM, facts)


# ════════════════════════════════════════════════════════════════
# 축 5 · 확정금리 (원리금보장형)
# ════════════════════════════════════════════════════════════════
#
# ━━ 왜 수익률과 별도 축인가 ━━
# 과제 안내 5페이지는 연금상품을 둘로 나눈다:
#
#   원리금보장형 — 예금·GIC 등. 원금이 보장된다.
#   실적배당형   — 펀드·ETF 등. 운용 실적에 따라 손익이 갈린다.
#
# 그리고 6축(상품분류·위험등급·판매클래스·총보수·수익률·시장잔고)은
# **실적배당형 투자설명서**를 전제로 정의된 것이다. 그런데 실적배당형의
# '수익률'은 성격상 단일 값으로 확정할 수 없다:
#   · 과거 실적일 뿐 미래를 보장하지 않는다(투자설명서 자신이 명시한다)
#   · 클래스별·기간별(1년/3년/5년/설정 이후)로 값이 갈린다
#   · 실측에서 잡히는 것은 대부분 '5%로 가정' 같은 예시용 가정치였다
#
# 반면 **원리금보장형의 약정이율은 계약상 확정된 값**이다. 사용자에게
# 의미 있는 숫자이고, 결정론적으로 뽑아도 왜곡이 없다. 그래서 실적배당형
# 수익률과 섞지 않고 별도 축으로 둔다 — 섞으면 "확정된 이율"과 "지나간
# 실적"이 같은 이름으로 답변에 실린다.

_RATE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # "약정이율 연 3.50%" / "적용금리: 3.20 %" / "확정금리 연 3.4%"
    ("표제_이율", re.compile(
        r'(?:약정\s*이율|적용\s*금리|확정\s*금리|제시\s*금리|공시\s*이율|'
        r'기본\s*이율|약정\s*금리)[^\n%]{0,15}?(\d+(?:\.\d+)?)\s*%')),
    # 표 형태: "약정이율 | 3.50%"
    ("표_이율", re.compile(
        r'(?:약정\s*이율|적용\s*금리|확정\s*금리|공시\s*이율)\s*[|｜:]\s*'
        r'연?\s*(\d+(?:\.\d+)?)\s*%')),
)

# ⚠️ 이율로 오인하면 안 되는 것들. 특히 '연체이율'과 '중도해지이율'은
#    실재하는 이율이지만 **그 상품의 약정이율이 아니다.** 중도해지이율을
#    "이 상품 금리는 0.5%입니다"로 답하면 명백한 오답이다.
_RATE_BLOCKLIST = ("연체", "중도해지", "중도 해지", "지연", "할인", "가산",
                   "세율", "과세", "물가", "수수료")


def extract_guaranteed_rate(text: str,
                            facts: ProductFacts) -> Optional[FactHit]:
    hits: list[FactHit] = []
    for name, pat in _RATE_PATTERNS:
        for m in pat.finditer(text):
            line = _line_of(text, m.start())
            if any(b in line for b in _RATE_BLOCKLIST):
                continue
            rate = float(m.group(1))
            # 연금 상품의 약정이율이 20%를 넘는 일은 없다. 넘으면
            # 다른 수치를 잘못 집은 것이다(예: 편입비율·한도).
            if not (0 < rate <= 20):
                continue
            hits.append(FactHit(
                axis=AXIS_GUARANTEED_RATE, value=rate,
                label=f"연 {rate}%", snippet=line, pattern=name))
        if hits:
            break
    return _pick_one(hits, AXIS_GUARANTEED_RATE, facts)


# ════════════════════════════════════════════════════════════════
# 진입점
# ════════════════════════════════════════════════════════════════

def extract_product_facts(text: str, doc_id: str = "") -> ProductFacts:
    """문서 전문에서 4축을 뽑는다 (판매클래스·총보수는 products.py 담당).

    ⚠️ 실패는 예외가 아니라 **빈 값 + conflicts 기록**이다. 투자설명서가
       아닌 문서(제도안내·약관)에는 이 축들이 애초에 없는 것이 정상이므로,
       못 찾았다고 해서 오류가 아니다.
    """
    facts = ProductFacts(doc_id=doc_id)
    if not text:
        return facts
    facts.risk_grade = extract_risk_grade(text, facts)
    facts.asset_class = extract_asset_class(text, facts)
    facts.aum = extract_aum(text, facts)
    facts.returns = extract_returns(text)
    facts.guaranteed_rate = extract_guaranteed_rate(text, facts)
    return facts


# ── 진단용 — 키워드는 있는데 안 잡힌 줄 ────────────────────────
#
# 이게 없으면 패턴을 근거 있게 고칠 수 없다. corpus_health.py가
# "문턱 미달로 지나간 구간까지 보고"하는 것과 같은 이유다.

_AXIS_KEYWORDS: dict[str, tuple[str, ...]] = {
    AXIS_RISK_GRADE: ("위험등급", "위험 등급", "등급"),
    AXIS_ASSET_CLASS: ("상품분류", "자산유형", "집합투자기구의 종류",
                       "투자신탁의 종류"),
    AXIS_RETURNS: ("수익률",),
    AXIS_AUM: ("시장잔고", "설정액", "순자산총액", "운용규모"),
    AXIS_GUARANTEED_RATE: ("약정이율", "적용금리", "확정금리", "공시이율",
                           "원리금보장"),
}


def _fact_lines(raw: dict) -> list[tuple[str, str, str]]:
    """저장된 팩트 dict → (축, 표기, 원문 스니펫) 목록."""
    out: list[tuple[str, str, str]] = []
    for key, axis in (("risk_grade", AXIS_RISK_GRADE),
                      ("asset_class", AXIS_ASSET_CLASS),
                      ("aum", AXIS_AUM),
                      ("guaranteed_rate", AXIS_GUARANTEED_RATE)):
        if hit := raw.get(key):
            out.append((axis, hit.get("label") or str(hit.get("value")),
                        hit.get("snippet", "")))
    for hit in (raw.get("returns") or [])[:4]:
        out.append((AXIS_RETURNS, hit.get("label", ""), hit.get("snippet", "")))
    return out


def collect_facts(doc_ids, doc_meta_lookup) -> list[dict]:
    """근거로 쓰인 문서들의 상품 팩트를 모은다 (서빙 시점).

    doc_meta_lookup : doc_id -> doc_meta dict 를 돌려주는 호출 가능 객체
                      (DocumentStore.doc_meta 를 그대로 넘기면 된다)

    ⚠️ **근거에 오른 문서만** 본다. 색인에는 158문서의 팩트가 다 있지만,
       검색이 고르지 않은 문서의 수치를 답변에 쓰면 그건 근거 없는 인용이다.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for doc_id in doc_ids:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        meta = doc_meta_lookup(doc_id) or {}
        raw = meta.get("product_facts") or {}
        if not _fact_lines(raw):
            continue
        out.append({**raw, "doc_id": doc_id,
                    "product_name": (meta.get("entities") or {}).get(
                        "product_name", "")})
    return out


def render_facts_block(facts: list[dict], limit: int = 4) -> str:
    """답변 생성 프롬프트에 실을 블록.

    ⚠️ 원문 스니펫을 함께 싣는다. 값만 주면 모델이 그 값을 근거 없이
       재해석하거나 다른 상품에 붙일 수 있다. 원문을 보여 주면 인용이
       되고, 수치 검증도 같은 문자열로 통과한다.

    ⚠️ 위험등급은 **숫자만 쓰지 말라**고 명시한다. 1등급이 가장 위험한데,
       숫자만 보면 "1등급이라 안전하다"는 정반대 서술이 나온다.
    """
    if not facts:
        return ""
    lines = ["\n[상품 팩트 — 제공 문서에서 추출한 확정 값. 이 값만 사용 가능]"]
    for f in facts[:limit]:
        head = f.get("product_name") or f.get("doc_id", "")
        lines.append(f"· {head} ({f.get('doc_id', '')})")
        for axis, label, snippet in _fact_lines(f):
            lines.append(f"    {axis}: {label}")
            if snippet:
                lines.append(f"      근거 원문: {snippet}")
        if conflicts := f.get("conflicts"):
            for axis, why in conflicts.items():
                lines.append(f"    {axis}: 확정 불가 — {why}")
    lines.append(
        "  ※ 위험등급은 숫자가 작을수록 위험이 큽니다(1등급이 가장 높은 위험). "
        "숫자만 쓰지 말고 위 표기를 그대로 옮기십시오.")
    if any(f.get("guaranteed_rate") for f in facts[:limit]):
        # 확정금리와 실적 수익률을 같은 말로 쓰면 안 된다 — 전자는 계약상
        # 보장된 값이고 후자는 지나간 실적이다. 사용자가 가장 오해하기
        # 쉬운 자리이므로 프롬프트에서 명시적으로 구분해 준다.
        lines.append(
            "  ※ 확정금리는 원리금보장형의 **약정된 이율**입니다. "
            "실적배당형의 수익률(과거 실적)과 같은 것처럼 쓰지 마십시오.")
    return "\n".join(lines)


def fact_snippets(facts: list[dict]) -> list[str]:
    """수치 검증용 원문 텍스트.

    이 스니펫들은 코퍼스 원문에서 결정론적으로 잘라 온 것이므로, 여기
    들어 있는 수치는 정의상 근거가 있다. 검색이 표 청크를 못 건졌다는
    이유로 정작 우리가 문서에서 확정한 값이 '근거 없는 수치'로 잡히면
    안 된다 — 그 경우 답변이 통째로 템플릿으로 축퇴한다.
    """
    out: list[str] = []
    for f in facts:
        for _axis, label, snippet in _fact_lines(f):
            if snippet:
                out.append(snippet)
            if label:
                out.append(label)
    return out


def near_misses(text: str, facts: ProductFacts) -> dict[str, list[str]]:
    """축 키워드는 있는데 값이 안 뽑힌 줄을 모은다 — 패턴 조정 근거."""
    found = set(facts.found_axes)
    out: dict[str, list[str]] = {}
    for axis, keywords in _AXIS_KEYWORDS.items():
        if axis in found:
            continue
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if s and any(k in s for k in keywords):
                lines.append(s[:160])
            if len(lines) >= 5:
                break
        if lines:
            out[axis] = lines
    return out
