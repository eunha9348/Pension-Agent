"""감독 계층 테스트 — 권한 계층(단조성)이 핵심.

━━ 왜 단조성이 전부인가 ━━
L6에서 HyperCLOVA X는 의미 감사를 맡되 **심각도를 올릴 수만 있다.**
LLM이 APPROVE라고 해도 결정론적 REVISE는 REVISE로 남아야 한다.
이 성질이 깨지면 L6 전체가 무의미해진다 — LLM이 자기 답변을 통과시키는
구조가 되기 때문이다. 리팩터링 시 이 테스트가 방어선이다.
"""

from __future__ import annotations

import pytest

from app.core.supervisory_board import (Finding, SupervisionResult, Verdict,
                                        audit_plan, build_remediation_prompt,
                                        merge_supervision, parse_llm_audit,
                                        supervise, supervise_plan)

DET_VERDICTS = [Verdict.APPROVE, Verdict.REVISE, Verdict.DOWNGRADE, Verdict.BLOCK]
_ORDER = {Verdict.APPROVE: 0, Verdict.REVISE: 1, Verdict.DOWNGRADE: 2, Verdict.BLOCK: 3}


# ════════════════════════════════════════════════════════════════
# 권한 계층 (단조성)
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("det", DET_VERDICTS)
@pytest.mark.parametrize("llm", DET_VERDICTS)
def test_LLM은_심각도를_올릴_수만_있다(det, llm):
    """어떤 조합에서도 최종 판정은 결정론적 판정보다 약해지지 않는다."""
    merged = merge_supervision(SupervisionResult(det), llm, [])
    assert _ORDER[merged.verdict] >= _ORDER[det]
    assert _ORDER[merged.verdict] == max(_ORDER[det], _ORDER[llm])


def test_결정론_REVISE는_LLM_APPROVE로_완화되지_않는다():
    merged = merge_supervision(SupervisionResult(Verdict.REVISE), Verdict.APPROVE, [])
    assert merged.verdict == Verdict.REVISE


def test_결정론_BLOCK은_무엇으로도_풀리지_않는다():
    for llm in DET_VERDICTS:
        assert merge_supervision(SupervisionResult(Verdict.BLOCK), llm, []).verdict \
            == Verdict.BLOCK


def test_LLM이_더_엄격하면_상향된다():
    merged = merge_supervision(SupervisionResult(Verdict.APPROVE), Verdict.BLOCK,
                               [Finding("의미감사", "X", Verdict.BLOCK, "문제", "고칠 것")])
    assert merged.verdict == Verdict.BLOCK
    assert merged.findings


def test_확인항목은_최대_2건으로_제한된다():
    merged = merge_supervision(SupervisionResult(Verdict.APPROVE), Verdict.APPROVE, [],
                               llm_questions=["a", "b", "c", "d"])
    assert len(merged.revised_ask_back) <= 2


# ════════════════════════════════════════════════════════════════
# LLM 감사 응답 파싱
# ════════════════════════════════════════════════════════════════

def test_감사응답_파싱():
    raw = '{"verdict":"REVISE","findings":[{"code":"의미 정합성",' \
          '"detail":"연차 혼동","directive":"구분해 서술"}],' \
          '"most_critical_questions":["연금실제수령연차"]}'
    verdict, findings, questions = parse_llm_audit(raw)
    assert verdict == Verdict.REVISE
    assert findings[0].detail == "연차 혼동"
    assert questions == ["연금실제수령연차"]


def test_코드펜스가_붙어도_파싱된다():
    verdict, _, _ = parse_llm_audit('```json\n{"verdict":"BLOCK","findings":[]}\n```')
    assert verdict == Verdict.BLOCK


def test_파싱_실패는_문제없음과_구분된다():
    """감사자가 응답을 못 준 것과 '문제없음'은 다르다."""
    verdict, findings, _ = parse_llm_audit("응답을 드릴 수 없습니다")
    assert verdict == Verdict.APPROVE          # 심각도를 임의로 올리지도 않는다
    assert findings and findings[0].code == "PARSE_FAIL"


# ════════════════════════════════════════════════════════════════
# 결정론적 4대 감사
# ════════════════════════════════════════════════════════════════

def test_단정적_추천은_지적된다():
    result = supervise("C-Pe 클래스가 가장 유리합니다. 이걸 추천드립니다.",
                       citations=[object()])
    assert result.verdict != Verdict.APPROVE
    assert any(f.auditor == "준법" for f in result.findings)


def test_계산결과를_제시하는데_근거가_없으면_지적된다():
    """근거 문서 없이 수치를 내놓으면 등급을 강등한다."""
    result = supervise("세액공제는 148.5만원입니다.",
                       calc_results=[{"A_tax_credit": 148.5}], citations=[])
    assert result.verdict != Verdict.APPROVE
    assert any(f.code == "NO_BASIS" for f in result.findings)


def test_시정지시문이_생성된다():
    result = supervise("이 상품이 가장 유리합니다.", citations=[object()])
    if result.verdict == Verdict.REVISE:
        prompt = build_remediation_prompt(result, "이 상품이 가장 유리합니다.")
        assert "수정" in prompt
        assert "새로운 수치를 만들지 말고" in prompt


# ════════════════════════════════════════════════════════════════
# 계획 감사 (1.5 계층)
# ════════════════════════════════════════════════════════════════

def test_미등록_함수는_계획에서_제거된다():
    spec = {"asked_for": [{"id": "a", "description": "계산", "type": "calculation",
                           "calc_function": "존재하지_않는_함수"}],
            "planned_calls": [{"function": "존재하지_않는_함수", "args": {}}],
            "plan": ["계산"]}
    findings, safe = audit_plan(spec, {"연금수령한도_계산"})
    assert findings
    assert not [c for c in safe.get("planned_calls", [])
                if c["function"] == "존재하지_않는_함수"]


def test_정상_계획은_승인된다():
    spec = {"asked_for": [{"id": "a", "description": "연금수령한도",
                           "type": "calculation",
                           "calc_function": "연금수령한도_계산"}],
            "planned_calls": [{"function": "연금수령한도_계산", "args": {}}],
            "plan": ["한도 계산"]}
    result, safe = supervise_plan(spec, {"연금수령한도_계산"})
    assert result.verdict == Verdict.APPROVE
    assert safe["planned_calls"]


# ════════════════════════════════════════════════════════════════
# L4 · 가입자격은 조건이 없어도 침묵하지 않는다
# ════════════════════════════════════════════════════════════════
#
# 예전에는 `if fund_class and account_type:` 안에만 판정이 있어서, 조건이
# 하나라도 없으면 경고도 되묻기도 없이 통째로 건너뛰었다. 실제 사용자 질의는
# 대부분 짧아서("총보수 낮은 클래스 뭐예요") 이 침묵 경로가 오히려 정상 경로였다.

def test_조건이_다_있으면_가입자격을_판정한다():
    from app.pipeline import _eligibility_verdict

    v = _eligibility_verdict({"fund_class": "C-Re", "account_type": "연금저축"},
                             "C-Re 클래스로 가입할 수 있나요?")
    assert v.known
    assert v.eligible is not None
    assert v.missing == []


def test_조건이_없으면_되묻되_막지는_않는다():
    """단정하지 않고, 침묵하지도 않고, **막지도 않는다.**

    2026-08-29 개편 — 예전에는 여기서 "확정할 수 없습니다" 경고를 붙여
    추천을 사실상 보류시켰다. 지금은 확인 항목만 올리고 추천은 살린다.
    """
    from app.pipeline import _eligibility_verdict

    v = _eligibility_verdict({}, "총보수 가장 낮은 클래스로 가입하고 싶은데요")
    assert v.missing, "무엇이 필요한지 알려야 한다"
    assert not v.known
    assert not v.blocks, "모른다는 이유로 추천을 막으면 안 된다"


def test_가입자격을_묻지_않은_질의는_되묻지_않는다():
    """확인 항목은 최대 2건이라 무관한 되묻기가 자리를 차지하면 안 된다."""
    from app.pipeline import _eligibility_verdict

    v = _eligibility_verdict({}, "연금수령한도가 얼마인가요?")
    assert v.missing == []
    assert not v.blocks


def test_가입자격은_검색과_병렬로_판정된다():
    """2026-08-29 구조 변경 — 자격 판정은 L3 결과에 의존하지 않는다.

    예전에는 계산(L5) 뒤에 있었고, 그래서 '자격을 모른다'가 추천을 막는
    방향으로 작동했다. 지금은 검색과 동시에 판정하고, 합류 barrier가
    **확정적으로 불가한 것만** 걷어낸다.
    """
    import inspect

    import app.pipeline as p

    src = inspect.getsource(p._answer_question_impl)
    # 자격 판정이 병렬 블록 안에서, 계산보다 먼저 일어난다
    assert src.index("_eligibility_verdict") < src.index("run_calculations")
    assert "ThreadPoolExecutor" in src
    # 합류 barrier가 근거 필터 뒤에 온다
    assert src.index("_exploit(") < src.index("_eligibility_barrier(")


def test_자격을_모르면_추천을_막지_않는다():
    """'모른다'와 '안 된다'는 다르다 — 이 구분이 이번 개편의 핵심이다."""
    from app.core.coverage_pipeline import TraceLogger
    from app.pipeline import _eligibility_barrier, _eligibility_verdict

    v = _eligibility_verdict({}, "총보수가 가장 낮은 클래스가 뭔가요?")
    assert not v.known
    assert not v.blocks, "모른다는 이유로 막으면 안 된다"

    cands = [{"fund_class": "C-Pe", "expense": 0.5},
             {"fund_class": "C-Re", "expense": 0.7}]
    kept, warn = _eligibility_barrier(cands, v, TraceLogger())
    assert len(kept) == 2, "자격 미상인데 후보가 걸러졌다"
    assert warn == []


def test_자격이_확정적으로_불가하면_barrier가_제외한다():
    """조건이 충분한데 실제로 자격이 안 되면 추천에서 빠져야 한다."""
    from app.core.coverage_pipeline import TraceLogger
    from app.pipeline import _eligibility_barrier, _eligibility_verdict

    v = _eligibility_verdict({"account_type": "연금저축"},
                             "어떤 클래스로 가입할까요?")
    cands = [{"fund_class": "C-Pe"}, {"fund_class": "C-Re"}]
    kept, warn = _eligibility_barrier(cands, v, TraceLogger())
    # 계좌유형을 알면 클래스별로 판정되고, 맞지 않는 것은 빠진다
    assert all(c.get("eligible") is True for c in kept)
    if len(kept) < len(cands):
        assert warn and "제외" in warn[0]


def test_barrier가_판정_결과를_후보에_기록한다():
    """L6 적합성 감사가 eligible 플래그를 본다 — 비어 있으면 감사가 무력해진다."""
    from app.core.coverage_pipeline import TraceLogger
    from app.pipeline import _eligibility_barrier, _eligibility_verdict

    v = _eligibility_verdict({"account_type": "IRP"}, "클래스 추천")
    cands = [{"fund_class": "C-Pe"}]
    kept, _ = _eligibility_barrier(cands, v, TraceLogger())
    for c in kept:
        assert "eligible" in c


# ════════════════════════════════════════════════════════════════
# L6 정합성 감사 (2026-08-29 신설) — 감독을 '로직 정합성 점검 위주'로 재조정
# ════════════════════════════════════════════════════════════════
#
# ⚠️ 이 감사의 설계 제약: 오탐은 미탐보다 **엄격히 나쁘다.**
#    권한 계층상 LLM 감사는 심각도를 올릴 수만 있고 결정론적 판정을
#    완화하지 못하므로, 결정론적 오탐은 되돌릴 수 없는 강제 강등이 된다.
#    아래 테스트가 지키는 것이 정확히 그 경계다.

def test_한_문장_안의_모순을_잡는다():
    from app.core.supervisory_board import audit_coherence

    codes = [f.code for f in audit_coherence("중도인출은 가능합니다만 불가능합니다.")]
    assert "SELF_CONTRADICTION" in codes


def test_부정문만_있으면_모순이_아니다():
    """'불가능합니다'는 '가능합니다'를 부분문자열로 포함한다.

    단순 `in` 검사를 쓰면 부정문 하나만으로 스스로 발화한다. 실제로 그
    형태의 오탐이 실측에서 나왔다(E14 '명예퇴직금도 퇴직소득으로 보나요?').
    경계를 준 정규식이 아니면 이 테스트가 깨진다.
    """
    from app.core.supervisory_board import audit_coherence

    assert audit_coherence("IRP로 넣으면 일부만 꺼내는 것은 불가능합니다.") == []


def test_조건별_결론은_모순이_아니다():
    """조건에 따라 결론이 갈리는 것은 결함이 아니라 **설계 요구사항**이다.

    CLAUDE.md — 단정적 추천 금지, 확인조건 제시 후 상황별 결론.
    이걸 모순으로 잡으면 정상 동작을 깎는다.
    """
    from app.core.supervisory_board import audit_coherence

    answer = ("DC 계좌로 지급되는 경우에는 과세이연이 가능합니다. "
              "급여계좌로 지급되는 경우에는 계좌 내 이연이 불가능합니다.")
    assert audit_coherence(answer) == []


def test_정합성_감사는_문체를_판정하지_않는다():
    """L5'는 사람처럼 이어지는 문장으로 쓰도록 설계됐다(R7).

    구획 표시가 없다는 이유로 감사가 지적하면 그 설계를 되돌리게 된다.
    """
    from app.core.supervisory_board import audit_coherence

    flowing = ("말씀하신 조건이라면 연금수령한도는 1,200만원입니다. "
               "그렇게 판단한 근거는 평가액과 연금수령연차이고, "
               "연금실제수령연차가 확인되면 더 정확히 안내드릴 수 있습니다.")
    assert audit_coherence(flowing) == []


def test_전제결론_정합은_결정론_계층에_두지_않는다():
    """'모른다고 해 놓고 단정한다'는 의미 감사(LLM) 소관이다.

    결정론적으로 시도했다가 298건 중 26건(8.7%) 오탐이 났다 — 실질적으로
    경우를 나눈 문장('…에 따라 달라집니다')이 표지 목록에 없어서 걸렸다.
    표현의 가짓수가 유한하지 않으므로 목록 확장으로는 닫히지 않는다.
    되살리려면 이 테스트를 먼저 읽을 것.
    """
    import inspect

    from app.core import supervisory_board as sb

    assert not hasattr(sb, "_UNKNOWN_MARKERS")
    assert "UNKNOWN_THEN_ASSERT" not in inspect.getsource(sb.audit_coherence)
    # 대신 의미 감사 프롬프트가 전제–결론 정합을 본다
    assert "전제" in sb.LLM_AUDIT_SYSTEM_PROMPT


def test_supervise가_정합성_감사를_실제로_호출한다():
    """배선 검사 — 함수만 있고 호출되지 않는 결함이 과거에 있었다."""
    from app.core.supervisory_board import Verdict, supervise

    res = supervise("중도인출은 가능합니다만 불가능합니다.")
    assert "SELF_CONTRADICTION" in [f.code for f in res.findings]
    assert res.verdict != Verdict.APPROVE
