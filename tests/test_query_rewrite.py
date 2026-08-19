"""F단계 · L1 질의 정규화(오타·줄임말) 회귀.

━━ 왜 L1에 붙였나 ━━
사용자는 구어체로 묻고 오타도 낸다("세엑공제", "아이알피", "깨면 손해").
문서는 법령체다("세액공제", "개인형퇴직연금", "중도해지"). BM25는 토큰이
정확히 일치해야 걸리므로, 이 격차를 메우지 않으면 검색이 아예 실패한다.

동의어 사전(vocab.py)이 일부를 처리하지만 27개 그룹에 없는 표현은 무력하다.
그래서 L1이 **이미 HyperCLOVA X로 원문을 읽고 L3보다 먼저 돈다**는 점을 이용해,
같은 호출에서 검색어까지 받아 온다 — LLM 호출은 여전히 3개소(L1·L5'·L6)다.

━━ 이 파일이 지키는 것 ━━
재작성은 **보조 신호**여야 하고, **구분해야 할 용어를 뭉개면 안 된다.**
후자가 훨씬 중요하다 — 연금수령연차와 연금실제수령연차를 합치면
퇴직소득세 감면율이 통째로 틀린다(trap B1).
"""

from __future__ import annotations

from app.analysis.query_spec import (MAX_SEARCH_TERMS, QUERY_SPEC_TOOL,
                                     sanitize_search_terms)
from app.analysis.vocab import DISTINCT_PAIRS, conflates_distinct_terms


# ════════════════════════════════════════════════════════════════
# 스키마 — L1이 검색어를 낼 수 있는가
# ════════════════════════════════════════════════════════════════

def test_L1_스키마에_검색어_필드가_있다():
    props = QUERY_SPEC_TOOL[0]["function"]["parameters"]["properties"]
    assert "search_terms" in props


def test_스키마가_용어_합치기를_금지한다():
    """설명에 경고가 없으면 LLM이 한 글자 차이를 오타로 오인한다."""
    desc = QUERY_SPEC_TOOL[0]["function"]["parameters"]["properties"]["search_terms"]["description"]
    assert "연금실제수령연차" in desc


def test_LLM_호출은_여전히_3개소다():
    """검색어를 얻자고 호출을 하나 더 늘리면 지연이 그만큼 늘어난다.
    L1이 L3보다 먼저 돈다는 사실을 이용해 기존 호출에 얹은 것이 핵심이다."""
    import app.pipeline as p
    src = open(p.__file__, encoding="utf-8").read()
    # 질의 재작성 전용 호출을 새로 만들지 않았는지
    assert "l1_rewrite" not in src
    assert "purpose=\"l5_regenerate\"" in src      # 기존 재생성 경로는 유지


# ════════════════════════════════════════════════════════════════
# 정상 동작 — 오타·줄임말이 문서 용어로 옮겨진다
# ════════════════════════════════════════════════════════════════

def test_정상_검색어는_그대로_통과한다():
    out = sanitize_search_terms(
        ["IRP", "개인형퇴직연금", "세액공제"], "아이알피 세엑공제 얼마에요")
    assert out == ["IRP", "개인형퇴직연금", "세액공제"]


def test_중복과_공백은_정리된다():
    out = sanitize_search_terms(["IRP", " IRP ", "세액공제"], "질의")
    assert out == ["IRP", "세액공제"]


def test_너무_짧거나_긴_항목은_버린다():
    out = sanitize_search_terms(["가", "세액공제", "가" * 50], "질의")
    assert out == ["세액공제"]


def test_개수_상한이_있다():
    """검색어가 너무 많으면 원 질의가 묻힌다."""
    out = sanitize_search_terms([f"용어{i}" for i in range(30)], "질의")
    assert len(out) == MAX_SEARCH_TERMS


def test_비어_있어도_안전하다():
    assert sanitize_search_terms(None, "질의") == []
    assert sanitize_search_terms([], "질의") == []


# ════════════════════════════════════════════════════════════════
# 안전장치 — 구분해야 할 용어를 뒤바꾸면 통째로 버린다
# ════════════════════════════════════════════════════════════════

def test_연차_2종을_뒤바꾸면_폐기한다():
    """⚠️ 이 테스트를 느슨하게 고치지 말 것.

    연금실제수령연차는 퇴직소득세 감면율을, 연금수령연차는 연금수령한도를
    결정한다. 한 글자 차이라 LLM이 오타로 오인하기 쉽지만, 뒤바뀌면
    답이 통째로 틀린다(trap_rules B1 — critical).
    """
    질의 = "연금실제수령연차가 몇 년차인지 어떻게 세나요"
    폐기됨 = sanitize_search_terms(["연금수령연차", "산정방법"], 질의)
    assert 폐기됨 == [], "구분해야 할 용어가 뒤바뀐 재작성이 통과했다"


def test_반대_방향도_막는다():
    질의 = "연금수령연차 기준으로 한도가 얼마인가요"
    assert sanitize_search_terms(["연금실제수령연차"], 질의) == []


def test_같은_쪽으로_다시_쓰는_것은_허용한다():
    """'실제수령연차'를 정식 명칭으로 펴 주는 것은 정상 동작이다."""
    질의 = "실제수령연차가 뭔가요"
    out = sanitize_search_terms(["연금실제수령연차", "퇴직소득세 감면"], 질의)
    assert "연금실제수령연차" in out


def test_두_개념을_모두_묻는_비교_질의는_통과한다():
    """오탐 회귀 — 비교 질의까지 막으면 정작 필요한 검색이 죽는다."""
    질의 = "연금수령연차랑 연금실제수령연차 뭐가 달라요"
    out = sanitize_search_terms(["연금수령연차", "연금실제수령연차"], 질의)
    assert len(out) == 2


def test_인출_사유_2종도_보호한다():
    """근퇴법상 중도인출 사유와 세법상 부득이한 사유는 다르다(trap A1)."""
    질의 = "부득이한 사유로 인출하면 세금이 어떻게 되나요"
    assert sanitize_search_terms(["중도인출", "세율"], 질의) == []


def test_과세_범위도_보호한다():
    """1,500만원 초과 시 '초과분'이 아니라 '전액'이 대상이다(trap C1)."""
    질의 = "1500만원 넘으면 전액이 과세 대상인가요"
    assert sanitize_search_terms(["초과분 과세"], 질의) == []


def test_보호_대상_쌍이_비어_있지_않다():
    """쌍 정의가 사라지면 안전장치가 조용히 무력해진다."""
    assert len(DISTINCT_PAIRS) >= 4
    for name, a, b in DISTINCT_PAIRS:
        assert name and a and b


def test_무관한_재작성은_영향을_받지_않는다():
    assert conflates_distinct_terms("세액공제 얼마", ["세액공제", "한도"]) == ""


# ════════════════════════════════════════════════════════════════
# 검색 반영 — 보조 신호여야 한다
# ════════════════════════════════════════════════════════════════

def test_재작성은_원_질의를_대체하지_않는다():
    """재작성이 빗나가도 원 질의로 찾은 근거가 남아 있어야 한다."""
    from app.retrieval.hybrid import EXPANSION_WEIGHT, REWRITE_WEIGHT
    assert REWRITE_WEIGHT < 1.0, "원 질의(1.0)보다 낮아야 한다"
    assert REWRITE_WEIGHT > EXPANSION_WEIGHT, \
        "사전에 없는 표현까지 옮겨 준 신호라 동의어 확장보다는 높게"


def test_검색어가_없어도_검색이_동작한다():
    """L1이 실패했거나 재작성이 폐기된 경우 — 흔한 경로다."""
    from app.ingest.store import get_store
    from app.retrieval.hybrid import make_retrieve_hybrid

    retrieve = make_retrieve_hybrid(get_store())
    assert retrieve({"query": "연금저축 세액공제 한도"}) != []


def test_검색어가_있으면_그것도_반영된다():
    from app.ingest.store import get_store
    from app.retrieval.hybrid import make_retrieve_hybrid

    retrieve = make_retrieve_hybrid(get_store())
    spec = {"query": "세엑공제 얼마", "search_terms": ["세액공제", "한도"]}
    assert retrieve(spec) != [], "재작성 검색어로도 근거를 찾지 못했다"
