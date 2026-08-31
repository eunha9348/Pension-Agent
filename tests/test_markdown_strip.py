"""HCX 마크다운 잔재 제거 — 실연동 실물 확인 (2026-09-01).

━━ 실측 결함 ━━
실서버(실물 코퍼스 + 실HCX)에서 "연금저축·IRP 세액공제 한도" 질의 답변에
아래처럼 마크다운 강조 표기가 별표 그대로 노출됐다:

    연금저축 단독으로는 **600만 원**까지 세액공제가 가능합니다.
    만약 IRP와 함께 이용하실 경우, 두 계좌의 합산 세액공제 한도는
    **900만 원**이며...

평가자는 answer 필드를 일반 텍스트로 읽는다 — "**"가 그대로 보인다.
mock 대역 300건 스캔에서는 0건이었다. mock은 render_template_answer가
만드는 결정론적 문장이라 애초에 마크다운을 쓰지 않기 때문이다.
프롬프트에 금지를 명시해도(SUPERVISOR_SYSTEM_PROMPT 규칙 8,
ADVISORY_SYSTEM_PROMPT 규칙 6, SUB_AGENT_REWRITE_PROMPT 규칙 7) HCX가
관성적으로 강조 표기를 쓸 수 있으므로, 프롬프트 지시만으로는 부족하다
— 생성 직후 결정론적으로 제거하는 이중 방어가 필요하다.

━━ 지켜야 할 순서 ━━
strip_markdown은 verify_grounding(수치 검증)보다 반드시 앞에서 적용한다.
검증이 본 텍스트와 사용자에게 나가는 텍스트가 달라지면 그 자체가 사고다
(표시 반올림값이 날조로 오판되던 것과 같은 계열의 함정).
"""

from __future__ import annotations

from app.generation.answer_prompt import strip_markdown

# 실제로 답변에 찍혔던 그 문장 (2026-09-01 실연동 캡처)
_REAL_CAPTURE = (
    "연금저축 단독으로는 **600만 원**까지 세액공제가 가능합니다. 만약 "
    "IRP와 함께 이용하실 경우, 두 계좌의 합산 세액공제 한도는 **900만 원**"
    "이며, 이를 통해 최대 공제를 받을 수 있습니다. 또한, 연금저축과 IRP의 "
    "연간 총 납입한도는 **1,800만 원**이므로, 이 범위 내에서 자유롭게 "
    "납입 계획을 세우실 수 있습니다.")


def test_실물_캡처_사례를_그대로_재현한다():
    """★ 실측 사고 재현 — 별표만 벗겨지고 숫자·문장은 그대로여야 한다."""
    out, changed = strip_markdown(_REAL_CAPTURE)
    assert changed is True
    assert "**" not in out
    assert "600만 원" in out
    assert "900만 원" in out
    assert "1,800만 원" in out
    # 문장 자체는 훼손되지 않아야 한다 — 별표만 벗기고 내용은 그대로
    assert "연금저축 단독으로는 600만 원까지 세액공제가 가능합니다" in out


def test_굵게_표기가_없으면_손대지_않는다():
    plain = "말씀하신 조건이라면 한도는 600만원입니다."
    out, changed = strip_markdown(plain)
    assert changed is False
    assert out == plain


def test_밑줄_강조도_제거한다():
    out, changed = strip_markdown("한도는 __600만원__입니다.")
    assert changed is True
    assert "__" not in out
    assert "600만원" in out


def test_마크다운_헤더를_제거한다():
    out, changed = strip_markdown("### 세액공제 한도\n600만원입니다.")
    assert changed is True
    assert "#" not in out
    assert "세액공제 한도" in out


def test_금지표현_치환과_함께_써도_숫자는_보존된다():
    """두 후처리(마크다운 제거 + 단정표현 치환)를 같이 걸어도 숫자는 그대로."""
    from app.generation.answer_prompt import strip_forbidden

    text = "이 상품이 **가장 유리합니다**. 한도는 **600만원**입니다."
    text, _ = strip_markdown(text)
    text, found = strip_forbidden(text)
    assert "**" not in text
    assert found  # "가장 유리합니다"가 치환 대상으로 잡혀야 한다
    assert "600만원" in text


# ── 배선 — 프롬프트에 마크다운 금지가 실제로 명시돼 있는가 ────────

def test_세_생성_프롬프트_전부에_마크다운_금지가_있다():
    """★ 프롬프트 지시 + 후처리 이중 방어 중 프롬프트 쪽이 빠지지 않았는지."""
    from app.core.sub_agent import SUB_AGENT_REWRITE_PROMPT
    from app.generation.advisory import ADVISORY_SYSTEM_PROMPT
    from app.generation.answer_prompt import SUPERVISOR_SYSTEM_PROMPT

    for name, prompt in [("SUPERVISOR_SYSTEM_PROMPT", SUPERVISOR_SYSTEM_PROMPT),
                         ("ADVISORY_SYSTEM_PROMPT", ADVISORY_SYSTEM_PROMPT),
                         ("SUB_AGENT_REWRITE_PROMPT", SUB_AGENT_REWRITE_PROMPT)]:
        assert "마크다운" in prompt, f"{name}에 마크다운 금지 지시가 없다"


# ── 배선 — 파이프라인 3개 생성 경로 전부에 걸려 있는가 ────────────

def test_파이프라인의_세_생성_경로_모두_마크다운을_제거한다():
    """L5' 초안 · L5' 재생성 · Sub-Agent 구제, 세 곳 다 빠짐없이."""
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    assert src.count("strip_markdown(") == 3, (
        f"strip_markdown 호출이 3곳이 아니라 {src.count('strip_markdown(')}곳이다 — "
        "초안·재생성·구제 재생성 중 하나가 빠졌을 수 있다")


def test_마크다운_제거가_수치검증보다_먼저_돈다():
    """★ 순서 불변식 — verify_grounding이 보는 텍스트와 나가는 텍스트가
    같아야 한다. 순서가 뒤집히면 검증이 마크다운 섞인 텍스트를 보게 되고,
    사용자에게는 정리된 텍스트가 나가 — 검증이 실제로 무엇을 검증했는지
    알 수 없게 된다."""
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline._answer_question_impl)
    md_pos = src.index("strip_markdown(draft)")
    verify_pos = src.index("verify_grounding(draft, evidence)")
    assert md_pos < verify_pos, "마크다운 제거가 수치 검증보다 뒤에 있다"
