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
