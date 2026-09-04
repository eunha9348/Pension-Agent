"""답변–법령 저촉 검사 (2026-09-04 신설).

━━ 무엇이 없었는가 ━━
법령 계층은 조문을 수집해 놓고도 **답변을 조문과 대조하지 않았다.**
HCX에게 물은 것은 "이 함정이 이 질의에 적용되는가"(law_judgements)뿐이고,
그것은 질의에 대한 판정이라 답변 내용과 무관하다.

게다가 그 판정조차 `_law_context`가 trap_ids로 게이팅해, 함정이 안 잡히면
조문이 한 줄도 실리지 않았다. 298건 실측:

    함정 0건            147건 (49.0%)
    함정 있으나 앵커 0   18건 ( 6.0%)   ← 합쳐서 55%
    법령 판정 대상 존재 135건 (45.0%)

즉 **질의의 55%에서 법령 계층이 아예 돌지 않았고, 나머지 45%에서도
답변이 조문에 저촉되는지는 묻지 않았다.**

━━ 무엇을 넣었는가 ━━
1. `law/relevance.py` — 함정과 무관하게 답변 용어로 조문을 고른다 (결정론적)
2. `law_conflicts` 판정 — L6 **같은 호출 안에서** 함께 받는다 (호출 수 불변)
3. 이중 인용 검증 — 조문 인용 + 답변 인용을 **양쪽 다** 글자 그대로 대조
4. 검증 통과분만 REVISE로 상향 (상향 전용 — 단조성 유지)
5. Sub-Agent가 미해소 저촉을 critical 이상으로 감지 → 구제 재생성 개방
"""

from __future__ import annotations

import json

import pytest

from app.core.supervisory_board import (Finding, Verdict, _apply_law_conflicts,
                                        build_llm_audit_payload,
                                        law_conflicts_from_audit, supervise)
from app.law.citation_guard import parse_law_conflicts, verify_conflicts
from app.law.relevance import select_relevant_articles
from app.law.schema import LawArticle
from app.law.store import LawStore

# ── 시험용 조문 ───────────────────────────────────────────────
# 실제 소득세법 제14조 제3항의 요지를 본뜬 문장. 원문 그대로 대조하는
# 로직을 시험하는 것이 목적이므로 이 텍스트 자체가 '원문'이다.
_ART_1500 = LawArticle(
    law_name="시험소득세법", article_no="제14조", clause_no="제3항",
    text=("가목 및 나목 외의 연금소득의 합계액이 연 1천500만원 이하인 경우 "
          "그 연금소득. 다만 이연퇴직소득을 연금수령하는 경우의 연금소득은 "
          "합계액에 포함하지 아니한다."),
    effective_date="2024-01-01", source_url="", fetched_at="")

_ART_LIMIT = LawArticle(
    law_name="시험소득세법", article_no="제59조의3", clause_no="제1항",
    text=("연금저축계좌에 납입한 금액이 연 600만원을 초과하는 경우에는 그 "
          "초과하는 금액은 없는 것으로 하고, 퇴직연금계좌에 납입한 금액을 "
          "합한 금액이 연 900만원을 초과하는 경우에는 그 초과하는 금액은 "
          "없는 것으로 한다."),
    effective_date="2024-01-01", source_url="", fetched_at="")

_ART_UNRELATED = LawArticle(
    law_name="시험도로법", article_no="제3조", clause_no="",
    text="도로관리청은 도로의 구조를 보전하기 위하여 필요한 조치를 한다.",
    effective_date="2024-01-01", source_url="", fetched_at="")


@pytest.fixture()
def store() -> LawStore:
    return LawStore([_ART_1500, _ART_LIMIT, _ART_UNRELATED])


# 조문과 어긋나는 답변 — "초과분만 과세"는 제14조 제3항이 정하는 구조가 아니다
_BAD_ANSWER = ("연간 연금소득이 1,500만원을 넘으면 초과분에 대해서만 "
               "종합과세됩니다. 나머지는 그대로 분리과세됩니다.")


# ════════════════════════════════════════════════════════════
# 1. 조문 선택 — 함정 게이팅이 없는가
# ════════════════════════════════════════════════════════════

def test_함정이_없어도_관련_조문을_고른다(store):
    """★ 이 검사의 존재 이유 — 실측 55%가 여기에 걸려 있었다."""
    arts, status = select_relevant_articles(_BAD_ANSWER, store=store)
    refs = [a.ref for a in arts]
    assert "시험소득세법 제14조 제3항" in refs, status


def test_무관한_조문은_고르지_않는다(store):
    """도로법이 연금 답변의 검사 대상이 되면 안 된다."""
    arts, _ = select_relevant_articles(_BAD_ANSWER, store=store)
    assert "시험도로법 제3조" not in [a.ref for a in arts]


def test_이미_앵커로_실린_조문은_중복해서_싣지_않는다(store):
    arts, _ = select_relevant_articles(
        _BAD_ANSWER, store=store,
        exclude_refs=frozenset({"시험소득세법 제14조 제3항"}))
    assert "시험소득세법 제14조 제3항" not in [a.ref for a in arts]


def test_수집본이_비면_조문을_고르지_않는다():
    arts, status = select_relevant_articles(_BAD_ANSWER, store=LawStore([]))
    assert arts == []
    assert "비어" in status


def test_선택_결과가_실행마다_같다(store):
    """★ 조문 선택이 흔들리면 같은 질의가 매번 다른 검사를 받는다."""
    a = [x.ref for x in select_relevant_articles(_BAD_ANSWER, store=store)[0]]
    b = [x.ref for x in select_relevant_articles(_BAD_ANSWER, store=store)[0]]
    assert a == b and a, "조문 선택이 결정론적이지 않다"


def test_도메인_용어가_없으면_조문을_고르지_않는다(store):
    arts, status = select_relevant_articles("안녕하세요 반갑습니다", store=store)
    assert arts == []
    assert "도메인 용어" in status


# ════════════════════════════════════════════════════════════
# 2. 이중 인용 검증 — 두 겹 중 하나라도 뚫리면 안 된다
# ════════════════════════════════════════════════════════════

def _claim(ref="시험소득세법 제14조 제3항",
           quote="연금소득의 합계액이 연 1천500만원 이하인 경우",
           span="초과분에 대해서만 종합과세됩니다",
           conflict="조문은 합계액 전체를 대상으로 한다"):
    return parse_law_conflicts([{
        "law_ref": ref, "quote": quote,
        "answer_span": span, "conflict": conflict}])


def test_양쪽_인용이_실재하면_채택된다(store):
    kept, trace = verify_conflicts(store, _BAD_ANSWER, _claim())
    assert len(kept) == 1, trace
    assert kept[0].verified and kept[0].span_ok


def test_지어낸_조문_인용은_폐기된다(store):
    """차단선 ① — 조문 쪽."""
    kept, trace = verify_conflicts(
        store, _BAD_ANSWER,
        _claim(quote="연금소득은 전액을 종합과세 대상으로 삼는다"))
    assert kept == []
    assert any("폐기" in t for t in trace)


def test_저장소에_없는_조문은_폐기된다(store):
    kept, _ = verify_conflicts(store, _BAD_ANSWER,
                               _claim(ref="시험소득세법 제999조"))
    assert kept == []


def test_답변에_없는_문장을_지목하면_폐기된다(store):
    """★ 차단선 ② — 이게 없으면 멀쩡한 답변이 강제 재생성된다.

    조문은 정확히 인용해 놓고 답변이 하지도 않은 말을 저촉이라고
    지어내는 경우다. 결정론 계층의 판정은 LLM이 완화하지 못하므로
    (단조성), 이 오탐은 되돌릴 수 없는 강제 강등이 된다.
    """
    kept, trace = verify_conflicts(
        store, _BAD_ANSWER,
        _claim(span="1,500만원을 넘으면 전액이 종합과세됩니다"))
    assert kept == []
    assert any("답변에 그대로 존재하지 않음" in t for t in trace)


def test_지목_문장이_너무_짧으면_폐기된다(store):
    kept, trace = verify_conflicts(store, _BAD_ANSWER, _claim(span="과세"))
    assert kept == []
    assert any("짧아" in t for t in trace)


def test_공백_차이는_흡수한다(store):
    """줄바꿈·들여쓰기까지 불일치로 보면 정탐을 전부 잃는다."""
    kept, _ = verify_conflicts(
        store, _BAD_ANSWER,
        _claim(quote="연금소득의   합계액이 연\n1천500만원 이하인 경우"))
    assert len(kept) == 1


def test_저장소가_비면_저촉_검사를_하지_않는다():
    kept, trace = verify_conflicts(LawStore([]), _BAD_ANSWER, _claim())
    assert kept == []
    assert any("비어 있어" in t for t in trace)


# ── 제공 문서 우선 (과제 안내 6페이지) ──────────────────────
#
# "기본 연금제도 자료에 한해 외부데이터 수집이 가능합니다. 단, **제공자료가
#  최종근거이며, 외부정보는 보조로만 쓰고 상충 시 제공자료 우선**"
#
# 법령은 법제처에서 수집한 외부 자료다. 조문과 제공 문서가 어긋날 때
# 조문을 이유로 답변을 고치라고 하면 이 규칙을 정면으로 위반한다.
#
# 저촉을 채택하는 경우는 하나뿐이다:
#   **제공 문서와도 맞지 않고 법령과도 맞지 않을 때.**

def test_제공문서가_뒷받침하면_저촉을_폐기한다(store):
    """★ 이 검사의 핵심 — 외부 법령이 제공 자료를 뒤집지 못한다."""
    evidence = ["연간 연금소득이 1,500만원을 넘으면 초과분에 대해서만 "
                "종합과세됩니다. 나머지는 그대로 분리과세됩니다."]
    kept, trace = verify_conflicts(store, _BAD_ANSWER, _claim(),
                                   evidence_texts=evidence)
    assert kept == [], "제공 문서가 뒷받침하는 서술을 조문으로 뒤집었다"
    assert any("제공 문서 우선" in t for t in trace), trace


def test_제공문서와도_법령과도_안_맞으면_채택한다(store):
    """★ 사용자 지정 규칙의 나머지 절반 — 이때는 명확히 수정한다."""
    evidence = ["연금저축 가입자격은 제한이 없습니다."]   # 전혀 다른 주제
    kept, trace = verify_conflicts(store, _BAD_ANSWER, _claim(),
                                   evidence_texts=evidence)
    assert len(kept) == 1, trace
    assert "제공 문서와도 어긋남" in " ".join(trace)


def test_근거가_0건이면_저촉을_그대로_본다(store):
    """뒷받침할 제공 문서 자체가 없으면 우선순위를 적용할 대상이 없다."""
    kept, _ = verify_conflicts(store, _BAD_ANSWER, _claim(), evidence_texts=[])
    assert len(kept) == 1


def test_지목문장이_근거에_그대로_있으면_폐기한다(store):
    """가장 강한 뒷받침 신호 — 원문 그대로 존재."""
    evidence = ["안내드립니다. 초과분에 대해서만 종합과세됩니다. 참고하십시오."]
    kept, trace = verify_conflicts(store, _BAD_ANSWER, _claim(),
                                   evidence_texts=evidence)
    assert kept == []
    assert any("그대로 존재" in t for t in trace)


def test_수치가_근거에_없으면_제공문서_미뒷받침이다(store):
    """지목 문장의 수치가 제공 문서에 없으면 제공 문서와도 어긋난 것이다."""
    from app.law.citation_guard import corpus_supports_span

    ok, why = corpus_supports_span(
        "연금소득 3,700만원까지 분리과세됩니다",
        ["연간 연금소득 1,500만원 이하는 분리과세 대상입니다."])
    assert not ok
    assert "3700" in why.replace(",", "") or "3700.0" in why


def test_수치가_전부_근거에_있으면_뒷받침이다():
    from app.law.citation_guard import corpus_supports_span

    ok, _ = corpus_supports_span(
        "연금소득 1,500만원까지 분리과세됩니다",
        ["연간 연금소득 1,500만원 이하인 경우 분리과세 대상입니다."])
    assert ok


def test_수치_주장이_없으면_같은_주제_근거로_판단한다():
    """★ 수치가 없으면 조문과의 어긋남을 결정론적으로 말할 수 없다.
    그 판단은 의미 감사와 함정 규칙의 몫이다."""
    from app.law.citation_guard import corpus_supports_span

    ok, why = corpus_supports_span(
        "연금소득은 분리과세를 선택할 수 있습니다",
        ["연금소득의 분리과세 선택에 관한 안내입니다. 연금수령 시 적용됩니다."])
    assert ok
    assert "결정론적으로 판정할 수 없음" in why


def test_무관한_근거만_있으면_뒷받침이_아니다():
    from app.law.citation_guard import corpus_supports_span

    ok, _ = corpus_supports_span(
        "연금소득은 분리과세를 선택할 수 있습니다",
        ["도로관리청은 도로의 구조를 보전하여야 합니다."])
    assert not ok


def test_필수_필드가_빠진_주장은_파싱에서_버려진다():
    assert parse_law_conflicts([{"law_ref": "x", "quote": "y"}]) == []
    assert parse_law_conflicts([{"answer_span": "z"}]) == []
    assert parse_law_conflicts("배열이 아님") == []


# ════════════════════════════════════════════════════════════
# 3. 판정 반영 — 상향 전용인가
# ════════════════════════════════════════════════════════════

def _raw(conflicts):
    return json.dumps({"verdict": "APPROVE", "findings": [],
                       "law_conflicts": conflicts}, ensure_ascii=False)


def test_검증된_저촉은_REVISE로_올린다(store, monkeypatch):
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    det = supervise(_BAD_ANSWER)
    det.verdict = Verdict.APPROVE

    out, trace, kept = _apply_law_conflicts(
        _raw([{"law_ref": "시험소득세법 제14조 제3항",
               "quote": "연금소득의 합계액이 연 1천500만원 이하인 경우",
               "answer_span": "초과분에 대해서만 종합과세됩니다",
               "conflict": "조문은 합계액 전체를 대상으로 한다"}]),
        _BAD_ANSWER, det)

    assert out.verdict == Verdict.REVISE, trace
    assert len(kept) == 1
    assert any(f.code == "LAW_CONFLICT" for f in out.findings)


def test_저촉_지적에_조문_인용이_담긴_시정지시가_붙는다(store, monkeypatch):
    """★ 지시가 없으면 재생성이 무엇을 고쳐야 할지 모른다."""
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    det = supervise(_BAD_ANSWER)
    out, _, _ = _apply_law_conflicts(
        _raw([{"law_ref": "시험소득세법 제14조 제3항",
               "quote": "연금소득의 합계액이 연 1천500만원 이하인 경우",
               "answer_span": "초과분에 대해서만 종합과세됩니다",
               "conflict": "조문은 합계액 전체를 대상으로 한다"}]),
        _BAD_ANSWER, det)

    directive = next(f.directive for f in out.findings
                     if f.code == "LAW_CONFLICT")
    assert "제14조" in directive
    assert "연금소득의 합계액이 연 1천500만원 이하인 경우" in directive
    assert directive in out.directives


def test_저촉_없음_판정은_아무것도_완화하지_않는다(store, monkeypatch):
    """★ 단조성 — LLM의 '문제없음'이 결정론적 판정을 무르면 안 된다."""
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    det = supervise(_BAD_ANSWER)
    det.verdict = Verdict.BLOCK

    out, _, kept = _apply_law_conflicts(_raw([]), _BAD_ANSWER, det)
    assert out.verdict == Verdict.BLOCK, "저촉 없음 판정이 BLOCK을 완화했다"
    assert kept == []


def test_이미_BLOCK이면_REVISE로_내리지_않는다(store, monkeypatch):
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    det = supervise(_BAD_ANSWER)
    det.verdict = Verdict.BLOCK

    out, _, kept = _apply_law_conflicts(
        _raw([{"law_ref": "시험소득세법 제14조 제3항",
               "quote": "연금소득의 합계액이 연 1천500만원 이하인 경우",
               "answer_span": "초과분에 대해서만 종합과세됩니다",
               "conflict": "어긋남"}]),
        _BAD_ANSWER, det)
    assert out.verdict == Verdict.BLOCK
    assert len(kept) == 1, "채택 자체는 되어야 한다 (기록이 남아야 함)"


def test_폐기된_저촉은_판정을_바꾸지_않는다(store, monkeypatch):
    monkeypatch.setattr("app.law.store.get_store", lambda *a, **k: store)
    det = supervise(_BAD_ANSWER)
    det.verdict = Verdict.APPROVE

    out, trace, kept = _apply_law_conflicts(
        _raw([{"law_ref": "시험소득세법 제14조 제3항",
               "quote": "내가 지어낸 조문 문장입니다 정말로",
               "answer_span": "초과분에 대해서만 종합과세됩니다",
               "conflict": "어긋남"}]),
        _BAD_ANSWER, det)
    assert out.verdict == Verdict.APPROVE
    assert kept == []
    assert any("폐기" in t for t in trace)


def test_law_conflicts가_감사_응답에서_추출된다():
    got = law_conflicts_from_audit(_raw([
        {"law_ref": "A", "quote": "B", "answer_span": "C", "conflict": "D"}]))
    assert len(got) == 1 and got[0].law_ref == "A"


def test_저촉_검사가_터져도_감사는_계속된다(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("저장소 폭발")
    monkeypatch.setattr("app.law.citation_guard.verify_conflicts", boom)
    det = supervise(_BAD_ANSWER)
    before = det.verdict

    out, trace, kept = _apply_law_conflicts(
        _raw([{"law_ref": "A", "quote": "B" * 20,
               "answer_span": "초과분에 대해서만 종합과세됩니다", "conflict": "x"}]),
        _BAD_ANSWER, det)
    assert out.verdict == before
    assert kept == []
    assert any("실패" in t for t in trace), "실패 사실이 기록되지 않았다"


# ════════════════════════════════════════════════════════════
# 4. 페이로드 — 감사자에게 실제로 질문이 전달되는가
# ════════════════════════════════════════════════════════════

def test_조문이_있으면_저촉_판정을_요구한다():
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="1,500만원 넘으면 어떻게 되나요?",
        law_articles=[_ART_1500])
    assert "law_conflicts" in p
    assert "answer_span" in p
    assert "저촉" in p


def test_조문이_없으면_저촉_판정을_요구하지_않는다():
    """묻지 말아야 할 때 물으면 근거 없는 판정이 돌아온다."""
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="질문", law_articles=[])
    assert "law_conflicts" not in p


def test_함정이_없어도_저촉_판정을_요구한다():
    """★ candidate_traps 없이 조문만 있어도 물어야 한다."""
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="질문", law_articles=[_ART_1500], candidate_traps=None)
    assert "law_conflicts" in p
    assert "law_judgements" not in p, "판정 대상 함정이 없는데 함정 판정을 요구했다"


def test_애매하면_내지_말라는_지시가_있다():
    """오탐이 미탐보다 나쁘다 — 프롬프트에서도 그렇게 요구한다."""
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="질문", law_articles=[_ART_1500])
    assert "애매하면" in p and "빈 배열" in p


def test_제공문서가_최종근거임을_명시한다():
    """★ 과제 안내 6페이지: '외부 정보는 보조로만 쓰고 상충 시 제공자료
    우선'. 조문(외부 수집)과 표현이 다르다는 이유만으로 제공 문서 기반
    서술을 저촉으로 잘못 내지 않도록, 우선순위를 프롬프트에 명시한다."""
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="질문", law_articles=[_ART_1500])
    assert "제공 문서" in p and "최종 근거" in p


def test_긴_조문은_답변_용어_주변을_실어_보낸다():
    """★ 함정이 없으면 verify_any가 비어 머리말만 실리던 자리."""
    long_art = LawArticle(
        law_name="시험소득세법", article_no="제14조", clause_no="제3항",
        text=("머리말 " * 400 +
              "연금소득의 합계액이 연 1천500만원 이하인 경우 그 연금소득."),
        effective_date="2024-01-01", source_url="", fetched_at="")
    p = build_llm_audit_payload(
        answer=_BAD_ANSWER, evidence_texts=[], calc_results=[],
        question="질문", law_articles=[long_art],
        focus_terms=["연금소득", "종합과세"])
    assert "1천500만원" in p, "초점을 못 잡아 정작 필요한 조항이 잘려 나갔다"
