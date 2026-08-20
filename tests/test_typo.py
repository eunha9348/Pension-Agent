"""오타 교정 회귀 — 결정론적 폴백.

━━ 왜 있는가 ━━
L1(HyperCLOVA X)이 만드는 search_terms가 오타·구어체를 문서 용어로 옮겨
주지만, L1 호출이 실패하면(타임아웃·429 — 실제로 여러 번 발생) 규칙 기반
경로로 떨어지고 거기엔 오타 대응이 없었다. BM25는 토큰이 정확히 일치해야
걸리므로 "세엑공제"는 근거를 0건 반환한다.

━━ 이 파일이 지키는 것 ━━
**잘못 고치는 것이 안 고치는 것보다 나쁘다.**
이 도메인은 한 글자 차이가 답을 뒤집는다. 아래 안전장치 테스트를
느슨하게 고치면 오타 교정이 오답 생산기가 된다.
"""

from __future__ import annotations

from app.analysis.query_spec import reconcile_spec, rule_based_spec
from app.analysis.typo import (MIN_LEN, correct_query, correct_token,
                               corrected_terms, edit_distance)


# ════════════════════════════════════════════════════════════════
# 편집거리
# ════════════════════════════════════════════════════════════════

def test_편집거리_기본():
    assert edit_distance("세액공제", "세액공제") == 0
    assert edit_distance("세엑공제", "세액공제") == 1
    assert edit_distance("연금수령연차", "연금실제수령연차") == 2


def test_길이_차이가_크면_조기_종료한다():
    """전수 비교라 성능이 중요하다 — cap을 넘으면 계산을 멈춰야 한다."""
    assert edit_distance("가", "가나다라마바사아", cap=2) > 2


# ════════════════════════════════════════════════════════════════
# 정상 교정
# ════════════════════════════════════════════════════════════════

def test_한_글자_오타를_교정한다():
    assert correct_token("세엑공제") == "세액공제"
    assert correct_token("연금저죽") == "연금저축"
    assert correct_token("퇴직소듣세") == "퇴직소득세"


def test_질의에서_오타를_찾아낸다():
    assert correct_query("세엑공제 얼마에요") == [("세엑공제", "세액공제")]


def test_교정_결과만_추릴_수_있다():
    assert corrected_terms("연금저죽 한도") == ["연금저축"]


# ════════════════════════════════════════════════════════════════
# 안전장치 — 여기를 느슨하게 고치지 말 것
# ════════════════════════════════════════════════════════════════

def test_이미_아는_용어는_건드리지_않는다():
    """정상 용어를 '교정'하면 멀쩡한 질의가 망가진다."""
    assert correct_token("세액공제") == ""
    assert correct_token("연금저축") == ""
    assert correct_token("연금실제수령연차") == ""


def test_연차_2종을_넘나드는_교정을_막는다():
    """⚠️ 최우선 안전장치.

    연금수령연차(수령한도)와 연금실제수령연차(퇴직소득세 감면율)는
    편집거리 2다. 오타로 보고 교정하면 답이 통째로 틀린다(trap B1).
    """
    assert correct_token("연금수령연차") == ""      # 이미 아는 용어
    # 교정이 일어나더라도 반대편으로 넘어가면 안 된다
    for _raw, fixed in correct_query("연금수령연차가 몇 년차인가요"):
        assert "실제" not in fixed


def test_실제수령연차_질의도_반대로_넘어가지_않는다():
    for _raw, fixed in correct_query("연금실제수령연차 알려주세요"):
        assert fixed != "연금수령연차"


def test_짧은_말은_교정하지_않는다():
    """'연금'↔'연말' 처럼 짧은 말은 한 글자만 달라도 전혀 다른 뜻이다."""
    assert correct_token("연금") == ""
    assert correct_token("세금") == ""
    assert MIN_LEN >= 3


def test_후보가_애매하면_교정하지_않는다():
    """둘 이상에 똑같이 가까우면 찍지 말아야 한다."""
    # 도메인과 무관한 말은 가까운 후보가 없거나 애매하다 → 교정 없음
    assert correct_token("아무말이나") == ""


def test_도메인_밖_단어는_교정하지_않는다():
    assert correct_token("비트코인") == ""
    assert correct_query("오늘 날씨 어때요") == []


def test_교정은_원문을_바꾸지_않는다():
    """검색어를 보탤 뿐이어야 한다 — 교정이 빗나가도 원문 검색은 살아 있다."""
    q = "세엑공제 얼마에요"
    spec = rule_based_spec(q)
    assert spec["query"] == q, "원문이 교정본으로 바뀌었다"
    assert "세액공제" in spec["search_terms"]


# ════════════════════════════════════════════════════════════════
# 파이프라인 연결 — L1이 실패해도 오타 대응이 남는가
# ════════════════════════════════════════════════════════════════

def test_규칙_경로에도_검색어가_생긴다():
    """L1 호출 실패 시 떨어지는 경로. 예전에는 여기 오타 대응이 없었다."""
    spec = rule_based_spec("세엑공제 얼마에요")
    assert spec["search_terms"] == ["세액공제"]


def test_L1_재작성이_폐기돼도_오타_교정은_남는다():
    """두 방어가 서로 대체재가 아니라 보완재여야 한다."""
    q = "세엑공제 얼마에요"
    fallback = rule_based_spec(q)
    llm = {"query": q, "intent": "세액공제", "asked_for": [],
           "search_terms": [], "source": "llm"}       # 재작성 폐기된 상태
    merged = reconcile_spec(llm, fallback, q)
    assert "세액공제" in merged["search_terms"]


def test_두_경로의_검색어가_합쳐진다():
    q = "아이알피 세엑공제"
    fallback = rule_based_spec(q)
    llm = {"query": q, "intent": "세액공제", "asked_for": [],
           "search_terms": ["개인형퇴직연금"], "source": "llm"}
    merged = reconcile_spec(llm, fallback, q)
    assert "개인형퇴직연금" in merged["search_terms"]   # L1 것
    assert "세액공제" in merged["search_terms"]         # 규칙 것


def test_검색어가_중복되지_않는다():
    q = "세엑공제"
    fallback = rule_based_spec(q)
    llm = {"query": q, "intent": "세액공제", "asked_for": [],
           "search_terms": ["세액공제"], "source": "llm"}
    merged = reconcile_spec(llm, fallback, q)
    assert merged["search_terms"].count("세액공제") == 1


def test_정상_질의는_교정_없이_지나간다():
    """오탐 회귀 — 멀쩡한 질의에 엉뚱한 검색어가 붙으면 안 된다."""
    spec = rule_based_spec("연금저축과 IRP 합쳐서 세액공제 얼마까지 되나요")
    assert spec["search_terms"] == []
