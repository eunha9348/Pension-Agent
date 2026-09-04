"""상품 팩트 6축 추출 (2026-09-04 신설).

━━ 무엇이 없었는가 ━━
과제 안내가 정의한 실적배당형 상품 데이터의 축은 여섯이다:
  상품분류(자산유형) · 위험등급 · 판매클래스 · 총보수 · 수익률 · 시장잔고

구현돼 있던 것은 **판매클래스·총보수 둘뿐**이었다(products.py).
위험등급은 trap_rules D2가 "운용사 간 직접 비교 주의"라는 경고 문구만
끼워 넣을 뿐, 등급 값 자체를 문서에서 뽑아 대조하는 장치가 없었다.
수익률·시장잔고·상품분류는 추출기도 계산함수도 트랩 규칙도 없었다.

━━ 두 가지 결함을 동시에 고친다 ━━
① **축 4개가 아예 없던 것** — product_facts.py 신설
② **검색이 표 청크를 놓치면 사실이 사라지던 것** — 기존
   products.py::extract_class_expenses는 검색된 근거 청크만 본다.
   그래서 검색 순위에 사실의 존재 여부가 걸려 있었다. 새 추출은
   **색인 시점에 문서 전문**을 훑어 doc_meta에 남기므로 사라지지 않는다.

━━ 왜 정규식인가 (MRC/NER이 아니라) ━━
투자설명서는 【제목】 + 파이프 구분 표라는 규칙적 형태다. "이 상품의
위험등급이 몇 등급인가"는 판단이지 생성이므로 "판단은 코드, 문장은 LLM"
원칙이 그대로 적용된다. span 모델은 확률적이라 틀린 구간을 높은 확신으로
반환해도 결정론적으로 기각할 수 없고, torch 모델이 하나 더 상주한다.
"""

from __future__ import annotations

import pytest

from app.analysis.product_facts import (AXIS_AUM, AXIS_RETURNS,
                                        AXIS_RISK_GRADE, collect_facts,
                                        extract_product_facts, fact_snippets,
                                        near_misses, render_facts_block)

# ── 실제 투자설명서 형태를 본뜬 표본 ──────────────────────────

_HEADED = """미래에셋퇴직연금증권자투자신탁
【집합투자기구의 종류】
투자신탁의 종류 | 증권집합투자기구(채권형)
【투자위험등급】
투자위험등급: 제4등급 (보통 위험)
【운용실적】
최근 1년 수익률 3.87%
최근 3년 수익률 12.41%
【펀드 규모】
순자산총액 2,450억원
"""

_TABLE = """위험등급 | 3등급
상품분류 | 주식형
수익률 | 1년 | -2.10%
시장잔고 | 5,678억원
"""

# 총보수·세율·신용등급이 섞인 문맥 — 전부 오탐 대상이다
_BAIT = """【보수 및 수수료에 관한 사항】
종류 | 총보수(연)
C-P  | 0.5440%
C-Pe | 0.4390%
연금소득세율은 5.5%이며 세액공제율은 13.2%입니다.
신용등급 A2 이상 채권에 투자합니다.
연 3등급 이상의 우량 등급을 유지합니다.
"""


# ════════════════════════════════════════════════════════════
# 1. 축별 추출
# ════════════════════════════════════════════════════════════

def test_표제형_문서에서_네_축을_모두_뽑는다():
    f = extract_product_facts(_HEADED, "d1")
    assert f.risk_grade.value == 4
    assert f.asset_class.value == "채권형"
    assert f.aum.value == 2450.0
    assert {h.value for h in f.returns} == {3.87, 12.41}


def test_표형식_문서에서도_뽑는다():
    """【제목】 없이 파이프 표만 있는 형태."""
    f = extract_product_facts(_TABLE, "d2")
    assert f.risk_grade.value == 3
    assert f.asset_class.value == "주식형"
    assert f.aum.value == 5678.0
    assert [h.value for h in f.returns] == [-2.10]


def test_모든_값이_원문_스니펫을_들고_다닌다():
    """★ 값만 남기면 인용도 검증도 못 한다."""
    f = extract_product_facts(_HEADED, "d1")
    for hit in (f.risk_grade, f.asset_class, f.aum, *f.returns):
        assert hit.snippet, f"{hit.axis}에 근거 원문이 없다"
        assert hit.pattern, f"{hit.axis}에 패턴 이름이 없다 (진단 불가)"


def test_투자설명서가_아니면_빈_값이_정상이다():
    """제도안내·약관에는 이 축들이 애초에 없다 — 오류가 아니다."""
    f = extract_product_facts("연금저축 세액공제 한도는 연 600만원입니다.", "d3")
    assert f.found_axes == []
    assert not f.conflicts


# ════════════════════════════════════════════════════════════
# 2. 오탐 방지 — 결정론 계층에서 오탐은 미탐보다 나쁘다
# ════════════════════════════════════════════════════════════

def test_총보수_세율_신용등급을_잡지_않는다():
    """★ 0.5440%(총보수)·5.5%(세율)·A2(신용등급)는 이 축들이 아니다."""
    f = extract_product_facts(_BAIT, "bait")
    assert f.risk_grade is None, f"신용등급/연 3등급을 위험등급으로 잡았다: {f.risk_grade}"
    assert f.returns == [], f"보수·세율을 수익률로 잡았다: {f.returns}"
    assert f.aum is None


def test_수익률_차단어가_같은_줄에_있으면_담지_않는다():
    f = extract_product_facts("1년 수익률 기준 총보수 0.54%를 공제합니다.", "x")
    assert f.returns == []


def test_라벨_없는_등급은_표제어가_있어야_잡는다():
    """'제2등급'만 덜렁 있으면 무슨 등급인지 알 수 없다."""
    assert extract_product_facts("본 채권은 제2등급입니다.", "x").risk_grade is None
    ok = extract_product_facts("제2등급(높은 위험)에 해당합니다.", "x")
    assert ok.risk_grade.value == 2


# ════════════════════════════════════════════════════════════
# 2-b. 실물 코퍼스 실측으로 드러난 것 (2026-09-04, 158문서)
# ════════════════════════════════════════════════════════════
#
# 아래 문형은 전부 서버에서 `python -m scripts.corpus_facts --index`를
# 돌려 얻은 **실제 출력**에서 그대로 옮긴 것이다. 추측으로 만든 표본이
# 아니다 — 패턴을 실물에 맞추는 유일한 근거다.

_REAL_SELF_CLASSIFY = """미래에셋자산운용㈜는 이 집합투자기구의 실제 수익률 변동성을 감안
하여 1등급으로 분류하였습니다. 펀드의 위험 등급은 운용실적, 시장 상
투자위험등급
1등급[매우 높은 위험]
[투자위험등급 분류 기준]
1등급[매우 높은 위험]
2등급[높은 위험]
3등급[다소 높은 위험]
4등급[보통 위험]
5등급[낮은 위험]
6등급[매우 낮은 위험]
"""


def test_등급체계_설명표가_있어도_자기_등급을_확정한다():
    """★ 실측에서 35건이 이것 때문에 '충돌'로 버려졌다.

    투자설명서에는 1~6등급을 전부 나열한 **등급 체계 설명표**가 함께
    실린다. 그것까지 긁으면 서로 다른 값이 잡혀 확정을 포기하게 된다.
    'N등급으로 분류하였습니다'는 그 펀드 자신의 등급만 말하므로,
    이 문형을 최우선으로 봐야 한다.
    """
    f = extract_product_facts(_REAL_SELF_CLASSIFY, "R2_KR510902511M")
    assert f.risk_grade is not None, f"등급 체계표 때문에 확정을 포기했다: {f.conflicts}"
    assert f.risk_grade.value == 1
    assert f.risk_grade.pattern == "자기분류"


def test_자기분류가_등급체계표보다_우선한다():
    """★ 실측 R2_KR5110501016 — ['4','5','6'] 충돌로 버려졌던 문서."""
    text = ("하여 6등급으로 분류하였습니다.\n투자위험등급\n6등급[매우 낮은 위험]\n"
            "등급체계: 4등급[보통 위험] 5등급[낮은 위험] 6등급[매우 낮은 위험]\n")
    f = extract_product_facts(text, "R2_KR5110501016")
    assert f.risk_grade.value == 6, f.conflicts


def test_표제_다음줄에_등급이_와도_잡는다():
    """실물은 '투자위험등급' 다음 **줄**에 등급이 온다.
    [^\\n]으로 막으면 이 형태를 통째로 놓친다."""
    f = extract_product_facts("투자위험등급\n2등급[높은 위험]\n", "x")
    assert f.risk_grade.value == 2


@pytest.mark.parametrize("label,text", [
    ("위험등급 산정 설명",
     "실제 수익률 변동성을 감안하여 3등급으로 분류하였습니다"),
    ("보수 예시용 가정치",
     "총보수비용은 일정하고, 연간 투자수익률은 5%로 가정하였습니다."),
    ("지수 추종 서술", "수익률 추종을 목표로 하는 3.554"),
])
def test_수익률로_오인하기_쉬운_문맥을_거부한다(label, text):
    """★ 실측에서 '수익률'은 6,972회 나오지만 대부분 실적이 아니다.

    특히 '연간 투자수익률 5% 가정'은 **보수 예시를 위한 가정치**다.
    이걸 그 펀드의 실적으로 답하면 명백한 오답이므로, 억지로 넓혀
    잡는 것보다 0건이 낫다.
    """
    assert extract_product_facts(text, "x").returns == [], label


@pytest.mark.parametrize("label,text", [
    ("소규모 해지 기준", "설정액 50억원 미만인 소규모 집합투자기구는 해지될 수 있습니다."),
    ("유지 요건", "순자산총액 15억원 이상을 유지하여야 합니다."),
])
def test_규제_기준선을_시장잔고로_잡지_않는다(label, text):
    """★ 실측에서 여러 문서가 똑같이 ['15.0','50.0']억으로 충돌했다.

    서로 다른 펀드가 같은 값을 갖는 건 우연이 아니다 — 제도상 기준선을
    긁고 있었다는 뜻이다. 기준선을 그 펀드의 잔고로 답하면 오답이다.
    """
    assert extract_product_facts(text, "x").aum is None, label


def test_실제_잔고는_여전히_잡는다():
    """대조군 — 차단어가 정상 값까지 막으면 안 된다."""
    f = extract_product_facts("순자산총액 2,450억원\n", "x")
    assert f.aum is not None and f.aum.value == 2450.0


# ════════════════════════════════════════════════════════════
# 2-c. 확정금리 (원리금보장형) — 수익률과 별개 축
# ════════════════════════════════════════════════════════════
#
# 과제 안내 5페이지는 연금상품을 원리금보장형(예금·GIC)과
# 실적배당형(펀드·ETF)으로 나눈다. 6축은 실적배당형 전제이고, 그쪽의
# '수익률'은 과거 실적이라 단일 값으로 확정할 수 없다.
#
# 반면 **원리금보장형의 약정이율은 계약상 확정된 값**이라 결정론적으로
# 뽑아도 왜곡이 없다. 그래서 별도 축으로 둔다 — 섞으면 "보장된 이율"과
# "지나간 실적"이 답변에서 같은 이름으로 나간다.

@pytest.mark.parametrize("label,text,expected", [
    ("약정이율 평문", "본 상품의 약정이율은 연 3.50%입니다.", 3.5),
    ("표 형태", "약정이율 | 3.20%", 3.2),
    ("공시이율", "공시이율 연 2.85% 적용", 2.85),
])
def test_원리금보장형_약정이율을_뽑는다(label, text, expected):
    f = extract_product_facts(text, "x")
    assert f.guaranteed_rate is not None, label
    assert f.guaranteed_rate.value == expected


@pytest.mark.parametrize("label,text", [
    # ★ 실재하는 이율이지만 **그 상품의 약정이율이 아니다.**
    #    중도해지이율을 "이 상품 금리는 0.5%"로 답하면 명백한 오답이다.
    ("연체이율", "연체이율은 연 12.0%가 적용됩니다."),
    ("중도해지이율", "중도해지이율 연 0.50%"),
    ("세율 혼동", "적용금리 대비 세율은 15.4%입니다."),
    ("비율 오인", "약정이율 편입비율 85.0%"),
])
def test_이율로_오인하기_쉬운_것을_거부한다(label, text):
    assert extract_product_facts(text, "x").guaranteed_rate is None, label


@pytest.mark.parametrize("text", [
    # ★ 실물 158문서에서 '약정이율' 25회가 전부 이 형태였다 —
    #    위험 고지문이고 수치가 없다. 이 코퍼스에 원리금보장형 상품
    #    문서가 없다는 뜻이므로 0건이 올바른 동작이다.
    "자산의 경우 시장매각이 제한되고, 중도해지 시 약정이율의 축소 적용 등 불이익이 발생",
    "약정이율 축소 적용 등으로 당초 기대했던 수익 보다 적어질 위험이 있습니다.",
])
def test_실물_약정이율_고지문에서_값을_지어내지_않는다(text):
    assert extract_product_facts(text, "x").guaranteed_rate is None


def test_확정금리와_수익률을_구분하라고_프롬프트에_명시한다():
    """★ 사용자가 가장 오해하기 쉬운 자리다."""
    f = extract_product_facts("약정이율 연 3.50%", "d1")
    block = render_facts_block([f.as_dict()])
    assert "확정금리" in block
    assert "과거 실적" in block and "같은 것처럼 쓰지" in block


def test_실적배당형_수익률은_여전히_확정하지_않는다():
    """대조군 — 확정금리 축을 넣었다고 수익률 판정이 느슨해지면 안 된다."""
    f = extract_product_facts(
        "연간 투자수익률은 5%로 가정하였습니다.", "x")
    assert f.returns == []


# ════════════════════════════════════════════════════════════
# 2-d. 과거 운용실적 표 — 파싱하지 않고 원문 인용
# ════════════════════════════════════════════════════════════
#
# 과제 안내 5페이지가 수익률을 6축에 넣었으므로 빼지 않는다. 다만 실물은
# 헤더와 값이 다른 줄에 있는 **다단 표**이고 OCR이 깨져 있어, 컬럼을
# 정렬해 파싱하면 엉뚱한 펀드에 엉뚱한 값이 붙는다. 그래서 표를 잘라서
# **그대로 보여주고**, 우리가 숫자를 재구성하지는 않는다.

_REAL_TABLE = """미래에셋퇴직연금증권자투자신탁
투자위험등급
3등급[다소 높은 위험]
기구 수 운용규모 최근1년 최근2년 최근1년 최근2년
12 | 3,450억원 | 5.23% | 8.11% | 4.90% | 7.80%
비교지수 | - | 4.80% | 7.20% | - | -
"""


def test_실적표를_원문_그대로_인용한다():
    """★ 실측에서 잡힌 '최근1년 최근2년' 헤더 형태."""
    f = extract_product_facts(_REAL_TABLE, "d1")
    assert f.return_table is not None, "실적표를 못 찾았다"
    assert "최근1년" in str(f.return_table.value)
    assert "5.23%" in str(f.return_table.value)


def test_실적표의_컬럼을_파싱하지_않는다():
    """★ 핵심 안전판 — 컬럼을 재구성하면 오정렬로 오답이 난다.

    표가 있어도 개별 수익률 '값'으로는 확정하지 않는다. 표는 참고용
    원문으로만 제시한다.
    """
    f = extract_product_facts(_REAL_TABLE, "d1")
    assert f.returns == [], f"표에서 값을 파싱했다: {[h.label for h in f.returns]}"


def test_실적표가_있으면_수익률_축이_채워진_것으로_본다():
    from app.analysis.product_facts import AXIS_RETURNS
    f = extract_product_facts(_REAL_TABLE, "d1")
    assert AXIS_RETURNS in f.found_axes


def test_명시적_문장은_값으로_확정한다():
    """표와 달리 한 줄에 기간과 수치가 함께 있으면 모호하지 않다."""
    f = extract_product_facts("최근 1년 수익률 3.87%", "x")
    assert [h.value for h in f.returns] == [3.87]


def test_실적표_제시에_과거실적_고지가_붙는다():
    """★ '예상 수익률'로 읽히면 안 된다 — 준법 감사도 그 표현을 막는다."""
    f = extract_product_facts(_REAL_TABLE, "d1")
    block = render_facts_block([f.as_dict()])
    assert "과거 운용실적" in block
    assert "보장하지 않습니다" in block
    assert "임의로 값을 골라" in block


# ── 총보수 표 (실측 문형) ──────────────────────────────────
_REAL_EXPENSE = """미래에셋퇴직연금증권자투자신탁
투자자가 부담하는 수수료 및 총보수 (단위: %) 1,000만원 투자시 투자자가
수수료 총보수 동종유형
C-P 0.5440 0.6100
C-Pe 0.4390 0.5200
"""


def test_총보수_표를_원문_그대로_인용한다():
    """★ 실측: 총보수는 100/158 문서에 있는데 기존 추출기는 5건(3.2%)만
    잡았다. 원인은 '클래스와 요율이 같은 줄에 30자 이내'를 요구하는데
    실물 표가 그 형태가 아니기 때문이다 — 수익률과 같은 다단 표다."""
    f = extract_product_facts(_REAL_EXPENSE, "d1")
    assert f.expense_table is not None, "총보수 표를 못 찾았다"
    assert "0.5440" in str(f.expense_table.value)


def test_총보수_표의_컬럼을_짝지어_파싱하지_않는다():
    """★ 클래스↔요율을 임의로 짝지으면 엉뚱한 클래스에 엉뚱한 보수가 붙는다."""
    from app.analysis.product_facts import AXIS_EXPENSE
    f = extract_product_facts(_REAL_EXPENSE, "d1")
    assert AXIS_EXPENSE in f.found_axes
    # 표 인용일 뿐 개별 값 확정이 아니다
    assert f.expense_table.pattern == "보수표_원문"


def test_총보수_표_제시에_주의_문구가_붙는다():
    f = extract_product_facts(_REAL_EXPENSE, "d1")
    block = render_facts_block([f.as_dict()])
    assert "임의로" in block and "가입 자격" in block


def test_프롬프트_지시문에_마크다운을_쓰지_않는다():
    """★ HCX가 지시문의 마크다운을 그대로 따라 쓴다 (CLAUDE.md 실연동 확인)."""
    f = extract_product_facts(_REAL_TABLE, "d1")
    block = render_facts_block([f.as_dict()])
    assert "**" not in block, "지시문에 마크다운이 섞였다"


# ════════════════════════════════════════════════════════════
# 3. 값이 갈리면 확정하지 않는다
# ════════════════════════════════════════════════════════════

def test_서로_다른_값이_나오면_비우고_사유를_남긴다():
    """★ 억지로 하나를 고르면 그 순간 날조다."""
    text = ("투자위험등급: 제2등급 (높은 위험)\n"
            "투자위험등급: 제5등급 (낮은 위험)\n")
    f = extract_product_facts(text, "conflict")
    assert f.risk_grade is None
    assert AXIS_RISK_GRADE in f.conflicts
    assert "2" in f.conflicts[AXIS_RISK_GRADE]


def test_같은_값이_반복되는_것은_정상이다():
    """투자설명서는 같은 값을 여러 번 싣는다 — 충돌이 아니다."""
    text = "투자위험등급: 제4등급 (보통 위험)\n" * 3
    f = extract_product_facts(text, "dup")
    assert f.risk_grade.value == 4
    assert not f.conflicts


# ════════════════════════════════════════════════════════════
# 4. 위험등급 방향 — 1등급이 가장 위험하다
# ════════════════════════════════════════════════════════════

def test_표준과_어긋난_등급표기는_경고를_남긴다():
    """구형 5단계이거나 판독 오류일 수 있다 — 버리지 말고 드러낸다."""
    f = extract_product_facts("투자위험등급: 1등급(매우 낮은 위험)", "old")
    assert f.risk_grade.value == 1
    assert "매우 높은 위험" in f.risk_grade.warning


def test_표준과_맞으면_경고가_없다():
    f = extract_product_facts("투자위험등급: 1등급(매우 높은 위험)", "std")
    assert f.risk_grade.warning == ""


def test_프롬프트_블록이_등급_방향을_명시한다():
    """★ 숫자만 주면 '1등급이라 안전하다'는 정반대 서술이 나온다."""
    f = extract_product_facts(_HEADED, "d1")
    block = render_facts_block([{**f.as_dict(), "product_name": "테스트펀드"}])
    assert "1등급이 가장 높은 위험" in block
    assert "4등급(보통 위험)" in block


def test_프롬프트_블록에_근거_원문이_실린다():
    f = extract_product_facts(_HEADED, "d1")
    block = render_facts_block([f.as_dict()])
    assert "근거 원문" in block
    assert "투자위험등급: 제4등급 (보통 위험)" in block


# ════════════════════════════════════════════════════════════
# 5. 서빙 — 근거로 채택된 문서의 팩트만 쓴다
# ════════════════════════════════════════════════════════════

def _meta_for(text: str) -> dict:
    return {"entities": {"product_name": "테스트펀드"},
            "product_facts": extract_product_facts(text, "d1").as_dict()}


def test_근거에_없는_문서의_팩트는_가져오지_않는다():
    """★ 색인에는 158문서가 다 있지만, 검색이 안 고른 문서의 수치를
    답변에 쓰면 그건 근거 없는 인용이다."""
    store = {"d1": _meta_for(_HEADED), "d2": _meta_for(_TABLE)}
    got = collect_facts(["d1"], store.get)
    assert [f["doc_id"] for f in got] == ["d1"]


def test_팩트가_없는_문서는_목록에_오르지_않는다():
    store = {"d9": {"product_facts": {}}}
    assert collect_facts(["d9"], store.get) == []


def test_중복_문서는_한_번만_담는다():
    store = {"d1": _meta_for(_HEADED)}
    assert len(collect_facts(["d1", "d1", "d1"], store.get)) == 1


# ════════════════════════════════════════════════════════════
# 6. 배선 — 색인 시점에 실제로 만들어지는가
# ════════════════════════════════════════════════════════════

def test_색인_메타데이터에_팩트가_실린다():
    """★ 배선을 검사하는 테스트는 배선을 지나가야 한다 (CLAUDE.md)."""
    from types import SimpleNamespace

    from app.ingest.metadata import build_doc_metadata

    doc = SimpleNamespace(
        doc_id="R2_TEST", full_text=_HEADED, page_count=1, source_path="x",
        layout="l", warnings=[], pages=[SimpleNamespace(text=_HEADED)])
    meta = build_doc_metadata(doc, [])

    assert "product_facts" in meta, "doc_meta에 product_facts가 없다"
    assert meta["product_facts"]["risk_grade"]["value"] == 4


# ════════════════════════════════════════════════════════════
# 7. 수치 검증 — 확정한 값이 '근거 없는 수치'로 잡히면 안 된다
# ════════════════════════════════════════════════════════════

def test_검색이_표를_놓쳐도_팩트_수치가_검증을_통과한다():
    """★ 이 결함이 이 작업의 핵심 동기다.

    검색이 가입자격 표만 건지고 위험등급 표를 놓치면, 우리가 문서에서
    정확히 확정한 4등급·3.87%가 '근거 없는 수치'로 잡혀 답변이 통째로
    템플릿으로 축퇴한다.
    """
    from app.core.coverage_pipeline import EvidenceChunk
    from app.generation.grounding import make_verify_grounding

    facts = collect_facts(["d1"], {"d1": _meta_for(_HEADED)}.get)
    unrelated = [EvidenceChunk(doc_id="d1", score=0.9,
                               text="가입자격: 연금저축계좌를 통하여 가입한 자.")]
    answer = "이 상품은 4등급(보통 위험)이며 최근 1년 수익률은 3.87%입니다."

    without = make_verify_grounding(question="위험등급 알려줘", slots=[],
                                    llm_call=None, citations=["c"])(
        answer, unrelated)
    assert not without.numeric.passed, "대조군이 성립하지 않는다 (원래 통과했다면 이 테스트는 무의미)"

    with_facts = make_verify_grounding(
        question="위험등급 알려줘", slots=[], llm_call=None, citations=["c"],
        fact_texts=fact_snippets(facts))(answer, unrelated)
    assert with_facts.numeric.passed, (
        f"확정 팩트가 근거 없는 수치로 잡혔다: {with_facts.numeric.ungrounded}")


def test_팩트에_없는_수치는_여전히_잡힌다():
    """★ 안전판 — 팩트 주입이 수치 검증을 무르게 하면 안 된다."""
    from app.core.coverage_pipeline import EvidenceChunk
    from app.generation.grounding import make_verify_grounding

    facts = collect_facts(["d1"], {"d1": _meta_for(_HEADED)}.get)
    v = make_verify_grounding(
        question="위험등급 알려줘", slots=[], llm_call=None, citations=["c"],
        fact_texts=fact_snippets(facts))(
        "이 상품의 위험등급은 6등급이고 수익률은 55.5%입니다.",
        [EvidenceChunk(doc_id="d1", score=0.9, text="가입자격 안내")])
    assert not v.numeric.passed
    # 팩트에 있는 값은 4등급·3.87%·12.41%뿐이다. 6등급도 55.5%도 아니다.
    assert {6.0, 55.5} <= set(v.numeric.ungrounded), v.numeric.ungrounded


# ════════════════════════════════════════════════════════════
# 8. 두 경로 모두에 실리는가 (F3와 같은 계열의 사고 방지)
# ════════════════════════════════════════════════════════════

def test_L5_프롬프트에_팩트_블록이_실린다():
    from app.generation.answer_prompt import build_supervisor_payload

    spec = {"query": "위험등급 알려줘",
            "_product_facts": collect_facts(["d1"], {"d1": _meta_for(_HEADED)}.get)}
    p = build_supervisor_payload(spec, [], [])
    assert "[상품 팩트" in p and "4등급(보통 위험)" in p


def test_L4sub_프롬프트에도_같은_블록이_실린다():
    """★ ADVISORY 경로에만 빠지면 상담형 질의에서 위험등급을 못 말한다."""
    from app.generation.advisory import build_advisory_payload

    spec = {"query": "상품 하나 추천해 주세요",
            "_product_facts": collect_facts(["d1"], {"d1": _meta_for(_HEADED)}.get)}
    p = build_advisory_payload(spec, [])
    assert "[상품 팩트" in p and "4등급(보통 위험)" in p


def test_팩트가_없으면_블록도_없다():
    from app.generation.answer_prompt import build_supervisor_payload
    assert "[상품 팩트" not in build_supervisor_payload({"query": "질문"}, [], [])


# ════════════════════════════════════════════════════════════
# 9. 진단 — 놓친 줄을 보고하는가
# ════════════════════════════════════════════════════════════

def test_키워드는_있는데_못_뽑으면_놓친_줄로_보고한다():
    """★ 이게 없으면 '패턴이 맞아서 0건'인지 '틀려서 0건'인지 모른다."""
    odd = "본 펀드의 위험등급 구분은 별도 표를 참조하십시오."
    f = extract_product_facts(odd, "x")
    misses = near_misses(odd, f)
    assert AXIS_RISK_GRADE in misses
    assert "별도 표" in misses[AXIS_RISK_GRADE][0]


def test_뽑은_축은_놓친_줄에_오르지_않는다():
    f = extract_product_facts(_HEADED, "d1")
    misses = near_misses(_HEADED, f)
    for axis in (AXIS_RISK_GRADE, AXIS_AUM, AXIS_RETURNS):
        assert axis not in misses
