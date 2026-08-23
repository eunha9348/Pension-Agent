"""B단계 · 검증 계층 신뢰성 회귀.

━━ 이 파일이 지키는 것 ━━
이 프로젝트의 차별점은 "스스로를 감사한다"는 것이다. 그런데 감사가
형식만 남고 실제로는 통과시켜 버리면, 감사가 없는 것보다 나쁘다 —
리포트에 찍힌 '통과' 표시가 전부 거짓이 되기 때문이다.

여기 테스트는 전부 **감사가 헐거워지는 방향**을 막는다. 느슨하게 고치고
싶어지면 아래 사고 기록을 먼저 읽을 것.
"""

from __future__ import annotations

from app.core.numeric_verifier import extract_numbers, verify_numeric_grounding
from app.core.supervisory_board import Verdict, audit_fitness
from app.core.trap_rules import (build_trap_context, term_present,
                                 unaddressed_traps, verify_terms_for, TRAPS)


# ════════════════════════════════════════════════════════════════
# B-1 · 함정별 개별 검증
# ════════════════════════════════════════════════════════════════

def test_모든_함정에_검증_핵심어가_있다():
    """함정을 새로 추가하고 핵심어를 빠뜨리면 그 규칙은 영원히
    '해소됨'으로 처리된다 — 조용히 뚫리는 구멍이라 테스트로 막는다."""
    missing = [r.id for r in TRAPS if not verify_terms_for(r.id)]
    assert not missing, f"검증 핵심어가 없는 함정: {missing}"


def test_일반적인_단어_하나로_함정이_통과되지_않는다():
    """Q-002 사고 — '유동성 관리에는 주의가 필요합니다'라는 문장이
    전혀 무관한 D2(운용사 간 위험등급 비교)를 통과시켰다."""
    checks = [{"id": "D2", "severity": "high",
               "verify_any": verify_terms_for("D2"), "correction": "…"}]
    answer = "안정성을 우선하신다면 유동성 관리에는 주의가 필요합니다."
    assert [m["id"] for m in unaddressed_traps(answer, checks)] == ["D2"]


def test_해당_개념을_실제로_다루면_통과한다():
    checks = [{"id": "D2", "severity": "high",
               "verify_any": verify_terms_for("D2"), "correction": "…"}]
    answer = "위험등급은 집합투자업자의 내부기준이라 운용사가 다르면 그대로 비교하기 어렵습니다."
    assert unaddressed_traps(answer, checks) == []


def test_함정마다_따로_판정한다():
    """5건이 감지되면 5건을 각각 본다 — 하나 맞혔다고 전부 통과가 아니다."""
    checks = [{"id": tid, "severity": "critical",
               "verify_any": verify_terms_for(tid), "correction": "…"}
              for tid in ("B1", "E1", "E2")]
    # B1만 다룬 답변
    answer = "연금 개시 후 실제로 인출한 해만 실제수령연차로 쌓입니다."
    missed = {m["id"] for m in unaddressed_traps(answer, checks)}
    assert missed == {"E1", "E2"}


def test_critical_미해소는_REVISE_비critical은_DOWNGRADE():
    crit = [{"id": "E1", "severity": "critical",
             "verify_any": verify_terms_for("E1"), "correction": "…"}]
    high = [{"id": "D2", "severity": "high",
             "verify_any": verify_terms_for("D2"), "correction": "…"}]
    a = "일반적인 답변입니다."
    assert audit_fitness(a, trap_checks=crit)[0].severity == Verdict.REVISE
    assert audit_fitness(a, trap_checks=high)[0].severity == Verdict.DOWNGRADE


def test_시정_지시에_무엇을_고칠지_담긴다():
    """뭉뚱그린 지시는 재생성도 실패한다 — 구체적이어야 한다."""
    checks = [{"id": "E1", "severity": "critical",
               "verify_any": verify_terms_for("E1"),
               "correction": "명예퇴직수당은 법정 외 퇴직급여에 해당합니다."}]
    f = audit_fitness("일반적인 답변입니다.", trap_checks=checks)[0]
    assert "E1" in f.directive
    assert "법정 외" in f.directive


def test_영문_약어는_낱말_경계로_본다():
    """'DB'가 다른 영문 안에서 잡히면 오탐이 된다."""
    assert term_present("DB형 제도는", "DB") is True
    assert term_present("ADBE 라는 종목", "DB") is False


def test_한국어는_조사가_붙어도_잡는다():
    assert term_present("공적연금은 제외됩니다", "공적연금") is True


def test_명퇴_질의의_critical_함정이_검증_대상에_오른다():
    """Q-001 회귀 — 이 세 건이 판정 대상에서 빠지면 같은 실패가 재현된다."""
    ctx = build_trap_context(
        "명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
        "어마어마하다던데, 절세법만 알려주세요.")
    ids = {c["id"] for c in ctx["checks"] if c["severity"] == "critical"}
    assert {"B1", "E1", "E2"} <= ids


def test_실제_실패_답변이_이제는_걸린다():
    """Q-001의 최종 답변(세액공제만 설명)을 그대로 넣어 본다.
    당시에는 통과했지만 이제는 미해소로 걸려야 한다."""
    ctx = build_trap_context(
        "명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
        "어마어마하다던데, 절세법만 알려주세요.")
    actual_failed_answer = (
        "연금계좌에 납입한 금액은 종합소득 산출세액에서 공제가 가능합니다. "
        "연간 600만 원 이내의 금액 또는 총 900만 원 중에서 선택할 수 있으며, "
        "IRP 계좌를 이용한다면 퇴직소득세를 감면받을 수 있지만, 한번 인출하면 "
        "일부를 다시 넣는 것은 불가능하므로 신중히 결정하셔야 합니다.")
    missed = {m["id"] for m in unaddressed_traps(actual_failed_answer, ctx["checks"])}
    assert "E1" in missed and "E2" in missed, \
        "명예퇴직급여 처리를 다루지 않은 답변이 여전히 통과한다"


# ════════════════════════════════════════════════════════════════
# B-2 · 수치 검증의 단위 인식
# ════════════════════════════════════════════════════════════════

def test_단위가_붙은_작은_수는_검증_대상이다():
    """Q-002 사고 — '만기 3개월~3년'의 3·7이 _TRIVIAL_MAX(12) 이하라
    검증 시도조차 되지 않았고, 근거에 없는 수치가 그대로 나갔다."""
    nums = extract_numbers("만기가 3개월에서 3년 사이의 국공채에 투자합니다")
    assert 3.0 in nums


def test_여러_단위를_인식한다():
    nums = extract_numbers("11년차부터, 만 55세 이후, 5등급, 연 7회")
    assert {11.0, 55.0, 5.0, 7.0} <= nums


def test_조문번호와_목록순서는_여전히_제외한다():
    """오탐 회귀 — 제12조·2항까지 대조하면 멀쩡한 답변이 차단된다."""
    nums = extract_numbers("제12조 제2항에 따라 처리됩니다")
    assert 12.0 not in nums and 2.0 not in nums


def test_연도는_여전히_제외한다():
    assert 2024.0 not in extract_numbers("2024년 1월 1일 이후 적용됩니다")


def test_Q002의_만기_수치가_검사_대상에_들어온다():
    """Q-002 회귀 — 당시엔 '0개 수치 전부 근거 확인'으로 **검사조차 안 됐다.**
    이제는 최소한 대조 대상에는 오른다."""
    evidence = ["2010.07.16 비교지수변경(Customized KIS 중장기 채권지수 "
                "(1Y~7Y, 듀레이션: 3.0±0.7))"]
    answer = "단기는 주로 만기가 3개월에서 3년 사이의 국공채에 투자합니다."
    result = verify_numeric_grounding(answer, [], evidence)
    assert result.checked_count > 0, "여전히 아무것도 검증하지 않는다"


def test_수치검증의_한계를_명시한다():
    """⚠️ 이 테스트는 '아직 못 잡는 것'을 기록해 둔 것이다 — 실패가 아니다.

    Q-002에서 답변의 '만기 3년'은 근거의 '듀레이션 3.0'과 숫자가 같다.
    수치 검증기는 값만 대조하므로 이 둘을 구분할 수 없다. 즉
    **숫자는 근거에 있는데 의미가 다른 경우**는 이 계층의 사각지대다.

    그래서 Q-002는 세 계층이 함께 막는다:
      · A-4 연혁 청크 강등 — 애초에 연혁이 근거로 뽑히지 않게
      · L6 의미 감사(HyperCLOVA X) — 숫자와 서술의 정합 판정
      · B-3 '통과 아님' 표기 — 검사하지 않았음을 감추지 않기
    이 사실을 잊고 "수치 검증을 통과했으니 안전하다"고 믿으면 안 된다.
    """
    evidence = ["듀레이션: 3.0±0.7"]
    result = verify_numeric_grounding("만기 3년입니다.", [], evidence)
    assert result.passed is True     # 값이 같으므로 통과한다 (한계)
    assert result.checked_count == 1  # 다만 검사는 실제로 수행됐다


def test_근거에_아예_없는_수치는_잡는다():
    evidence = ["55세 이후 10년간 연금수령"]
    result = verify_numeric_grounding("만 47세부터 수령할 수 있습니다.", [], evidence)
    assert not result.passed
    assert 47.0 in result.ungrounded


def test_근거에_있는_수치는_통과한다():
    """오탐 회귀 — 단위 인식이 과하면 정상 답변이 차단된다."""
    evidence = ["연간 연금저축계좌 납입액 600만원 이내 세액공제 13.2%",
                "55세 이후 10년간 연간 연금수령한도 내에서 연금수령"]
    answer = "55세 이후 연금수령이 가능하며 세액공제율은 13.2%입니다."
    assert verify_numeric_grounding(answer, [], evidence).passed


# ════════════════════════════════════════════════════════════════
# B-3 · 검증 0건을 '통과'로 표기하지 않는다
# ════════════════════════════════════════════════════════════════

def test_대조할_수치가_없으면_통과라고_쓰지_않는다():
    """트레이스가 '0개 수치 전부 근거 확인 · 통과'로 찍히면,
    아무것도 검사하지 않은 답변이 검증된 것처럼 보인다."""
    trace = verify_numeric_grounding("수치가 없는 설명입니다.", [], []).as_trace()
    assert "통과 아님" in trace
    assert "0개 수치 전부 근거 확인" not in trace


def test_실제로_대조한_경우에만_통과라고_쓴다():
    trace = verify_numeric_grounding(
        "세액공제율은 13.2%입니다.", [], ["세액공제 13.2%"]).as_trace()
    assert "통과" in trace and "통과 아님" not in trace


# ════════════════════════════════════════════════════════════════
# 채점기가 금액 표기 흔들림 때문에 정답을 틀렸다고 하면 안 된다
# ════════════════════════════════════════════════════════════════
#
# 실사고(E-04): 한도를 정확히 1,200만원으로 계산했고 답변에도 실었는데,
# LLM이 "1200만원"(쉼표 없이)이라고 써서 문자열 일치 채점이 실패로 봤다.
# 계산·검증·보강 장치는 전부 정상이었다 — 채점기만 틀렸다.

def test_금액은_쉼표_유무를_가리지_않는다():
    from tests.eval_set import _includes

    assert _includes("한도는 1200만원입니다.", "1,200만원")
    assert _includes("한도는 1,200만원입니다.", "1,200만원")
    assert _includes("한도는 1억 2,000만원입니다.", "1억 2,000만원")


def test_금액_값이_다르면_여전히_실패한다():
    """표기를 흡수하는 것이지 판정을 느슨하게 하는 것이 아니다."""
    from tests.eval_set import _includes

    assert not _includes("한도는 900만원입니다.", "1,200만원")
    assert not _includes("연금수령한도를 안내드립니다.", "1,200만원")


def test_금액이_아닌_항목은_완전일치를_요구한다():
    from tests.eval_set import _includes

    assert _includes("연금실제수령연차로 결정됩니다.", "연금실제수령연차")
    assert not _includes("실제수령연차로 결정됩니다.", "연금실제수령연차")
    assert not _includes("초과분만 대상입니다.", "전액")
