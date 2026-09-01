"""critical 함정 강제 삽입 — 재생성보다 늦게 돌아야 한다 (2026-09-01).

━━ 실물 확인 결함 ━━
"IRP 퇴직금 3억이 있는데 연 1500만원 넘게 받으면 종합과세되나요?"에서
C2(1,500만원 계산 제외 소득) 함정이 감지됐고, 답변 끝에 정정 각주까지
붙었다:

    ※ 국민연금 등 공적연금과 이연퇴직소득은 1,500만원 계산에 포함되지
      않습니다. 사적연금 납입분에서 발생한 소득만 대상입니다.

그런데 본문은 여전히 반대 소리를 했다: "1,500만원 이하로 조절하는 게
중요합니다" — 퇴직금(이연퇴직소득)에는 애초에 적용되지 않는 조언을 그대로
유지했다. L6 감독심사는 "승인 — 지적사항 없음"으로 통과시켰다.

━━ 원인 ━━
`_enforce_critical_traps`가 REVISE→재생성→구제재생성보다 **앞**(요구사항
반영 검증 직후)에서 돌았다. 그 결과:

  1. 각주가 먼저 붙어 정정 문구("이연퇴직소득")가 답변 텍스트에 존재하게 됨
  2. `unaddressed_traps`(TRAP_UNADDRESSED 판정의 근거)가 텍스트 전체에서
     그 용어를 찾아 "이미 해소됨"으로 봄
  3. critical 함정인데도 REVISE가 뜨지 않아 재생성 자체가 트리거되지 않음
  4. 본문은 원래 초안 그대로(각주와 모순) 나가고, L6도 그 상태를 승인

즉 강제 삽입이 **재생성 기회를 스스로 없애는** 역설적인 구조였다.

━━ 수정 ━━
강제 삽입을 REVISE→재생성→구제재생성 **뒤**(맨 마지막, 인용 무결성 검사
직전)로 옮겼다. 이제 첫 검증에서 critical 함정이 정말 안 걸렸으면
TRAP_UNADDRESSED가 정상적으로 REVISE를 내고, 재생성·구제재생성이 실제로
본문을 고칠 기회를 받는다(둘 다 시정 지시에 이 함정의 correction 문구를
그대로 담아 LLM에 넘긴다). 그래도 끝내 반영 안 되면 그때 결정론적으로
덧붙인다 — 원래 함수가 설계된 '최후의 보루' 의도 그대로다.

각주가 last-resort로 붙는 경우, 그 근거 문서가 인용(citations/
retrieved_context)에도 반영되도록 인용 조립을 강제 삽입 뒤에 한 번 더
돌린다 — 그러지 않으면 "답은 나갔는데 그 답의 근거가 retrieved_context에
없는" 상태가 된다.
"""

from __future__ import annotations

import inspect

from app import pipeline


def _src() -> str:
    return inspect.getsource(pipeline._answer_question_impl)


# ── 배선 — 순서가 실제로 뒤바뀌었는가 ────────────────────────

def test_강제삽입은_구제재생성_뒤에_온다():
    """★ 부품이 아니라 순서. 이게 이번 결함의 핵심이었다."""
    src = _src()
    enforce_pos = src.index("draft = _enforce_critical_traps(")
    rescue_pos = src.index("SubAgent_구제재생성")
    assert enforce_pos > rescue_pos, (
        "강제 삽입이 구제재생성보다 앞에 있다 — 재생성이 본문을 고칠 "
        "기회를 얻지 못하고 다시 건너뛰어질 것이다")


def test_강제삽입은_L6_감독심사보다_늦게_온다():
    """L6의 첫 판정이 강제 삽입 이전 상태를 보고 REVISE를 낼 수 있어야 한다."""
    src = _src()
    enforce_pos = src.index("draft = _enforce_critical_traps(")
    l6_pos = src.index('trace.log("L6_감독심사"')
    assert enforce_pos > l6_pos


def test_강제삽입_뒤에_인용을_다시_계산한다():
    """★ 최후의 보루로 붙은 각주도 근거 문서가 retrieved_context에 실려야 한다.

    시점을 옮기고 인용 재계산을 안 하면 "답은 나갔는데 근거는 없는" 상태가
    된다 — CLAUDE.md "사용한 것만 인용" 원칙 위반이다.
    """
    src = _src()
    enforce_pos = src.index("draft = _enforce_critical_traps(")
    after = src[enforce_pos:]
    # 강제 삽입 직후 첫 build_citations 재호출까지의 거리 안에서 찾는다
    assert "used_evidence = _used_evidence(" in after
    assert "citations = build_citations(" in after
    # 그 뒤로 다시 draft를 바꾸는 코드가 없어야 한다(인용이 최종본 기준)
    citations_pos = after.index("citations = build_citations(")
    tail = after[citations_pos:]
    assert "draft +=" not in tail and "draft = " not in tail.replace(
        "draft = attach_citations", ""), (
        "인용 재계산 뒤에도 draft가 더 바뀐다 — 인용이 최종 답변과 어긋난다")


def test_강제삽입_함수_자체는_그대로다():
    """★ 이동만 했다 — 함수 내부 로직은 건드리지 않았다(회귀 최소화)."""
    from app.pipeline import _enforce_critical_traps

    assert "재생성 후에도" in inspect.getsource(_enforce_critical_traps)


# ── 단위 — 강제 삽입된 정정문의 근거가 인용에 반영되는가 ─────
#
# ⚠️ mock 코퍼스에는 실제 trap_rules.py가 가리키는 R2_ 문서 ID가 없다
#    (그 카탈로그는 실물 코퍼스 기준이다). 그래서 end-to-end로 돌리면
#    _used_evidence가 "검색되지 않은 문서는 인용하지 않는다"는 **다른
#    올바른 원칙** 때문에 doc_id가 안 실린다 — 이건 버그가 아니다.
#    그 메커니즘 자체는 여기서 합성 근거로 직접, 결정론적으로 검증한다.

def test_강제삽입된_정정문의_근거가_검색됐다면_인용에_실린다():
    """★ 최후의 보루로 각주가 붙었을 때, 그 근거가 retrieved_context에도 실려야 한다."""
    from app.core.coverage_pipeline import EvidenceChunk
    from app.pipeline import _addressed_trap_docs, _used_evidence

    checks = [{"id": "C2", "severity": "critical",
              "correction": "이연퇴직소득은 1,500만원 계산에서 제외됩니다.",
              "docs": ["doc99"], "verify_any": ["이연퇴직소득"]}]

    # 본문에는 정정문이 없다가(=addressed 아님) 강제 삽입 뒤에는 있다
    body_before = "1,500만원 이하로 조절하시는 게 중요합니다."
    body_after = body_before + "\n\n※ 이연퇴직소득은 1,500만원 계산에서 제외됩니다."

    assert _addressed_trap_docs(body_before, checks) == {}, (
        "강제 삽입 전인데 이미 반영된 것으로 보이면 이 테스트 전제가 틀렸다")

    addressed = _addressed_trap_docs(body_after, checks)
    assert addressed == {"doc99": "함정 교정 근거"}

    # doc99가 실제로 검색된 근거 목록(evidence)에 있어야 인용으로 나온다
    evidence = [EvidenceChunk(doc_id="doc99", text="이연퇴직소득 관련 원문")]
    used = _used_evidence(evidence, slots=[], query_spec={}, trap_docs=addressed)
    assert any(u["doc_id"] == "doc99" for u in used), (
        "강제 삽입된 정정문의 근거 문서가 인용 목록에 없다")


def test_강제삽입_실제_함수를_거쳐도_같은_결과다():
    """★ 위 테스트가 손으로 만든 body_after가 아니라, 실제
    `_enforce_critical_traps`가 만든 결과로도 같은 결론이 나오는지 확인한다.
    """
    from app.core.coverage_pipeline import EvidenceChunk, TraceLogger
    from app.pipeline import (_addressed_trap_docs, _enforce_critical_traps,
                              _used_evidence)

    checks = [{"id": "C2", "severity": "critical",
              "correction": "이연퇴직소득은 1,500만원 계산에서 제외됩니다.",
              "docs": ["doc99"], "verify_any": ["이연퇴직소득"]}]
    body = "1,500만원 이하로 조절하시는 게 중요합니다."

    enforced = _enforce_critical_traps(body, checks, TraceLogger())
    assert "이연퇴직소득" in enforced

    evidence = [EvidenceChunk(doc_id="doc99", text="이연퇴직소득 관련 원문")]
    addressed = _addressed_trap_docs(enforced, checks)
    used = _used_evidence(evidence, slots=[], query_spec={}, trap_docs=addressed)
    assert any(u["doc_id"] == "doc99" for u in used)


# ── end-to-end — 실측 사고를 그대로 재현한다 ─────────────────

class _StubbornClient:
    """L5' 초안·재생성·구제재생성 전부 정정문을 반영하지 않는 대역.

    실물에서 실제로 벌어진 일 — HCX가 세 번의 기회 모두 C2를 못 넣었다.
    이 대역은 그 최악의 경우를 재현한다. 목적은 "결국 정정문이 들어가는가"가
    아니라 **"재생성 경로가 실제로 시도되는가"**다 — 그게 이번 결함의 핵심
    (재생성이 시도조차 안 됐다)이었기 때문이다.
    """

    is_mock = False

    def __init__(self):
        self.calls: list[str] = []

    def call(self, system, user, purpose="?", **kw):
        self.calls.append(purpose)
        if "감사자" in system:
            return '{"verdict":"APPROVE","findings":[]}'
        if purpose in ("l5_supervisor", "l5_regenerate", "subagent_rewrite"):
            return ("[확인된 조건]\n확인했습니다.\n\n[조건별 결론]\n"
                    "연 1,500만원을 초과하시면 전액이 종합과세 대상이 될 수 "
                    "있으니 1,500만원 이하로 조절하시는 게 중요합니다.\n\n"
                    "[한계 고지]\n확인이 필요합니다.")
        return "일반 진단 응답"

    def call_with_functions(self, s, u, t, purpose="?", **kw):
        self.calls.append(purpose)
        return {"name": None, "arguments": None, "raw": ""}


def test_실측_사고_재현_HCX가_세번_다_놓쳐도_재생성은_시도된다():
    """★ 이번 결함의 핵심 — 예전에는 이 REVISE 자체가 뜨지 않았다.

    각주가 먼저 붙어 TRAP_UNADDRESSED가 이미 '해소'로 봤기 때문이다.
    지금은 critical 함정이 안 걸리면 정상적으로 REVISE → 재생성 →
    구제재생성이 전부 시도돼야 한다 — HCX가 셋 다 놓쳐도 마찬가지다.
    """
    from app.pipeline import answer_question

    client = _StubbornClient()
    r = answer_question(
        "Q", "IRP에 있는 퇴직금인데 연 1500만원 넘게 받으면 세금이 어떻게 되나요?",
        client=client)

    assert "l5_regenerate" in client.calls, (
        "재생성이 시도되지 않았다 — 강제 삽입이 다시 앞으로 새치기했을 수 있다")
    assert "subagent_rewrite" in client.calls, (
        "구제재생성이 시도되지 않았다")

    tt = r["think_trace"]
    assert "SubAgent_구제재생성" in tt
    # 세 번 다 놓쳤으니 최후의 보루(강제 삽입)까지 가거나, 최소한
    # 검증 미통과 고지를 통해 정정 취지가 사용자에게 도달해야 한다 —
    # 조용히 원본만 나가면 안 된다.
    assert ("함정교정_강제삽입" in tt) or ("검증_미통과_고지" in tt), (
        "정정 취지가 사용자에게 어떤 형태로도 도달하지 않았다")
    assert "이연퇴직소득" in r["answer"], (
        "정정 사실(이연퇴직소득 제외)이 최종 답변 어디에도 없다")
