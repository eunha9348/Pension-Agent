"""법령 접지 계층 회귀 테스트.

이 파일이 지키는 것은 하나다 — **지어낸 근거가 통과하지 못한다.**
그래서 정상 경로보다 공격 경로(의역·창작·짧은 인용·없는 조문)를 더
촘촘히 못 박는다. 여기가 뚫리면 나머지 설계는 의미가 없다.
"""

from __future__ import annotations

import pytest

from app.law.citation_guard import (apply_to_traps, parse_law_judgements,
                                    verify_citation, verify_judgements)
from app.law.schema import LawArticle, LawJudgement
from app.law.store import LawStore, canon_ref

# 실제 조문 형식을 흉내낸 픽스처. 내용은 검증 로직 시험용이며
# 법령 사실을 주장하지 않는다 — 문자열 대조가 목적이다.
_A1 = LawArticle(
    law_name="시험법", article_no="제10조", clause_no="제1항",
    text="가입자가 이 조에 따라 적립금을 중도인출하는 경우에는 "
         "대통령령으로 정하는 사유에 해당하여야 한다.",
    effective_date="2024-01-01",
    source_url="https://example.invalid/1", fetched_at="2026-08-27T00:00:00+00:00")
_A2 = LawArticle(
    law_name="시험법", article_no="제20조", clause_no="",
    text="연금계좌에 납입한 금액에 대하여는 종합소득산출세액에서 공제한다.",
    effective_date="2024-01-01",
    source_url="https://example.invalid/2", fetched_at="2026-08-27T00:00:00+00:00")


@pytest.fixture
def store() -> LawStore:
    return LawStore([_A1, _A2])


# ════════════════════════════════════════════════════════════════
# 인용 검증 — 정상 경로
# ════════════════════════════════════════════════════════════════

def test_원문_그대로_인용하면_통과한다(store):
    c = verify_citation(store, "시험법 제10조 제1항",
                        "대통령령으로 정하는 사유에 해당하여야 한다")
    assert c.ok
    assert c.article.ref == "시험법 제10조 제1항"


def test_공백_차이는_통과한다(store):
    """줄바꿈·들여쓰기 차이로 정탐을 잃으면 안 된다."""
    c = verify_citation(store, "시험법 제10조 제1항",
                        "대통령령으로   정하는\n사유에 해당하여야 한다")
    assert c.ok


def test_항을_빠뜨려도_조_단위로_찾는다(store):
    """LLM이 항 표기를 흘리는 일이 잦다. 원문이 실재하면 통과가 맞다."""
    c = verify_citation(store, "시험법 제10조",
                        "대통령령으로 정하는 사유에 해당하여야 한다")
    assert c.ok


@pytest.mark.parametrize("ref", [
    "시험법 제10조 제1항", "시험법제10조제1항", "시험법 10조 1항",
])
def test_조문_표기_흔들림을_흡수한다(store, ref):
    c = verify_citation(store, ref, "대통령령으로 정하는 사유에 해당하여야 한다")
    assert c.ok, ref


# ════════════════════════════════════════════════════════════════
# 인용 검증 — 공격 경로 ★ 여기가 핵심
# ════════════════════════════════════════════════════════════════

def test_없는_조문을_대면_폐기된다(store):
    c = verify_citation(store, "시험법 제999조",
                        "대통령령으로 정하는 사유에 해당하여야 한다")
    assert not c.ok
    assert "없는 조문" in c.reason


def test_조문은_맞지만_내용을_지어내면_폐기된다(store):
    """가장 위험한 형태 — 실재하는 조문에 없는 말을 붙이는 것."""
    c = verify_citation(store, "시험법 제10조 제1항",
                        "중도인출 시 기타소득세 16.5퍼센트를 원천징수한다")
    assert not c.ok
    assert "그대로 존재하지 않음" in c.reason


def test_의역하면_폐기된다(store):
    """어미·조사만 바꾼 의역. 유사도 비교로 바꾸면 이게 통과해 버린다."""
    c = verify_citation(store, "시험법 제10조 제1항",
                        "대통령령이 정하는 사유에 해당해야 합니다")
    assert not c.ok


def test_짧은_인용은_폐기된다(store):
    """'중도인출' 같은 짧은 말은 어느 조문에나 있어 대조가 무의미하다."""
    c = verify_citation(store, "시험법 제10조 제1항", "중도인출")
    assert not c.ok
    assert "너무 짧아" in c.reason


def test_빈_인용과_빈_참조는_폐기된다(store):
    assert not verify_citation(store, "", "대통령령으로 정하는 사유에 해당하여야 한다").ok
    assert not verify_citation(store, "시험법 제10조", "").ok


def test_다른_조문의_원문을_붙이면_폐기된다(store):
    """제20조 본문을 제10조 근거라고 우기는 경우."""
    c = verify_citation(store, "시험법 제10조 제1항",
                        "연금계좌에 납입한 금액에 대하여는")
    assert not c.ok


# ════════════════════════════════════════════════════════════════
# 판정 검증 · 반영
# ════════════════════════════════════════════════════════════════

def test_검증_실패한_판정은_반영되지_않고_기록에_남는다(store):
    js = [
        LawJudgement("A1", True, "시험법 제10조 제1항",
                     "대통령령으로 정하는 사유에 해당하여야 한다"),
        LawJudgement("C4", True, "시험법 제999조", "있지도 않은 조문의 인용문입니다"),
    ]
    kept, trace = verify_judgements(store, js)
    assert [j.trap_id for j in kept] == ["A1"]
    assert any("C4" in t and "폐기" in t for t in trace)


def test_저장소가_비면_판정을_아예_수행하지_않는다():
    """수집 전에는 법령 근거 판정이 꺼져 있어야 한다."""
    kept, trace = verify_judgements(LawStore([]), [
        LawJudgement("A1", True, "시험법 제10조", "대통령령으로 정하는 사유에")])
    assert kept == []
    assert any("비어 있어" in t for t in trace)


def test_검증된_판정은_함정을_추가한다(store):
    kept, _ = verify_judgements(store, [
        LawJudgement("A1", True, "시험법 제10조 제1항",
                     "대통령령으로 정하는 사유에 해당하여야 한다")])
    ids, trace = apply_to_traps(["C4"], kept)
    assert set(ids) == {"C4", "A1"}
    assert any("A1 추가" in t for t in trace)


def test_검증된_판정은_함정을_제거할_수_있다(store):
    """'인용 검증부 전권' — 조문에 근거가 실재할 때만 주어지는 권한이다."""
    kept, _ = verify_judgements(store, [
        LawJudgement("A1", False, "시험법 제10조 제1항",
                     "대통령령으로 정하는 사유에 해당하여야 한다")])
    ids, trace = apply_to_traps(["A1", "C4"], kept)
    assert ids == ["C4"]
    assert any("A1 제거" in t for t in trace)


def test_상향전용_모드는_제거를_기각한다(store):
    kept, _ = verify_judgements(store, [
        LawJudgement("A1", False, "시험법 제10조 제1항",
                     "대통령령으로 정하는 사유에 해당하여야 한다")])
    ids, trace = apply_to_traps(["A1"], kept, authority="escalate_only")
    assert ids == ["A1"]
    assert any("기각" in t for t in trace)


def test_검증_실패한_제거요청은_함정을_지우지_못한다(store):
    """공격 시나리오 — 지어낸 근거로 critical 함정을 무력화하려는 경우."""
    kept, _ = verify_judgements(store, [
        LawJudgement("A1", False, "시험법 제10조 제1항",
                     "중도인출은 언제나 자유롭게 허용된다")])   # 창작
    ids, _ = apply_to_traps(["A1"], kept)
    assert ids == ["A1"], "지어낸 근거로 함정이 제거됐다 — 차단선이 뚫렸다"


# ════════════════════════════════════════════════════════════════
# 파싱 · 저장소
# ════════════════════════════════════════════════════════════════

def test_판정_파싱은_형식이_깨진_항목을_버린다():
    js = parse_law_judgements([
        {"trap_id": "A1", "applies": True, "law_ref": "x", "quote": "y"},
        {"applies": True},          # trap_id 없음
        "문자열",                    # dict 아님
    ])
    assert [j.trap_id for j in js] == ["A1"]


def test_판정_파싱은_배열이_아니면_빈_목록이다():
    assert parse_law_judgements(None) == []
    assert parse_law_judgements({"trap_id": "A1"}) == []


def test_원문이_바뀌면_적재가_실패한다():
    """저장본 손상을 조용히 넘기면 정상 인용이 억울하게 기각된다."""
    d = _A1.to_dict()
    d["text"] = d["text"] + " 조작된 문장"
    with pytest.raises(ValueError, match="저장 시점과 다릅니다"):
        LawArticle.from_dict(d)


def test_저장소_왕복이_원문을_보존한다(tmp_path):
    p = tmp_path / "articles.json"
    LawStore.save([_A1, _A2], p)
    back = LawStore.load(p)
    assert len(back) == 2
    assert back.get("시험법 제10조 제1항").text == _A1.text


def test_저장소_파일이_없으면_빈_저장소다(tmp_path):
    """수집 전에도 시스템은 기동해야 한다."""
    s = LawStore.load(tmp_path / "없는파일.json")
    assert s.is_empty and len(s) == 0


def test_손상된_저장소는_조용히_비지_않는다(tmp_path):
    p = tmp_path / "articles.json"
    p.write_text("{망가진 JSON", encoding="utf-8")
    with pytest.raises(RuntimeError, match="읽지 못했습니다"):
        LawStore.load(p)


def test_참조_표준화는_의미를_바꾸지_않는다():
    assert canon_ref("소득세법 제59조의3 제1항") == canon_ref("소득세법제59조의3제1항")
    assert canon_ref("소득세법 제59조") != canon_ref("소득세법 제60조")


# ════════════════════════════════════════════════════════════════
# 실수집본(7,426건)에서 확인된 형태
# ════════════════════════════════════════════════════════════════

def test_원문자_항번호가_일반숫자_인용과_매칭된다():
    """법제처 원문은 항번호를 ①로 싣는데 LLM은 '제1항'으로 인용한다.

    NFKC가 ①→1로 정규화해 주므로 일치해야 한다. 이게 깨지면 항이 있는
    조문 전체(수집본의 대부분)가 인용 검증을 통과하지 못한다.
    """
    circled = LawArticle(
        law_name="소득세법", article_no="제1조의2", clause_no="제①항",
        text="① 이 법에서 사용하는 용어의 뜻은 다음과 같다.",
        effective_date="2026-01-01", source_url="u", fetched_at="t")
    store = LawStore([circled])

    c = verify_citation(store, "소득세법 제1조의2 제1항",
                        "이 법에서 사용하는 용어의 뜻은 다음과 같다")
    assert c.ok, "원문자 항번호가 일반숫자 인용과 매칭되지 않는다"


def test_같은_참조가_둘이면_둘_다_후보가_된다():
    """7천 건 규모에서는 같은 참조가 둘 이상 나온다(부칙·개정 이력).

    첫 건만 후보로 삼으면, 뒤쪽 조문에 실재하는 인용이 '원문에 없음'으로
    기각된다. 정탐을 잃는 조용한 실패라 눈에 띄지 않는다.
    """
    dup_a = LawArticle("시험법", "제5조", "", "앞쪽 조문의 고유한 문장입니다.",
                       "2026-01-01", "u", "t")
    dup_b = LawArticle("시험법", "제5조", "", "뒤쪽 조문의 고유한 문장입니다.",
                       "2026-01-01", "u", "t")
    store = LawStore([dup_a, dup_b])

    assert len(store.get_candidates("시험법 제5조")) == 2
    # 뒤쪽 조문의 원문을 인용해도 통과해야 한다
    assert verify_citation(store, "시험법 제5조", "뒤쪽 조문의 고유한 문장입니다").ok
    assert verify_citation(store, "시험법 제5조", "앞쪽 조문의 고유한 문장입니다").ok
    # 어느 쪽에도 없는 문장은 여전히 폐기된다
    assert not verify_citation(store, "시험법 제5조", "어디에도 없는 문장입니다").ok


# ════════════════════════════════════════════════════════════════
# 앵커 — 추측으로 채워지지 않았는지
# ════════════════════════════════════════════════════════════════

def test_앵커는_확인_전까지_비어_있다():
    """조문 번호를 기억으로 적으면 틀렸을 때 알 방법이 없다.

    수집·확인을 거친 앵커만 등재한다. 이 테스트가 깨진다면 ANCHORS에
    실제 수집본으로 확인한 항목이 들어온 것이므로, --verify 가 통과하는지
    확인한 뒤 이 테스트를 갱신할 것.
    """
    from app.law.anchors import ANCHORS, law_backed

    assert ANCHORS == {}, "확인되지 않은 앵커가 등재됐습니다"
    assert not law_backed("C4")


def test_탐색어는_조문번호를_주장하지_않는다():
    """SEARCH_TERMS는 검색 질의여야 한다 — 조문 번호가 들어가면 주장이 된다."""
    import re

    from app.law.anchors import SEARCH_TERMS

    for tid, terms in SEARCH_TERMS.items():
        for t in terms:
            assert not re.search(r'제?\d+조', t), f"{tid}의 탐색어에 조문번호: {t}"
