"""외부 감사(300건 실측) 지적사항 회귀 테스트.

이 파일이 지키는 규칙: **주장이 아니라 재현된 결함만 고정한다.**
감사 리포트의 지적 중 코드로 재현·검증된 것만 여기 담았다. 원본 문서 없이는
판단할 수 없는 항목(종신형 vs 연령별 세율 우선순위, IRP 부분인출 가부 등)은
의도적으로 제외했다 — 근거 없이 고정하면 그게 또 다른 오답이 된다.

각 테스트는 "무엇이 잘못됐고 왜 그게 문제인가"를 함께 남긴다.
"""

from __future__ import annotations

import pytest

from app.analysis.conditions import derive_conditions
from app.analysis.calc_params import make_calc_params_builder
from app.analysis.query_spec import reconcile_spec, rule_based_spec, sanitize_spec
from app.core.coverage_pipeline import (RequirementSlot, SlotStatus,
                                        run_calculations)
from app.generation.render import render_calc_result

TAX_CREDIT_FN = "사적연금_납입한도_세액공제_계산"
CONTRIB_KEYS = ("pension_saving_manwon", "irp_manwon",
                "combined_contribution_manwon")


def _calc_functions(question: str) -> set[str]:
    return {s.get("calc_function")
            for s in rule_based_spec(question)["asked_for"]
            if s.get("calc_function")}


def _render(question: str, fn: str) -> str:
    slot = RequirementSlot(slot_id="s", description="t",
                           slot_type="calculation", calc_function=fn)
    slot.status = SlotStatus.CALC_PENDING
    slots = run_calculations([slot],
                             make_calc_params_builder(derive_conditions(question)))
    return render_calc_result(slots[0].calc_result or {})


# ════════════════════════════════════════════════════════════════
# 결함 1 · 서로 다른 질문이 같은 "세액공제 카드"로 답변됨
# ════════════════════════════════════════════════════════════════
# 실측 300건 중 43건이 납입액 없이 세액공제 계산으로 라우팅돼, 전부 같은
# 한도 카드(600/900/1800)를 [조건별 결론] 자리에 받았다. 그 43건 중 PASS는
# 3건뿐이었고 그 3건은 실제로 한도를 묻는 질의였다.
#
# 사용자 체감상 이건 오답보다 나쁘다 — 형식이 그럴듯해서 답을 받았다고
# 착각하게 만든다.

@pytest.mark.parametrize("question", [
    "배우자 명의 연금저축에 내가 납입하면 내가 세액공제를 받을 수 있나요?",
    "소득이 없는 전업주부도 연금저축 세액공제를 받을 수 있나요?",
    "연말정산에서 IRP 세액공제를 받으려면 어떤 서류가 필요한가요?",
    "연금저축보험과 연금저축펀드는 세액공제에서 차이가 있나요?",
    "세액공제 한도를 초과해서 납입한 금액은 다음 해로 이월할 수 있나요?",
])
def test_사실_질의는_세액공제_계산을_돌리지_않는다(question):
    """계산 결과가 답이 될 수 없는 질의다 — 자격·절차·규칙을 묻고 있다."""
    assert TAX_CREDIT_FN not in _calc_functions(question), question


@pytest.mark.parametrize("question", [
    "연금저축과 IRP를 합쳐서 연간 세액공제 한도는 얼마인가요?",
    "연금저축만으로 받을 수 있는 세액공제 한도는 얼마인가요?",
])
def test_한도_자체를_묻는_질의는_계산을_유지한다(question):
    """상수라도 그 상수가 곧 답인 질의다. 오탐만 보고 조이면 정답도 죽는다."""
    assert TAX_CREDIT_FN in _calc_functions(question), question


@pytest.mark.parametrize("question", [
    "총급여 4000만원인데 연금저축에 600만원 넣으면 세액공제 얼마인가요?",
    "총급여 8천만원인데 IRP에 900만원 넣으면 얼마나 돌려받나요?",
])
def test_납입액이_있으면_계산을_유지한다(question):
    assert TAX_CREDIT_FN in _calc_functions(question), question


def test_계산을_접어도_사실_슬롯은_남는다():
    """검색·함정 유도가 사실 슬롯에 붙어 있다 — 계산만 접고 검색은 살린다."""
    q = "배우자 명의 연금저축에 내가 납입하면 내가 세액공제를 받을 수 있나요?"
    ids = [s["id"] for s in rule_based_spec(q)["asked_for"]]
    assert "seaek_gongje_fact" in ids


# ════════════════════════════════════════════════════════════════
# 결함 4 · 계산 오답 — 어휘 미매칭으로 계산이 아예 안 돌았다
# ════════════════════════════════════════════════════════════════
# "얼마나 돌려받나요"는 사용자가 세액공제를 부르는 가장 흔한 말인데
# 주제어에 없어서 일반 폴백으로 떨어졌고, 숫자를 LLM이 지어냈다.

@pytest.mark.parametrize("question,expected", [
    # 연금계좌 = 연금저축 + 퇴직연금 → 합산 한도 900만원 × 16.5%
    ("총급여 5천만원 근로자가 연금계좌에 900만원 납입하면 환급액은 얼마인가요?", 148.5),
    ("총급여 8천만원인데 IRP에 900만원 넣으면 얼마나 돌려받나요?", 118.8),
    ("총급여 7000만원인 사람이 연금저축 600만원만 납입하면 환급액은 얼마인가요?", 79.2),
])
def test_환급_어휘_질의가_세액공제액을_계산한다(question, expected):
    from app.core.pension_calc_functions import calc_private_contribution_limit

    assert TAX_CREDIT_FN in _calc_functions(question), question
    cond = derive_conditions(question)
    x = cond.get("pension_saving_manwon")
    y = cond.get("irp_manwon") or cond.get("combined_contribution_manwon")
    rate = 0.165 if (cond.get("total_income_manwon") or 0) <= 5500 else 0.132
    got = calc_private_contribution_limit(x, y, rate)["A_tax_credit"]
    assert got == pytest.approx(expected), f"{question} → {got}"


@pytest.mark.parametrize("question", [
    # 퇴직소득세 환급·투자손실 환급은 세액공제와 전혀 다른 제도다
    "퇴직소득세를 이미 냈는데 IRP로 이체하면 환급받을 수 있나요?",
    "연금계좌에서 투자 손실이 나면 세금을 환급받을 수 있다던데 맞나요?",
])
def test_다른_제도의_환급은_세액공제로_끌어오지_않는다(question):
    assert TAX_CREDIT_FN not in _calc_functions(question), question


# ════════════════════════════════════════════════════════════════
# 결함 4 · 금액 미입력인데 "0만원"을 출력
# ════════════════════════════════════════════════════════════════
# 0원은 사실이 아니라 미입력이다. 사용자에게는 "세금이 0원"으로 읽힌다.
# calc_private_contribution_limit에서 이미 고친 것과 같은 원칙이다.

@pytest.mark.parametrize("question", [
    "만 68세인데 연금 받을 때 세율은 몇 퍼센트인가요?",
    "만 72세면 연금소득세율이 어떻게 되나요?",
    "만 80세인데 연금 받을 때 세금 몇 퍼센트 떼나요?",
])
def test_수령액을_모르면_원천징수세액을_내지_않는다(question):
    text = _render(question, "사적연금_원천징수_계산")
    assert "0만원" not in text, f"{question} → {text}"
    assert "%" in text, "세율은 나이만으로 정해지므로 반드시 나와야 한다"


def test_수령액이_있으면_세액을_계산한다():
    """오탐만 보고 접으면 정작 계산해야 할 때도 안 나온다."""
    text = _render("만 60세이고 매달 200만원씩 받으면 원천징수 세금은 얼마인가요?",
                   "사적연금_원천징수_계산")
    assert "원천징수세액" in text


def test_나이를_모르면_원천징수는_예외로_끊는다():
    """조용히 기본값을 쓰면 55세 미만으로 취급돼 16.5%가 나간다."""
    from app.core.pension_calc_functions import calc_private_withholding

    with pytest.raises(ValueError):
        calc_private_withholding(P_private_monthly=100)


# ════════════════════════════════════════════════════════════════
# 납입액 vs 계좌 잔고 혼동
# ════════════════════════════════════════════════════════════════
# '계좌에'는 잔고 표지가 아니라 위치 표지다. 반대로 잔고를 납입액으로 읽으면
# 연 납입한도(1,800만원)로는 불가능한 전제로 세액공제를 계산한다.

def test_연금계좌에_납입한_금액은_잔고가_아니다():
    c = derive_conditions("연금계좌에 900만원 납입하면 환급액은 얼마인가요?")
    assert c.get("combined_contribution_manwon") == 900
    assert "account_value_manwon" not in c


def test_계좌에_있는_금액은_잔고로_남는다():
    """반대 방향 회귀 — 수령한도 계산이 평가액을 잃으면 안 된다."""
    c = derive_conditions("계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?")
    assert c.get("account_value_manwon") == 10000


@pytest.mark.parametrize("question", [
    "IRP 평가액이 3억원인데 세액공제는 얼마나 받나요?",
    "IRP에 적립금 5000만원 있는데 세액공제 한도가 얼마인가요?",
])
def test_잔고를_납입액으로_읽지_않는다(question):
    c = derive_conditions(question)
    for k in CONTRIB_KEYS:
        assert k not in c, f"{question} — 잔고가 {k}로 잡혔다"


def test_평가액은_수령한도_계산에는_그대로_쓰인다():
    c = derive_conditions("IRP 평가액이 3억원이고 연금수령 2년차면 연금수령한도는 얼마인가요?")
    assert c.get("account_value_manwon") == 30000


def test_연봉이_퇴직급여_슬롯으로_새지_않는다():
    """UI-013 실사용 재현 (2026-09-06).

    "근속 25년차이고 연봉 8천만원인데 DC로 퇴직금 얼마나 받을 수 있나요"에서
    질의의 유일한 금액인 연봉 8,000만원이 '퇴직금' 키워드에 가장 가까운
    금액이라는 이유만으로 severance_manwon=8000으로 잡혔다. 그 결과
    calc_retirement_income_tax(severance_pay=8000, service_years=25)가
    돌아 "환산급여 1,200만원·산출세액 20만원" 같은, 질문(퇴직급여 총액이
    얼마인지)과 무관하고 근거도 없는 세금 계산이 답변으로 나갔다.
    saving/irp 슬롯에는 이미 있던 _is_income_amount 가드가 severance_manwon
    에는 빠져 있었다 — 같은 결함 계열이 이 자리만 놓친 것.
    """
    c = derive_conditions("근속 25년차이고 연봉 8천만원인데 DC로 퇴직금 얼마나 받을 수 있나요")
    assert "severance_manwon" not in c
    assert c.get("total_income_manwon") == 8000


def test_퇴직급여_총액이_명시되면_여전히_잡힌다():
    """반대 방향 회귀 — 실제로 퇴직급여 총액을 말한 질의는 계속 계산돼야 한다."""
    c = derive_conditions("퇴직금 8000만원 받았는데 근속연수는 25년입니다. 세금이 얼마인가요?")
    assert c.get("severance_manwon") == 8000


def test_LLM이_현금을_연금계좌_평가액으로_오판해도_반영하지_않는다():
    """UI-017 실사용 재현 (2026-09-06).

    "나 24살에 3000만원 현금있고 500만원 주택청약있는데 노후대비 어떻게
    해야할까? 연금을 아예 모르겠누 ㅋ"는 명백히 ADVISORY(불특정 개인 서술)
    질의인데, 실서버의 HyperCLOVA X(L1)가 "3000만원 현금"을
    account_value_manwon(연금계좌 평가액)으로 잘못 라벨링했다. 규칙 기반
    추출은 '계좌에'·'평가액' 없이는 이 슬롯을 만들지 않아 안전했지만,
    LLM이 준 조건을 병합하는 루프에는 그 검증이 없어 그대로 통과됐다.
    그 결과 계산 조건이 있는 것으로 오판돼 ADVISORY로 가야 할 질의가
    GENERAL로 잘못 라우팅되고, 대응하는 계산이 없어 "제공 자료로 확정하기
    어렵습니다"로 무너졌다(mock에서는 L1이 항상 비어 규칙 경로만 타므로
    로컬 회귀에서는 재현되지 않았다 — 실서버에서만 보이는 결함).
    """
    q = ("나 24살에 3000만원 현금있고 500만원 주택청약있는데 "
         "노후대비 어떻게 해야할까? 연금을 아예 모르겠누 ㅋ")
    c = derive_conditions(q, llm_conditions={"account_value_manwon": 3000, "age": 24})
    assert "account_value_manwon" not in c

    from app.analysis.routing import classify_route
    route = classify_route(q, conditions=c, asked_for=[])
    assert route.route == "ADVISORY", f"여전히 잘못 라우팅됨: {route}"


def test_LLM이_준_정당한_계좌평가액은_그대로_반영된다():
    """반대 방향 회귀 — '평가액' 같은 정당한 문맥의 LLM 값은 계속 계산돼야 한다."""
    c = derive_conditions(
        "IRP 평가액이 3억원이고 연금수령 2년차면 연금수령한도는 얼마인가요?",
        llm_conditions={"account_value_manwon": 30000, "pension_year": 2})
    assert c.get("account_value_manwon") == 30000


def test_LLM이_주택청약을_연금수령액으로_오판해도_반영하지_않는다():
    """UI-014 실사용 재현 (2026-09-06) — F28의 재발.

    "24살에 현금 3000만원 있고 주택청약 500만원 있는데 노후대비 어떻게
    해야할까?"에서 F28은 account_value_manwon 등 5개 키만 막았는데,
    실서버 HCX는 "주택청약 500만원"을 private_pension_annual_manwon
    (연간 연금수령액)으로 라벨링했다 — F28이 막지 않은 다른 키에서
    같은 결함이 그대로 재발했다. 그 결과 ADVISORY로 가야 할 질의가
    다시 GENERAL로 잘못 라우팅됐다.

    가드 대상 키를 routing._CALC_CONDITION_KEYS의 금액 키 전부로
    넓히고, 비연금 자산 신호에 '주택청약'도 추가해 해결했다.
    """
    q = "24살에 현금 3000만원 있고 주택청약 500만원 있는데 노후대비 어떻게 해야할까?"
    c = derive_conditions(q, llm_conditions={"age": 24,
                                              "private_pension_annual_manwon": 500})
    assert "private_pension_annual_manwon" not in c
    assert "private_pension_monthly_manwon" not in c

    from app.analysis.routing import classify_route
    route = classify_route(q, conditions=c, asked_for=[])
    assert route.route == "ADVISORY", f"여전히 잘못 라우팅됨: {route}"


def test_주택청약_아닌_정당한_연금수령액은_그대로_반영된다():
    """반대 방향 회귀 — 실제로 연금 수령액을 말한 질의는 계속 계산돼야 한다."""
    c = derive_conditions("사적연금으로 연간 1200만원 받으면 세금은 얼마인가요?",
                          llm_conditions={"private_pension_annual_manwon": 1200})
    assert c.get("private_pension_annual_manwon") == 1200


# ════════════════════════════════════════════════════════════════
# 결함 6 · PDF 이중 글리프가 답변까지 노출
# ════════════════════════════════════════════════════════════════

def test_이중_글리프를_복구한다():
    """감사 리포트 B08에 실제로 노출된 문자열."""
    from app.ingest.loader import repair_doubled_glyphs

    broken = "평가가액액 × 112200 ((1111 -- 연연금금수수령령연연차차)) 110000"
    fixed = repair_doubled_glyphs(broken)
    assert "연금수령연차" in fixed
    assert "120" in fixed
    assert "연연금금" not in fixed


@pytest.mark.parametrize("text", [
    "평가액 × 120 ÷ (11 − 연금수령연차) ÷ 100",
    "연 1,800만원 한도이며 1100원 단위로 절사",
    "가입자가 사망하거나 해외이주하는 경우",
    "4,500만원 이하 5,500만원 이하 600만원 900만원 16.5%",
    "종류 C-P2, C-P2E 수익증권 투자신탁",
    "2013년 3월 1일 이전 가입 계좌는 6년차부터 기산",
])
def test_정상_텍스트는_건드리지_않는다(text):
    """전역 중복 제거는 '1100원' 같은 정상 표기를 망가뜨린다."""
    from app.ingest.loader import repair_doubled_glyphs

    assert repair_doubled_glyphs(text) == text


def test_mock_코퍼스에_오탐이_없다():
    import glob
    import zipfile

    from app.ingest.loader import repair_doubled_glyphs

    changed = []
    for path in glob.glob("data/corpus_mock/*.zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                try:
                    text = z.read(name).decode("utf-8", "replace")
                except Exception:      # noqa: BLE001 — 바이너리 항목은 건너뛴다
                    continue
                if repair_doubled_glyphs(text) != text:
                    changed.append(f"{path}::{name}")
    assert not changed, f"정상 코퍼스가 변형됐다: {changed}"


# ════════════════════════════════════════════════════════════════
# 결함 2 · 근거가 빗나갔는데 "확인할 사항 없음"이라고 단언
# ════════════════════════════════════════════════════════════════
# 검색이 빗나갔는지는 이 단계에서 알 수 없다. 완결 선언은 근거가 엉뚱해도
# 확신에 찬 문장으로 나간다 — 무관한 회사 연혁을 근거로 답하면서 이 문구를
# 붙인 사례가 있다.

def test_완결_선언_문구를_쓰지_않는다():
    from app.generation import answer_prompt as ap

    assert "추가 확인이 필요한 사항은 없습니다." not in ap.SUPERVISOR_SYSTEM_PROMPT


def test_템플릿_한계고지가_완결을_선언하지_않는다():
    from app.generation.answer_prompt import render_template_answer

    out = render_template_answer({"user_conditions": {}}, [], [])
    assert "추가 확인이 필요한 사항은 없습니다" not in out
    # 구획 표시 대신 한계 고지 '내용'이 실제로 있는지를 본다
    assert ("달라질 수 있습니다" in out or "확인해 주시면" in out
            or "확정하기 어렵습니다" in out), out


# ════════════════════════════════════════════════════════════════
# 오해를 부르는 플래그 출력
# ════════════════════════════════════════════════════════════════

def test_한도_초과가_아니면_그_줄을_싣지_않는다():
    """'초과 = 아니오'는 정보가 없으면서 경고처럼 읽힌다.

    실제로 900만원 납입 건에서 L5'가 이 줄을 보고
    "연간 납입한도를 초과했습니다"라고 쓴 사례가 있다.
    """
    text = _render("총급여 8천만원인데 IRP에 900만원 넣으면 얼마나 돌려받나요?",
                   TAX_CREDIT_FN)
    assert "초과" not in text
    assert "세액공제액" in text


def test_가입불가는_False라도_반드시_싣는다():
    """False가 곧 결론인 키까지 숨기면 안 된다 — 오탐 억제의 반대 방향."""
    from app.generation.render import _SKIP_IF_FALSE

    assert "eligible" not in _SKIP_IF_FALSE
    assert render_calc_result({"eligible": False}).strip() != ""


# ════════════════════════════════════════════════════════════════
# 실서버 재검증에서 발견된 결함 2건 (CHK-03, 300건 감사와 무관한 신규 발견)
# ════════════════════════════════════════════════════════════════
# 위 라우팅 수정을 서버에 반영한 뒤 실제 응답으로 재검증하는 과정에서
# 발견됐다. 300건 감사에는 없던, 라우팅 수정이 계산 슬롯을 더 자주 만들게
# 되면서 비로소 드러난 결함들이다.

def test_같은_키에_규칙값이_없어도_원문_금액을_천장으로_쓴다():
    """900만원 납입인데 L1이 pension_saving_manwon=9,000,000을 준 사례.

    규칙 파서는 이 질의에서 combined_contribution_manwon만 채우고
    pension_saving_manwon은 비워 둔다. 예전 가드는 "같은 키에 규칙값이
    있을 때만" 비교했으므로, 비교 대상이 없어 900만배 부풀려진 값이
    그대로 통과했다. 그 결과 900만원 납입인데 "연간 납입한도(1,800만원)
    초과"로 잘못 표시됐다.
    """
    question = "총급여 5천만원 근로자가 연금계좌에 900만원 납입하면 환급액은 얼마인가요?"
    c = derive_conditions(question, llm_conditions={"pension_saving_manwon": 9000000})
    assert "pension_saving_manwon" not in c
    assert c.get("combined_contribution_manwon") == 900

    from app.core.pension_calc_functions import calc_private_contribution_limit

    out = calc_private_contribution_limit(
        c.get("pension_saving_manwon"), c.get("combined_contribution_manwon"), 0.165)
    assert out["IsLimitExceeded"] is False
    assert out["A_tax_credit"] == pytest.approx(148.5)


def test_정상_LLM_금액은_그대로_유지된다():
    """오염 방어가 정상 값까지 지우면 안 된다 — 양방향 회귀."""
    c = derive_conditions("계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?",
                          llm_conditions={"account_value_manwon": 10000, "pension_year": 1})
    assert c.get("account_value_manwon") == 10000


def test_같은_자릿수_내_미세_차이는_LLM_값을_따른다():
    """자릿수가 어긋난 게 아니라 L1이 문맥을 더 정확히 읽은 경우다."""
    c = derive_conditions("계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?",
                          llm_conditions={"account_value_manwon": 10000.5})
    assert c.get("account_value_manwon") == pytest.approx(10000.5)


def test_LLM이_자기_표현으로_낸_계산_슬롯이_규칙_슬롯과_중복되지_않는다():
    """L1이 파라프레이즈한 설명으로 계산 슬롯을 내면, 규칙이 같은 계산함수를
    다시 추가해 [조건별 결론]에 같은 계산 결과가 두 번 실렸다(id는 다르지만
    calc_function이 같은 경우 — 예전 dedup은 id로만 걸렀다).
    """
    question = "총급여 5천만원 근로자가 연금계좌에 900만원 납입하면 환급액은 얼마인가요?"
    fb = rule_based_spec(question)
    llm = sanitize_spec({
        "intent": "세액공제",
        "asked_for": [
            {"id": "s1", "description": "총급여 5천만 원 근로자의 세액 공제 후 환급액",
             "type": "calculation", "required": True,
             "calc_function": TAX_CREDIT_FN},
        ],
        "search_terms": [], "plan": [],
    }, question)
    out = reconcile_spec(llm, fb, question)
    fns = [s.get("calc_function") for s in out["asked_for"] if s.get("calc_function")]
    assert fns.count(TAX_CREDIT_FN) == 1, f"계산 슬롯이 중복됐다: {fns}"
    assert len(out["planned_calls"]) == len(
        {c["function"] for c in out["planned_calls"]}), "planned_calls도 중복 없어야 한다"


def test_규칙_계산슬롯_dedup이_사실_슬롯까지_지우지_않는다():
    """계산은 중복 제거하되, 근거 검색을 유도하는 사실 슬롯은 남아야 한다."""
    question = "총급여 5천만원 근로자가 연금계좌에 900만원 납입하면 환급액은 얼마인가요?"
    fb = rule_based_spec(question)
    llm = sanitize_spec({
        "intent": "세액공제",
        "asked_for": [
            {"id": "s1", "description": "환급액",
             "type": "calculation", "required": True,
             "calc_function": TAX_CREDIT_FN},
        ],
        "search_terms": [], "plan": [],
    }, question)
    out = reconcile_spec(llm, fb, question)
    ids = [s["id"] for s in out["asked_for"]]
    assert "seaek_gongje_fact" in ids


# ════════════════════════════════════════════════════════════════
# 검증기가 맞는 답을 깎아내던 결함 2건 (2026-08-29 실서버 실측)
# ════════════════════════════════════════════════════════════════
# 두 건 모두 "계산은 맞는데 검증이 답을 반려"시켜, 재생성 실패 → 등급
# 강등까지 갔다. 300건 재현에서 점수가 전혀 안 오른 원인이기도 하다.

def test_사용자가_말한_숫자를_되짚어도_날조가_아니다():
    """L10 실측 — 질의의 나이·금액이 '근거 없는 수치'로 잡혀 축퇴됐다."""
    from app.core.numeric_verifier import verify_numeric_grounding

    q = "만 65세가 연금으로 연 1200만원 받으면 세금은 얼마인가요?"
    calc = [{"r_withholding": 0.055, "T_withholding": 66.0}]
    ans = "만 65세이시고 연 1,200만원을 수령하시면 세율은 5.5%입니다."

    r = verify_numeric_grounding(ans, calc, [], question=q)
    assert r.passed, f"질의의 수치가 날조로 잡혔다: {r.ungrounded}"


def test_만원단위_계산값을_원단위로_써도_날조가_아니다():
    """계산함수는 만원 단위인데 답변은 원 단위로 쓰는 일이 흔하다."""
    from app.core.numeric_verifier import verify_numeric_grounding

    calc = [{"T_withholding": 66.0}]
    r = verify_numeric_grounding("원천징수세액은 660,000원입니다.", calc, [])
    assert r.passed, f"만원→원 표기가 날조로 잡혔다: {r.ungrounded}"


def test_단위환산은_금액키에만_적용된다():
    """모든 수에 ×10000을 적용하면 날조를 통과시킨다."""
    from app.core.numeric_verifier import _flatten_numbers

    nums = _flatten_numbers({"pension_year": 5, "T_withholding": 66.0})
    assert 660000.0 in nums, "금액 키는 원 단위도 허용해야 한다"
    assert 50000.0 not in nums, "연차 같은 비금액 키까지 환산하면 안 된다"


def test_계산값이_나온_질의는_상수_한도를_요구하지_않는다():
    """A08 실측 — 묻지도 않은 900·1,800만원이 없다고 REVISE→강등됐다."""
    from app.core.numeric_verifier import verify_calc_presence

    calc = [{"variants": [
        {"label": "총급여 5,500만원 이하",
         "result": {"연금저축_단독_한도": 600, "연금저축_IRP_합산_한도": 900,
                    "연간_총납입한도": 1800, "A_tax_credit": 99.0}},
        {"label": "총급여 5,500만원 초과",
         "result": {"연금저축_단독_한도": 600, "연금저축_IRP_합산_한도": 900,
                    "연간_총납입한도": 1800, "A_tax_credit": 79.2}},
    ]}]
    ans = "연금저축 단독 한도 600만원이므로 99만원 또는 79.2만원을 공제받습니다."

    p = verify_calc_presence(ans, calc)
    assert p.passed, f"묻지 않은 한도까지 요구했다: {[m[0] for m in p.missing]}"


def test_한도만_안내하는_질의는_여전히_한도를_요구한다():
    """E-01 회귀 — 계산값이 없으면 한도가 곧 답이므로 반드시 실려야 한다."""
    from app.core.numeric_verifier import verify_calc_presence

    calc = [{"연금저축_단독_한도": 600, "연금저축_IRP_합산_한도": 900,
             "연간_총납입한도": 1800, "note": "납입액 미확인"}]

    p = verify_calc_presence("한도가 정해져 있습니다.", calc)
    assert not p.passed, "한도 질의인데 한도 누락을 놓쳤다"
    assert len(p.missing) == 3


# ════════════════════════════════════════════════════════════════
# F35 · routing._CALC_CONDITION_KEYS 전수 감사 — 미검증 키 2종 발견
# ════════════════════════════════════════════════════════════════
#
# F27·F28·F34가 반복 수정한 것은 전부 같은 결함이었다: LLM(HCX)이 낸
# 값이 routing._CALC_CONDITION_KEYS의 멤버로 검증 없이 채워지면, 그
# 존재만으로 ADVISORY(개인 서술 상담)를 GENERAL(계산 경로)로 강제
# 전환한다. F34 수정 후 "_manwon 접미사가 있는 8개 키는 전부
# _GUARDED_MONEY_KEYS로 덮였는가"를 전수 대조했더니 누락은 없었지만
# (`money_keys - _GUARDED_MONEY_KEYS == set()`), _CALC_CONDITION_KEYS
# 에는 _manwon이 아닌 키도 있다: actual_receipt_year·children_total·
# pension_year·service_years·years_elapsed. 이 중 4개는
# _NUMERIC_CONDITION_KEYS/_BOUNDS로 이미 검증됐지만 **children_total
# 하나만 두 집합 어디에도 없어서** LLM 병합 루프의 catch-all
# `else: c[k] = v`로 떨어졌다 — 숫자 검증도 범위 검증도 없이 "2명"
# 같은 문자열까지 그대로 저장됐다.
#
# 같은 감사에서 계산 조건 키는 아니지만 routing.classify_route의
# has_account 신호(그 자체로 GENERAL을 강제)를 이루는 account_type·
# fund_class도 완전 자유 문자열이라 같은 위험이 있음을 확인했다.
# has_calc_slot(HCX가 지정한 calc_function)은 이미 supervise_plan()의
# 화이트리스트가 classify_route보다 먼저 걸러내므로 이 감사의 대상이
# 아니다(pipeline.py — supervise_plan 561행이 classify_route 581행보다
# 앞선다).
#
# 수정: children_total을 _NUMERIC_CONDITION_KEYS/_BOUNDS(0~10)에 추가.
# account_type은 _ACCOUNT_SIGNALS의 알려진 라벨(IRP/연금저축/퇴직연금)만,
# fund_class는 규칙 기반 추출과 동일한 클래스 표기 정규식만 허용한다.

def test_children_total에_비숫자_값이_반영되지_않는다():
    """★ 실측 재현 — '2명' 같은 문자열이 검증 없이 그대로 저장됐다."""
    from app.analysis.conditions import derive_conditions

    q = "24살에 현금 3000만원 있고 아이도 있는데 노후대비 어떻게 해야할까?"
    c = derive_conditions(q, llm_conditions={"age": 24, "children_total": "2명"})
    assert "children_total" not in c


def test_children_total에_있을_수_없는_값이_반영되지_않는다():
    """범위 검증 — 자녀 수가 10명을 넘는 값은 있을 수 없는 값이다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("아이가 있는데 어떻게 해야 할까요?",
                          llm_conditions={"children_total": 999})
    assert "children_total" not in c


def test_children_total_정당한_값은_그대로_반영된다():
    """대조군 — 범위 안의 정상 값까지 막으면 출산크레딧 계산이 죽는다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("자녀 2명 있는데 출산크레딧 얼마나 받나요?",
                          llm_conditions={"children_total": 2})
    assert c.get("children_total") == 2.0


def test_children_total_무검증_라우팅_오염이_더이상_없다():
    """★ 배선 — 비숫자 값이 사라지면 ADVISORY 질의가 GENERAL로 안 끌려간다."""
    from app.analysis.conditions import derive_conditions
    from app.analysis.routing import classify_route

    q = "24살에 현금 3000만원 있고 아이도 있는데 노후대비 어떻게 해야할까?"
    c = derive_conditions(q, llm_conditions={"age": 24, "children_total": "2명"})
    assert classify_route(q, c).route == "ADVISORY"


def test_account_type에_알수없는_값이_반영되지_않는다():
    """★ account_type은 있기만 해도 GENERAL을 강제한다 — 자유 문자열 금지."""
    from app.analysis.conditions import derive_conditions
    from app.analysis.routing import classify_route

    q = "24살에 현금 3000만원 있고 아이도 있는데 노후대비 어떻게 해야할까?"
    c = derive_conditions(q, llm_conditions={"age": 24, "account_type": "아무거나"})
    assert "account_type" not in c
    assert classify_route(q, c).route == "ADVISORY"


def test_account_type_정당한_라벨은_그대로_반영된다():
    """대조군 — 실제 계좌유형 언급까지 막으면 계좌 기반 계산이 죽는다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("연금 계획이 궁금해요",
                          llm_conditions={"account_type": "연금저축"})
    assert c.get("account_type") == "연금저축"


def test_fund_class에_알수없는_표기가_반영되지_않는다():
    """판매클래스도 자유 문자열이면 규칙 기반 추출과 기준이 어긋난다."""
    from app.analysis.conditions import derive_conditions
    from app.analysis.routing import classify_route

    q = "24살에 현금 3000만원 있고 아이도 있는데 노후대비 어떻게 해야할까?"
    c = derive_conditions(q, llm_conditions={"age": 24, "fund_class": "AAAA"})
    assert "fund_class" not in c
    assert classify_route(q, c).route == "ADVISORY"


def test_fund_class_정당한_표기는_그대로_반영된다():
    """대조군 — 실제 클래스 표기까지 막으면 안 된다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("판매클래스 비교하고 싶어요",
                          llm_conditions={"fund_class": "C-P2E"})
    assert c.get("fund_class") == "C-P2E"


# ════════════════════════════════════════════════════════════════
# F36 · '그 외 소득'이 과세방식 비교 계산에 반영되지 않는 결함 (UI-027)
# ════════════════════════════════════════════════════════════════
#
# 실측 (2026-09-06) — "연간 사적연금 수령액이 2000만원이고 그외 소득액에
# 소득공제를 적용하면 7000만원이야. …분리과세와 종합과세중 어떤 것으로
# 선택해야 이득일까"에 대해 답변이 종합과세 유리(253만원 차이)로 나왔다.
# 그런데 이 결과는 other_comprehensive_income=0(그 외 소득이 아예 없는
# 것으로 계산한 값)과 정확히 같다 — 7000만원이 통째로 무시됐다.
#
# 원인: calc_params.py는 compare_taxation_options()의
# other_comprehensive_income 인자를 conditions["other_income_manwon"]에서
# 읽지만, 이 키를 채우는 경로가 **어디에도 없었다** — 규칙 기반 추출에도
# 없고 L1 프롬프트의 user_conditions 스키마 예시에도 없어 HCX가 뽑아도
# extra_conditions로 새 나가 계산에 쓰이지 않았다(연금 외 종합소득은
# total_income_manwon과는 다른 키인데, 그 키 자체가 존재하지 않았다).
#
# 수정: ① "그 외 소득"류 표현 전용 규칙 기반 추출을 추가했다.
# ② "종합소득"이라고 부른 total_income_manwon은 개념이 같으므로
# other_income_manwon으로도 반영한다("총급여"는 소득공제 전 금액이라
# 근사도 안 되므로 제외). ③ L1 프롬프트 스키마에 other_income_manwon을
# 추가하고 total_income_manwon과의 구분을 명시했다. ④ 이 키도 다른
# 화폐 키와 같은 오분류 위험(F28/F34)이 있으므로 _GUARDED_MONEY_KEYS에
# 추가했다 — LLM이 "현금 3000만원"을 other_income_manwon으로 잘못
# 라벨링해도 반영되지 않는다.

def test_그외_소득_표현이_반영된다():
    """★ 실측 재현 — UI-027의 '그외 소득액' 7000만원이 통째로 빠졌었다."""
    from app.analysis.conditions import derive_conditions

    q = ("연간 사적연금 수령액이 2000만원이고 그외 소득액에 소득공제를 "
         "적용하면 7000만원이야. 분리과세와 종합과세중 어떤 것으로 "
         "선택해야 이득일까")
    c = derive_conditions(q)
    assert c.get("other_income_manwon") == 7000.0


def test_그외_소득_반영_후_계산_결과가_달라진다():
    """★ 배선 — 값이 반영되면 유불리 판정 자체가 뒤집힌다.

    other_comprehensive_income=0으로 계산하면 종합과세가 253만원 유리하다고
    나오지만, 실제로 7000만원을 반영하면 종합과세 과세표준이 크게 올라가
    분리과세가 유리한 쪽으로 뒤집힌다 — 숫자를 무시한 결과가 결론 자체를
    바꿔 놓았다는 뜻이다.
    """
    from app.core.pension_calc_functions import compare_taxation_options

    ignored = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                       other_comprehensive_income=0)
    reflected = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                        other_comprehensive_income=7000)
    assert ignored["lower_tax_option"] == "COMPREHENSIVE"
    assert reflected["lower_tax_option"] == "SEPARATE"


def test_종합소득이라고_부른_총소득은_그_외_소득으로도_반영된다():
    """대조군 — '종합소득'은 other_comprehensive_income과 개념이 같다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("종합소득이 7000만원이고 사적연금 2000만원 받는데 "
                          "분리과세가 나을까요 종합과세가 나을까요?")
    assert c.get("other_income_manwon") == 7000.0


def test_총급여는_그_외_소득으로_폴백되지_않는다():
    """★ 회귀 방지 — 총급여(공제 전)를 그 외 소득(공제 후)으로 근사하면 안 된다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("총급여가 7000만원이고 사적연금 2000만원 받는데 "
                          "분리과세가 나을까요 종합과세가 나을까요?")
    assert c.get("other_income_manwon") is None
    assert c.get("total_income_manwon") == 7000.0


def test_LLM이_현금을_그_외_소득으로_오판해도_반영하지_않는다():
    """★ F28/F34류 재확인 — 새로 추가한 화폐 키도 같은 가드를 받아야 한다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("현금 3000만원 있고 사적연금 2000만원인데 과세방식은?",
                          llm_conditions={"other_income_manwon": 3000})
    assert "other_income_manwon" not in c


def test_LLM이_준_정당한_그_외_소득은_반영된다():
    """대조군 — LLM이 스키마대로 정확히 낸 값까지 막으면 안 된다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("사적연금 2000만원인데 과세방식은?",
                          llm_conditions={"other_income_manwon": 5000})
    assert c.get("other_income_manwon") == 5000.0


def test_사적연금이_낀_연간수령액_표현도_잡힌다():
    """★ 같은 실측(UI-027) — '연간 사적연금 수령액이 2000만원'도 놓쳤었다.

    기존 키워드("연간 연금수령액"·"연간 수령액"·"연 연금수령액")는 '연간'과
    '수령액' 사이에 '사적연금'이 끼는 흔한 형태를 못 잡았다. 그 결과
    other_income_manwon 하나만 고쳐서는 이 실측 질의가 여전히 계산되지
    않았다 — private_pension_annual_manwon 자체가 비어 있었기 때문이다.
    """
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("연간 사적연금 수령액이 2000만원이고 그외 소득액에 "
                          "소득공제를 적용하면 7000만원이야. 분리과세와 "
                          "종합과세중 어떤 것으로 선택해야 이득일까")
    assert c.get("private_pension_annual_manwon") == 2000.0
    assert c.get("other_income_manwon") == 7000.0


def test_과세방식_비교_답변이_두_수치를_모두_반영한다():
    """★ 배선 — 파이프라인 끝까지 가서 7000만원이 무시된 결과가 아닌지 본다.

    other_comprehensive_income=0으로 계산하면 종합과세가 유리하다고 나오지만
    (253만원 차이), 실제로는 분리과세가 15.8만원 유리한 쪽으로 뒤집힌다.
    잘못된 결론(종합과세 유리)이 나가면 이 수정이 배선까지 안 됐다는 뜻이다.
    """
    from app.pipeline import answer_question

    r = answer_question(
        "ROUND-2",
        "연간 사적연금 수령액이 2000만원이고 그외 소득액에 소득공제를 "
        "적용하면 7000만원이야. 연금소득세의 1500만원 초과분을 분리과세와 "
        "종합과세중 어떤 것으로 선택해야 이득일까")
    assert "8,160만원" in r["answer"], "그 외 소득 7000만원이 과세표준에 반영되지 않았다"
    assert "분리과세" in r["answer"] and "낮습니다" in r["answer"]


# ════════════════════════════════════════════════════════════════
# F40 · 계산 과정의 투명성은 프롬프트가 아니라 think_trace가 보장한다
# ════════════════════════════════════════════════════════════════
#
# 사용자 요청 — "세율 정보가 안 나타나는데 이유가 뭐야?"에 대한 진단으로
# SUPERVISOR_SYSTEM_PROMPT에 "세율을 함께 쓰라"는 규칙을 추가했었다(F39).
# 그런데 사용자가 이어서 정정했다 — "내가 정확히 원한 건 세금 계산 과정을
# response에 투명하게 공개하는 것이지, 프롬프트를 손대는 게 아니다."
#
# 프롬프트 지시는 HyperCLOVA X가 안 지킬 수 있다(권고일 뿐 강제가 아니다 —
# 마크다운 금지 지시를 어긴 전례가 CLAUDE.md에 이미 있다). 반면 think_trace는
# **결정론적으로 조립되는 필드**라 LLM이 무엇을 쓰든 항상 같은 내용이 실린다.
# 그래서 F39의 프롬프트 규칙(규칙 10)은 되돌리고, 대신
# `TraceLogger.entries()`가 계산 단계(`결정론적_계산_실행`)의 산출값을
# `render_calc_result()`로 렌더링해 think_trace에 직접 싣도록 했다 —
# 답변 생성에 쓰는 것과 **같은 렌더러**라 표시가 어긋나지 않는다.

def test_세율_지시가_프롬프트에서_제거됐다():
    """★ 사용자가 명시적으로 정정한 방향 — 프롬프트 수정이 아니라 응답 자체.

    rule 9의 예시("...16.5%가 적용됩니다")는 이 기능과 무관한 기존 규칙이라
    "세율"이라는 낱말 자체는 여전히 나온다 — F39가 추가했던 규칙 10
    ("그 세율도 함께 쓰십시오")만 없어졌는지를 본다.
    """
    from app.generation import answer_prompt as ap

    assert "그 세율도" not in ap.SUPERVISOR_SYSTEM_PROMPT
    assert "억지로" not in ap.SUPERVISOR_SYSTEM_PROMPT


def test_계산_결과가_think_trace에_그대로_노출된다():
    """★ 배선 — LLM이 뭐라고 쓰든 think_trace에는 항상 계산 과정이 실린다."""
    from app.pipeline import answer_question

    r = answer_question(
        "ROUND-3",
        "연간 사적연금 수령액이 2000만원이고 그외 소득액에 소득공제를 "
        "적용하면 7000만원이야. 분리과세와 종합과세중 어떤 것으로 "
        "선택해야 이득일까")
    trace = r["think_trace"]
    assert "세율 16.5%" in trace
    assert "실효세율 17.15%" in trace
    assert "실효세율 18.64%" in trace
    assert "8,160만원" in trace


def test_계산_없는_질의는_think_trace가_늘어나지_않는다():
    """대조군 — 계산 슬롯이 없으면 이 렌더링이 아예 발동하지 않아야 한다."""
    from app.core.coverage_pipeline import TraceLogger

    trace = TraceLogger()
    trace.log("L0_분류", "일반 문의로 분류")
    entries = trace.entries()
    assert entries == ["[0.0ms] L0_분류 — 일반 문의로 분류"]


def test_계산_결과가_없으면_렌더링을_건너뛴다():
    """★ 회귀 방지 — output이 None이거나 없는 계산 단계에서 예외가 나면 안 된다."""
    from app.core.coverage_pipeline import TraceLogger

    trace = TraceLogger()
    trace.log("결정론적_계산_실행", "계산 완료", slot_id="s1")  # output 키 없음
    entries = trace.entries()
    assert len(entries) == 1
    assert "\n" not in entries[0]


# ════════════════════════════════════════════════════════════════
# F44 · 근속연수·납입기간이 연금수령연차로 새는 결함 (UI-043)
# ════════════════════════════════════════════════════════════════
#
# 실측(2026-09-07) — "DB형 회사에 30년간 다녔고 … 30년간 연금저축·IRP를
# 납입해왔어 … 연금을 처음 수령하기 시작할 때 최대 인출 한도는?"에서
# L1이 pension_year=30으로 채웠다. 사용자가 말한 30은 근속연수·납입
# 기간이지 연금수령연차가 아니다 — "처음 수령"이라는 말 자체가 1년차를
# 뜻하는데, "연금수령연차 30년차 → 한도 없음(무제한)"이라는 완전히
# 틀린 결론이 나갔다. F27/F28/F34가 잡은 것과 같은 계열(그럴듯하지만
# 다른 개념의 숫자가 계산 조건으로 샘)이지만, 대상 키가 금액이 아니라
# pension_year·actual_receipt_year라는 점이 다르다.
#
# 같은 보고에서 사용자가 "여기서 왜 나이를 제시했는데도 나이에 따라서
# 명확한 세율이 규명되지 않아?"라고 지적한 두 번째 결함도 함께 고쳤다 —
# "원천징수" TopicRule의 키워드("세금 얼마" 등)가 "세금은 얼마를 내게
# 되는지"처럼 조사가 끼는 흔한 어순을 못 잡아 계산 슬롯 자체가 안
# 만들어졌고, 나이(55세)가 확인됐는데도 사적연금_원천징수_계산이 돌지
# 않아 근거 문서의 "5.5~3.3%" 범위 서술만 답변에 남았다(F36/F41과 같은
# narrow-trigger 계열).

def test_근속연수와_같은_숫자는_연금수령연차로_반영되지_않는다():
    """★ 실측 재현 — 30년 근속·납입이 연금수령연차 30년차로 새면 안 된다.

    이 질의는 "처음 수령"이라는 표현도 함께 있어, LLM의 30(오분류)을
    거부한 자리에 아래 '처음 수령 → 1년차' 보완이 대신 채운다. 그래서
    최종 pension_year는 '없음'이 아니라 정확히 1이어야 한다 — 이게
    "값이 없는 것"과 "정확히 다른 값(1)이어야 하는 것"의 차이다.
    """
    from app.analysis.conditions import derive_conditions

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "또한 30년간 연간 연금저축 600만원, irp에 300만원을 납입해왔어. "
         "55세 기준으로, 연금을 처음 수령하기 시작할 때 최대 인출 한도는 얼마이고, "
         "그때 세금은 얼마를 내게 되는지 계산해줘.")
    c = derive_conditions(q, llm_conditions={"pension_year": 30, "age": 55})
    assert c.get("pension_year") == 1, "30년(근속·납입)이 아니라 '처음 수령'=1년차여야 한다"
    assert c.get("service_years") == 30
    assert c.get("age") == 55.0


def test_처음_수령이라는_말_자체가_1년차를_뜻한다():
    """★ 실측 보완 — pension_year를 되묻지 않고 산출 가능한 값을 낸다."""
    from app.analysis.conditions import derive_conditions

    assert derive_conditions("연금을 처음 수령하기 시작할 때 한도가 얼마예요?").get(
        "pension_year") == 1
    assert derive_conditions("첫 수령 시 한도가 얼마인가요?").get("pension_year") == 1


def test_가입_시점의_처음은_연차로_오인하지_않는다():
    """★ 오탐 방지 — '처음 가입'은 수령과 무관하다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("IRP에 처음 가입했는데 세액공제가 얼마나 되나요?")
    assert "pension_year" not in c


def test_이미_년차가_확인되면_처음_수령_보완이_덮어쓰지_않는다():
    """대조군 — 실제 명시된 연차가 있으면 그 값을 지킨다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("연금 5년차인데 처음 수령했을 때랑 지금이랑 뭐가 다른가요?")
    assert c.get("pension_year") == 5


# ════════════════════════════════════════════════════════════════
# F44-2 · 월소득이 연금수령한도의 계좌평가액으로 새는 결함 (UI-043)
# ════════════════════════════════════════════════════════════════
#
# 같은 실측에서 세 번째 결함도 발견했다 — calc_params.py의
# "연금수령한도_계산" ParamSpec이 account_value를
# `_first("account_value_manwon", "amount_manwon")`로 조달했다.
# conditions.py에는 이미 같은 값을 훨씬 안전하게 채우는 전용 로직이
# 있는데("수령한도 질의의 무맥락 금액은 계좌 평가액으로 본다" — 용도가
# 이미 밝혀진 금액이면 건드리지 않는 _PURPOSED 가드 포함), calc_params.py
# 쪽 폴백은 그 가드를 우회해 amount_manwon을 그대로 가져왔다. 그 결과
# "월소득은 800만원"(이미 다른 목적으로 언급된 금액)이 연금계좌 평가액인
# 것처럼 계산에 들어갔다. F27이 severance_pay에서 고친 것과 정확히 같은
# 패턴이라 같은 방식(폴백 제거)으로 고쳤다.

def test_이미_purposed된_금액은_계좌평가액으로_새지_않는다():
    """★ 실측 재현 — 월소득 800만원이 계좌 평가액으로 잘못 채워졌었다."""
    from app.analysis.calc_params import CalcParamsBuilder
    from app.analysis.conditions import derive_conditions
    from app.core.coverage_pipeline import RequirementSlot

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "또한 30년간 연간 연금저축 600만원, irp에 300만원을 납입해왔어. "
         "55세 기준으로, 연금을 처음 수령하기 시작할 때 최대 인출 한도는 얼마이고, "
         "그때 세금은 얼마를 내게 되는지 계산해줘.")
    c = derive_conditions(q, llm_conditions={"age": 55})
    assert "account_value_manwon" not in c   # 조건 자체가 안 채워짐

    builder = CalcParamsBuilder(conditions=c)
    slot = RequirementSlot("t1", "연금수령한도", "calculation",
                           calc_function="연금수령한도_계산")
    try:
        builder(slot)
        assert False, "account_value가 없는데 계산이 성공하면 안 된다"
    except Exception as e:
        assert "account_value" in e.missing_params


def test_무맥락_금액은_여전히_계좌평가액으로_반영된다():
    """대조군(E-05) — 용도가 안 밝혀진 금액까지 막으면 계산이 죽는다."""
    from app.analysis.calc_params import CalcParamsBuilder
    from app.analysis.conditions import derive_conditions
    from app.core.coverage_pipeline import RequirementSlot

    c = derive_conditions("1억이고 연금수령 10년차면 한도가 얼마인가요?")
    builder = CalcParamsBuilder(conditions=c)
    slot = RequirementSlot("t1", "연금수령한도", "calculation",
                           calc_function="연금수령한도_계산")
    params = builder(slot)
    assert params["account_value"] == 10000.0


def test_년차가_직접_명시되면_같은_값이어도_반영된다():
    """대조군 — 우연히 같은 값이라도 'OO년차' 직접 표현이 있으면 막지 않는다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("근속 10년이고 연금수령 10년차인데 한도가 얼마인가요?",
                          llm_conditions={"pension_year": 10, "service_years": 10})
    assert c.get("pension_year") == 10.0


def test_다른_값이면_근속연수와_달라도_그대로_반영된다():
    """대조군 — service_years와 다른 값이면 이 가드가 끼어들면 안 된다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("근속 30년인데 연금수령 5년차 한도가 얼마인가요?",
                          llm_conditions={"pension_year": 5, "service_years": 30})
    assert c.get("pension_year") == 5.0


def test_세금은_얼마_표현도_원천징수_계산을_발동시킨다():
    """★ 실측 재현 — '세금 얼마'만으로는 조사가 낀 흔한 어순을 못 잡았다."""
    from app.analysis.query_spec import rule_based_spec

    q = "연금 받으면 세금은 얼마를 내게 되는지 계산해줘"
    fns = [c["function"] for c in rule_based_spec(q)["planned_calls"]]
    assert "사적연금_원천징수_계산" in fns


def test_UI043_전체_질의가_두_계산_슬롯을_모두_만든다():
    """★ 배선 — 한도 계산과 세율 계산이 둘 다 생성돼야 한다."""
    from app.analysis.query_spec import rule_based_spec

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "또한 30년간 연간 연금저축 600만원, irp에 300만원을 납입해왔어. "
         "55세 기준으로, 연금을 처음 수령하기 시작할 때 최대 인출 한도는 얼마이고, "
         "그때 세금은 얼마를 내게 되는지 계산해줘.")
    fns = [c["function"] for c in rule_based_spec(q)["planned_calls"]]
    assert "연금수령한도_계산" in fns
    assert "사적연금_원천징수_계산" in fns


# ════════════════════════════════════════════════════════════════
# F45 · 월소득이 연금계좌 평가액으로 새는 결함 (UI-045)
# ════════════════════════════════════════════════════════════════
#
# 실측(2026-09-07) — "DB형 회사에 30년간 다녔고 퇴직 직전 월소득은
# 800만원이야. 55세 기준으로 종신형으로 처음 수령할 때 이 db계좌에서
# 최대 인출 한도는?"에서 답변이 "첫 해의 인출 한도는 96만원"이라는
# 터무니없는 값을 냈다. 역산하면 1.2×800/max(1,11-1)=96 — 800(월소득)이
# 그대로 연금계좌 평가액으로 쓰인 것이다. 사용자가 직접 지적했다:
# "이게 DB형 연금에 들어있는 총액으로 보고 계산한 것 같다."
#
# 원인은 두 겹이었다:
# ① LLM 조건 병합 루프의 _GUARDED_MONEY_KEYS 가드는
#    _is_non_pension_asset_amount(현금·예적금)만 보고 소득은 안 봤다.
#    "월소득"도 _INCOME_NOUN 목록에 없어 _is_income_amount 자체가 이
#    표현을 인식하지 못했다.
# ② "수령한도 질의의 무맥락 금액은 계좌 평가액으로 본다"는 규칙 기반
#    로직(E-05 대응)도 같은 구멍이 있었다 — "용도가 이미 조건 키로
#    자리 잡았는가"(_PURPOSED)만 봤지, 원문에 소득 표지가 있는지 자체는
#    보지 않았다. total_income_manwon 조건 키가 안 만들어졌을 뿐 "월소득"
#    이라는 용도 표지는 원문에 분명히 있었는데도 "용도 불명"으로 오판했다.
#
# 두 곳 모두 _is_income_amount로 소득 표지를 직접 확인하도록 고쳤다.

def test_월소득이_계좌평가액으로_반영되지_않는다():
    """★ 실측 재현 — LLM이 직접 account_value_manwon으로 라벨링한 경우."""
    from app.analysis.conditions import derive_conditions

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "55세 기준으로 연금을 종신형으로 수령할때 처음 수령하기 시작할 때 "
         "이 db계좌에서 최대 인출 한도는 얼마이고, 그때 세금은 얼마를 내게 "
         "되는지 계산해줘.")
    c = derive_conditions(q, llm_conditions={"account_value_manwon": 800, "age": 55})
    assert "account_value_manwon" not in c


def test_무맥락_금액_규칙도_월소득을_계좌평가액으로_삼지_않는다():
    """★ 실측 재현 — LLM 조건 없이도(순수 규칙 경로) 같은 오분류가 있었다.

    _PURPOSED 검사가 조건 키 존재 여부만 봐서, "월소득"이라는 용도
    표지가 원문에 있는데도 "용도 불명"으로 오판해 800을 계좌 평가액으로
    확정해 버렸다.
    """
    from app.analysis.conditions import derive_conditions

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "55세 기준으로 연금을 종신형으로 수령할때 처음 수령하기 시작할 때 "
         "이 db계좌에서 최대 인출 한도는 얼마이고, 그때 세금은 얼마를 내게 "
         "되는지 계산해줘.")
    c = derive_conditions(q)   # LLM 조건 없이 순수 규칙 경로만
    assert "account_value_manwon" not in c


def test_E05_대조군_무맥락_금액은_여전히_계좌평가액으로_반영된다():
    """대조군 — 소득 표지가 없는 무맥락 금액까지 막으면 계산이 죽는다."""
    from app.analysis.conditions import derive_conditions

    c = derive_conditions("1억이고 연금수령 10년차면 한도가 얼마인가요?")
    assert c.get("account_value_manwon") == 10000.0


def test_계산_결과가_잘못된_숫자_대신_확인_요청으로_바뀐다():
    """★ 배선 — 파이프라인 끝까지 가서 '96만원' 같은 오답이 사라졌는지 본다."""
    from app.pipeline import answer_question

    q = ("db형을 운용하는 회사에 30년간 다녔고 퇴직 직전 월소득은 800만원이야. "
         "55세 기준으로 연금을 종신형으로 수령할때 처음 수령하기 시작할 때 "
         "이 db계좌에서 최대 인출 한도는 얼마이고, 그때 세금은 얼마를 내게 "
         "되는지 계산해줘.")
    r = answer_question("ROUND-4", q)
    assert "96만원" not in r["answer"]
    assert "연금계좌 평가액" in r["answer"]


# ═══════════════════════════════════════════════════════════════════
# F46 · 공적연금이 사적연금 계산으로 들어가던 결함 (2026-09-06)
#
# "국민연금을 매달 200만원 받습니다. 세금은 얼마나 내나요"에서 월 수령액
# 정규식이 연금 **종류**를 구별하지 않아 200이 private_pension_monthly_manwon
# 으로 들어갔다. 연 2,400만원으로 환산돼 과세방식_비교_계산이 돌았고,
# "분리과세 396만원 vs 종합과세 112.2만원, 종합과세가 283.8만원 유리"라는
# **법적으로 존재하지 않는 세액**이 나갔다 — 공적연금에는 그 선택권이 없다.
# 같은 답변이 인용한 doc39는 "공적연금과 이연퇴직소득은 1,500만원 판정
# 대상에서 제외한다"였다(답변이 자기 근거로 자신을 반박).
#
# 기존 가드 3종은 모두 "이 돈이 어떤 종류인가"(소득·현금·근속연수)를 봤고,
# "누구의 연금인가" 축은 가드가 없었다.
# ═══════════════════════════════════════════════════════════════════

import pytest


@pytest.mark.parametrize("q", [
    "국민연금을 매달 200만원 받습니다. 세금은 얼마나 내나요",
    "군인연금 연 4800만원 수령하는데 세금이 얼마인가요",
    "아버지가 공무원연금 월 300만원 받으시는데 노후 대비를 어떻게 해야 할까요",
    "매달 200만원씩 국민연금을 받습니다. 세금은요",      # 명사가 금액 뒤
    "사학연금 월 250만원 받는데 세금은 얼마인가요",
])
def test_공적연금_수령액은_사적연금_조건이_되지_않는다(q):
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(q)
    assert "private_pension_monthly_manwon" not in c
    assert "private_pension_annual_manwon" not in c


def test_공적연금_가드는_LLM_경로에서도_동작한다():
    """★ F45의 교훈 — 같은 조건에 도달하는 다른 경로가 또 있는가.

    규칙 경로 3곳을 막아도 LLM 조건 병합 루프가 열려 있으면 같은 오답이
    그대로 나간다.
    """
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(
        "국민연금 월 150만원 받는데 노후 준비를 어떻게 해야 하나요",
        llm_conditions={"private_pension_monthly_manwon": 150})
    assert "private_pension_monthly_manwon" not in c


@pytest.mark.parametrize("q,key,expected", [
    ("연금저축에서 매달 200만원 받는데 세금은 얼마인가요",
     "private_pension_monthly_manwon", 200.0),
    ("연간 사적연금 수령액이 2000만원인데 세금이 얼마예요",
     "private_pension_annual_manwon", 2000.0),
    ("연 1200만원 받으면 원천징수세율이 몇 퍼센트인가요",
     "private_pension_annual_manwon", 1200.0),
    ("IRP에서 매월 150만원 수령하는데 세율이 어떻게 되나요",
     "private_pension_monthly_manwon", 150.0),
    # 공적연금이 문장에 **언급**되기만 하고 금액은 사적연금인 경우 —
    # 창을 넓히면 이런 정상 질의가 막힌다(그래서 앞 12자·뒤 8자로 고정).
    ("연금저축에서 월 200만원 받는데 국민연금은 별도예요. 세금은?",
     "private_pension_monthly_manwon", 200.0),
])
def test_대조군_정상_사적연금_금액은_그대로_반영된다(q, key, expected):
    from app.analysis.conditions import derive_conditions

    assert derive_conditions(q).get(key) == expected


@pytest.mark.parametrize("q,key,expected", [
    ("국민연금 월 100만원 받고 연금저축에서 월 200만원 받는데 세금은?",
     "private_pension_monthly_manwon", 200.0),
    ("국민연금 연 1200만원 받고 사적연금은 연 2000만원 받는데 세금은?",
     "private_pension_annual_manwon", 2000.0),
])
def test_공적연금과_사적연금이_함께_나오면_사적연금만_고른다(q, key, expected):
    """re.search가 아니라 finditer여야 한다.

    첫 매치(공적연금)에서 멈추면 뒤의 정상적인 사적연금 금액을 통째로
    놓쳐, 답할 수 있는 질의를 못 답하게 된다.
    """
    from app.analysis.conditions import derive_conditions

    assert derive_conditions(q).get(key) == expected


def test_공적연금_제외_사실이_고객_답변에_고지된다():
    """★ 배선 — 조건을 봤으면서 아무 말 없이 버리면 '확인된 조건이 없다'는
    부정확한 안내가 나간다. 평가지표 '정보한계 대응'은 한계 고지를 요구한다.

    ⚠️ diagnostic_notes가 아니라 condition_notes로 실어야 한다 — 전자는
       F21 이후 내부 진단 전용이라 고객 문장에 도달하지 않는다.
    """
    from app.pipeline import answer_question

    r = answer_question("F46-WIRE", "국민연금을 매달 200만원 받습니다. 세금은 얼마나 내나요")
    assert "공적연금" in r["answer"]
    # 존재하지 않는 분리과세 선택 세액이 다시 나오면 안 된다
    assert "분리과세를 선택하면" not in r["answer"]


# ═══════════════════════════════════════════════════════════════════
# F48(2) · 국민연금 계산함수가 규칙 경로에서 도달 불가였던 결함
#
# CALC_REGISTRY에 국민연금_본인부담금_계산·국민연금_수령액_계산·
# 출산크레딧_인정개월_계산 3종이 등록돼 있고 calc_params 스펙도 있는데,
# TOPIC_RULES의 "국민연금" 규칙이 calc_function=None이라 규칙 경로에서는
# 슬롯이 만들어지지 않았다. L1(HCX)이 직접 지정할 때만 돌았고, 그게 F41에서
# 근거 없이 지정돼 오라우팅을 일으킨 바로 그 경로다.
# ═══════════════════════════════════════════════════════════════════


def test_국민연금_본인부담금이_규칙_경로에서_계산된다():
    """★ 배선 — 400만원 × 9% × 0.5 = 18만원."""
    from app.pipeline import answer_question

    r = answer_question("F48-NP", "월소득이 400만원인데 국민연금 본인부담금이 얼마인가요")
    assert "18만원" in r["answer"]
    assert "월 기준소득월액" in r["answer"]      # 조건 라벨도 표시돼야 한다


def test_월_기준소득월액이_조건으로_추출된다():
    """monthly_income_manwon이 없으면 위 배선은 calc_needs를 못 채워 죽는다.

    ⚠️ avg_monthly_wage_manwon(DB형 평균임금)과 값이 같아질 수 있으나
       별개 키다 — 한쪽을 다른 쪽 폴백으로 쓰는 _first(...) 패턴은
       F27·F44에서 오답의 원인이었다.
    """
    from app.analysis.conditions import derive_conditions

    assert derive_conditions("월소득이 400만원인데").get("monthly_income_manwon") == 400.0
    assert derive_conditions("기준소득월액 350만원").get("monthly_income_manwon") == 350.0


def test_출산크레딧은_자녀수가_있을_때만_슬롯을_만든다():
    """calc_needs 게이팅 — 제도 설명만 묻는 질의에 빈 계산 카드를 내지 않는다."""
    from app.analysis.query_spec import rule_based_spec

    with_child = rule_based_spec("자녀가 2명 있는데 출산크레딧 인정개월수가 얼마인가요")
    fns = {s.get("calc_function") for s in with_child.get("asked_for") or []}
    assert "출산크레딧_인정개월_계산" in fns

    plain = rule_based_spec("출산크레딧이 뭔가요?")
    fns2 = {s.get("calc_function") for s in plain.get("asked_for") or []}
    assert "출산크레딧_인정개월_계산" not in fns2


def test_국민연금_수령액은_일부러_배선하지_않는다():
    """★ 의도적 미배선 — 소득대체율(r_irr)은 사용자가 말해 주는 값이 아니고
    제공 자료로도 확정할 수 없다. 배선하면 "국민연금 얼마 받나요"마다
    '적용 소득대체율'을 되묻는 슬롯이 생기는데, 그건 답이 아니라 알 수 없는
    용어를 되묻는 것이다. 검색 기반 일반 슬롯이 제도 문서를 찾아오게 둔다.
    """
    from app.analysis.query_spec import TOPIC_RULES

    wired = {r.calc_function for r in TOPIC_RULES}
    assert "국민연금_수령액_계산" not in wired
    assert "국민연금_본인부담금_계산" in wired
    assert "출산크레딧_인정개월_계산" in wired


def test_제도_설명_질의에는_국민연금_계산이_붙지_않는다():
    """빈 계산 카드 방지 — DB형/DC형과 같은 calc_needs 게이팅 원칙."""
    from app.pipeline import answer_question

    r = answer_question("F48-PLAIN", "국민연금이 뭐예요?")
    assert "본인부담금" not in r["answer"]


# ═══════════════════════════════════════════════════════════════════
# F48(1) · 연금 외 수령(해지·중도인출) 재원별 세금 계산함수 신설
#
# 함정 A9에 도메인 지식은 완전히 있는데 **계산기가 없었다.** "IRP에 퇴직금
# 1억, 세액공제 받은 납입액 3천만원, 운용수익 2천만원이 있는데 지금
# 해지하면 세금이 얼마인가요"에 수치가 하나도 안 나가고 함정 교정 문장만
# 실렸다(과제 안내 '기타(중도인출 등)' 유형).
#
# 투자설명서 [연금저축계좌 과세 주요 사항]:
#   · 이연퇴직소득(퇴직급여 이체분) → 퇴직소득 과세기준
#   · 세액공제 받은 납입액 + 운용수익 → 기타소득세 16.5%
#   · 세액공제 받지 않은 납입액 → 과세 제외
# "해지하면 전액 16.5%"는 오답이다.
# ═══════════════════════════════════════════════════════════════════


def test_재원별로_다른_과세기준이_적용된다():
    from app.core.pension_calc_functions import calc_non_pension_withdrawal_tax

    r = calc_non_pension_withdrawal_tax(
        credited_contribution=3000, investment_gain=2000,
        uncredited_contribution=1000)
    # 세액공제분 + 운용수익 = 5,000만원만 과세, × 16.5% = 825만원
    assert r["기타소득세_과세대상"] == 5000
    assert r["기타소득세액"] == 825.0
    assert r["과세제외_납입액"] == 1000       # 세액공제 안 받은 납입액은 과세 제외
    assert r["합계세액"] == 825.0
    # 전액(6,000만원)에 16.5%를 매기면 990만원 — 그 오답이 나오면 안 된다
    assert r["합계세액"] != 990.0


def test_퇴직소득세율을_모르면_세액을_지어내지_않는다():
    """★ 핵심 안전 속성 — 0은 사실이 아니라 미입력이다.

    퇴직소득세는 근속연수공제·환산급여 구조라 단일 실효세율이 없다.
    0으로 굴리면 사용자에게 "세금이 0원"으로 읽힌다
    (calc_private_withholding이 이미 세운 원칙과 같다).
    """
    from app.core.pension_calc_functions import calc_non_pension_withdrawal_tax

    r = calc_non_pension_withdrawal_tax(
        deferred_severance=10000, credited_contribution=3000,
        investment_gain=2000)
    assert r["이연퇴직소득_세액"] is None
    assert r["합계세액"] is None              # 합계도 낼 수 없다
    assert r["기타소득세액"] == 825.0         # 확정 가능한 몫은 그대로 낸다
    assert "미확정_사유" in r
    # 이연퇴직소득 1억에 16.5%를 매기면 1,650만원 — 절대 나오면 안 된다
    assert all(row.get("세액") != 1650.0 for row in r["재원별_내역"])


def test_퇴직소득세율을_주면_합계가_나온다():
    from app.core.pension_calc_functions import calc_non_pension_withdrawal_tax

    r = calc_non_pension_withdrawal_tax(
        deferred_severance=10000, credited_contribution=3000,
        investment_gain=2000, severance_effective_rate=0.05)
    assert r["이연퇴직소득_세액"] == 500.0     # 10000 × 5%
    assert r["합계세액"] == 1325.0             # 500 + 825


def test_재원_금액이_조건으로_추출된다():
    """calc_needs를 못 채우면 배선이 죽은 코드가 된다.

    ⚠️ _is_balance_amount 가드를 걸면 안 된다 — 이 세 재원은 본래 잔액
       성격이라 "운용수익 2천만원이 있는데"의 '있는데'가 잔고 표지로 걸려
       통째로 막혔다(실측).
    """
    from app.analysis.conditions import derive_conditions

    q = ("IRP에 퇴직금 1억, 세액공제 받은 납입액 3천만원, 운용수익 "
         "2천만원이 있는데 지금 해지하면 세금이 얼마인가요")
    c = derive_conditions(q)
    assert c.get("credited_contribution_manwon") == 3000.0
    assert c.get("investment_gain_manwon") == 2000.0
    assert c.get("severance_manwon") == 10000.0


def test_재원별_세금이_답변에_수치로_나간다():
    """★ 배선 — 이전에는 함정 교정 문장만 나가고 수치가 0건이었다."""
    from app.pipeline import answer_question

    q = ("IRP에 퇴직금 1억, 세액공제 받은 납입액 3천만원, 운용수익 "
         "2천만원이 있는데 지금 해지하면 세금이 얼마인가요")
    a = answer_question("F48-SRC", q)["answer"]
    assert "825만원" in a
    assert "별도 산출 필요" in a          # 이연퇴직소득분은 지어내지 않는다
    # ⚠️ F24/F25 계열 — 중첩 구조를 범용 렌더러에 맡기면 raw 키가 샌다
    assert "재원별_내역" not in a
    assert "기타소득세_과세대상" not in a


def test_재원_금액이_없으면_빈_계산_카드를_만들지_않는다():
    """calc_needs 게이팅 — DB형/DC형과 같은 원칙."""
    from app.pipeline import answer_question

    a = answer_question("F48-PLAIN2", "중도인출 사유가 뭔가요?")["answer"]
    assert "재원마다 과세기준이 다릅니다" not in a


# ═══════════════════════════════════════════════════════════════════
# F46 후속 · 단위 없는 숫자는 위치 기반 가드 전체가 눈이 먼다
#
# _is_public_pension_amount는 parse_amount_expressions가 찾아낸 금액의
# **위치**를 보고 판정한다. "공무원연금 월 300 받으시고"처럼 사용자가
# 단위 없이 숫자만 쓰면 그 파서가 표현을 못 잡아 위치가 없고, 따라서 이
# 계열 가드 전부(소득·현금·공적연금)가 눈이 먼다. 규칙 경로는 애초에
# 금액을 못 잡으니 무해하지만 **LLM은 단위가 없어도 값을 읽어 채운다.**
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("q,llm", [
    ("아버지가 공무원연금 월 300 받으시고 예금 5천만원 있으신데 어떻게 굴리는 게 좋을까요",
     {"private_pension_monthly_manwon": 300}),
    ("국민연금 월 150 받는데 노후 준비 어떻게",
     {"private_pension_monthly_manwon": 150}),
])
def test_공적연금만_언급된_질의는_사적연금_값을_받지_않는다(q, llm):
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(q, llm_conditions=llm)
    assert "private_pension_monthly_manwon" not in c


@pytest.mark.parametrize("q,llm,expected", [
    # 사적연금 명사가 하나라도 있으면 발동하지 않는다
    ("국민연금 말고 개인연금 월 200 받는데 세금은?",
     {"private_pension_monthly_manwon": 200}, 200.0),
    ("국민연금 외에 연금저축에서 월 200 받습니다. 세금은?",
     {"private_pension_monthly_manwon": 200}, 200.0),
    ("IRP에서 월 150 받는데 세율이 어떻게 되나요",
     {"private_pension_monthly_manwon": 150}, 150.0),
    # 공적연금 명사가 아예 없으면 발동하지 않는다 — 아무 연금도 특정하지
    # 않은 질의까지 막으면 정상 계산이 죽는다
    ("매달 200만원 받는데 세금은 얼마인가요",
     {"private_pension_monthly_manwon": 200}, 200.0),
])
def test_대조군_사적연금_근거가_있으면_그대로_반영된다(q, llm, expected):
    from app.analysis.conditions import derive_conditions

    c = derive_conditions(q, llm_conditions=llm)
    assert c.get("private_pension_monthly_manwon") == expected


# ═══════════════════════════════════════════════════════════════════
# F49 · 안내서 상품 비교 예시 질의가 상품 비교 규칙에 안 걸렸다
#
# 안내서 7페이지 대주제 2 예시:
#   "솔로몬 국공채 단기·중장기·장기, 뭐가 달라요? 안정적인 걸 원해요."
# 상품_비교 TopicRule의 키워드는 ("총보수","보수가 낮","수수료 비교",
# "어떤 클래스","비교해","저렴한")뿐이라 **하나도 안 걸렸다.** 사용자는
# "총보수"라고 말하지 않고 "뭐가 달라요"라고 말한다.
# 그 결과 대주제 2의 핵심 유형이 일반 폴백 슬롯으로 떨어져 무관한 문서
# (명예퇴직금 처리)를 근거로 답했다 — 평가지표 '근거 완전성'의
# "질의 대상과 무관한 근거를 배제했는가"를 정면으로 어긴다.
#
# ⚠️ 비교어를 그냥 넓히면 안 된다 — "뭐가 달라"·"차이"는 제도 비교에도
#    똑같이 쓰인다. require_any(AND 게이트)로 **상품 차원**임을 요구한다.
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("q", [
    "솔로몬 국공채 단기 · 중장기 · 장기, 뭐가 달라요? 안정적인 걸 원해요.",
    "이 펀드랑 저 펀드 차이가 뭔가요",
    "TDF랑 인덱스펀드 뭐가 달라요",
    "위험등급 3등급이랑 4등급 어떤 차이가 있나요",
    "채권형이랑 주식형 상품 어느 게 나은가요",
])
def test_상품_차원_비교질의가_상품_비교로_잡힌다(q):
    from app.analysis.query_spec import rule_based_spec

    assert rule_based_spec(q).get("intent") == "상품_비교", q


@pytest.mark.parametrize("q,expected", [
    # ★ 제도 비교는 끌려오면 안 된다 — 안내서 난이도 '하'의 기초 제도 질의다
    ("DC와 DB, 퇴직금이 정해지는 방식이랑 운용 주체가 어떻게 다른가요?", "일반"),
    ("연금저축이랑 IRP 차이가 뭔가요", "일반"),
    ("연금저축이랑 IRP 차이점이 뭐예요", "일반"),
    # 세제 비교는 원래 규칙이 가져가야 한다
    ("분리과세랑 종합과세 뭐가 달라요", "과세방식"),
])
def test_대조군_제도_세제_비교는_상품_비교로_가지_않는다(q, expected):
    from app.analysis.query_spec import rule_based_spec

    assert rule_based_spec(q).get("intent") == expected, q


def test_한국어_축약형_뭔가요도_잡는다():
    """'뭔'은 '뭐+ㄴ'의 축약이라 "차이가 뭐"가 "차이가 뭔가요"를 포함하지
    **않는다.** 한국어 부분문자열 매칭의 전형적인 함정(CLAUDE.md)."""
    from app.analysis.query_spec import rule_based_spec

    assert rule_based_spec("이 펀드 차이가 뭔가요").get("intent") == "상품_비교"
    assert rule_based_spec("이 펀드 차이가 뭐예요").get("intent") == "상품_비교"


def test_상품_차원_어휘는_L0_분류와_같은_목록을_쓴다():
    """같은 판단(이 질의가 상품 차원인가)을 두 곳이 다른 기준으로 하면
    반드시 어긋난다 — L0는 '상품'으로 분류하는데 주제 규칙만 모르는 상태가
    바로 F49의 본질이었다."""
    from app.analysis.query_spec import _PRODUCT_DIMENSION
    from app.core.grounding_retrieval import DOMAIN_AREAS

    assert set(DOMAIN_AREAS["상품"]) <= set(_PRODUCT_DIMENSION)


def test_상품_비교_질의가_무관한_문서를_근거로_쓰지_않는다():
    """★ 배선 — 이전에는 명예퇴직금 처리 문서가 근거로 실렸다."""
    from app.pipeline import answer_question

    r = answer_question(
        "F49-WIRE", "솔로몬 국공채 단기 · 중장기 · 장기, 뭐가 달라요? 안정적인 걸 원해요.")
    assert "명예퇴직" not in r["retrieved_context"]


# ═══════════════════════════════════════════════════════════════════
# F13 · 수치검증 허용 변형이 99.5~100.5% 구간을 무조건 통과시킴 (기존 결함)
#
# _matches()의 상대오차(rel_tol=0.005)가 **allowed 안의 모든 값**에 대해
# 블록 단위로 적용됐다. 근거에 100(전혀 다른 맥락의 값 — 예: "국민연금
# 가입 상한 연령 100세")이 있으면, 답변이 무관한 날조 수치("예상 세액
# 99.7만원")를 내도 100의 0.5% 이내(99.5~100.5)라는 이유만으로 통과했다.
#
# 표시 반올림(76.56→"77만원")을 흡수하려던 의도였지만, 그 케이스는 이미
# _flatten_numbers가 표시 함수를 그대로 호출해 파생값을 명시적으로 만드는
# 방식으로 별도 처리돼 있었다(2026-08-29). 즉 블록 단위 상대오차는 원래
# 목적에는 필요 없이 남아 있던 것이고, 오직 "무관한 값과 우연히 가까운
# 날조"를 통과시키는 구멍으로만 작동하고 있었다.
# ═══════════════════════════════════════════════════════════════════


def test_무관한_근거값과_우연히_가까운_날조_수치는_차단된다():
    """★ 실측 재현 — 예전에는 이게 통과했다.

    근거의 '100세'(가입 상한 연령)와 아무 관계도 없는 '99.7만원'(세액)이
    옛 0.5% 블록 허용오차 때문에 통과했었다.
    """
    from app.core.numeric_verifier import verify_numeric_grounding

    evidence = ["국민연금 가입 상한 연령은 100세까지 유예 신청이 가능하다."]
    answer = "예상 세액은 99.7만원입니다."
    r = verify_numeric_grounding(answer, [], evidence)
    assert not r.passed
    assert 99.7 in r.ungrounded


def test_실효세율_표시_반올림은_여전히_통과한다():
    """★ 대조군 — 정당한 표시 반올림까지 막으면 안 된다.

    _pct()는 유효숫자 4자리로 반올림한다(0.171504 → "17.15%"). 이 파생값도
    허용 집합에 명시적으로 들어가야 한다(만원 표시 반올림과 같은 원칙).
    """
    from app.core.pension_calc_functions import compare_taxation_options
    from app.generation.render import render_calc_result
    from app.core.numeric_verifier import verify_numeric_grounding

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    text = render_calc_result(r)
    v = verify_numeric_grounding(text, calc_results=[r],
                                 question="사적연금 2000만원 그외소득 7000만원")
    assert v.passed, v.ungrounded


def test_1500만원_기준액이_계산결과에_명시적으로_실린다():
    """★ 배선 — 이전에는 이 상수가 계산 결과 어디에도 없어, 답변이 우연히
    근처 값(예: 세액 합계 1504.8)에 걸려 낡은 0.5% 오차로 통과하고
    있었을 뿐이다. 법령 상수이므로 계산 결과에 직접 실어 근거를 만든다.
    """
    from app.core.pension_calc_functions import compare_taxation_options

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    assert r["과세방식_선택_기준액"] == 1500.0

    r2 = compare_taxation_options(P_np_annual=0, P_private_pension_annual=1000)
    assert r2["과세방식_선택_기준액"] == 1500.0


def test_1500만원_기준액은_강제표기_대상이_아니다():
    """LLM이 다른 말로 바꿔 쓸 수 있는 고정 진술문의 상수라 답변에 다른
    표현으로 실려도 누락으로 잡히면 안 된다(F24/F25류 — 새 계산 키가
    강제표기 대상으로 새어 들어가지 않게 할 것)."""
    from app.core.pension_calc_functions import compare_taxation_options
    from app.core.numeric_verifier import verify_calc_presence

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    p = verify_calc_presence("세액 차이는 15.8만원입니다.", [r])
    assert p.passed, [m[0] for m in p.missing]


def test_강제표기_검증도_표시_반올림값을_인정한다():
    """★ verify_calc_presence 방향 — raw 15.84든 표시형 15.8이든 답변에
    있으면 실린 것으로 본다. 한쪽만 인정하면 정상 답변이 '누락'으로
    잘못 잡힌다."""
    from app.core.pension_calc_functions import compare_taxation_options
    from app.core.numeric_verifier import verify_calc_presence

    r = compare_taxation_options(P_np_annual=0, P_private_pension_annual=2000,
                                 other_comprehensive_income=7000)
    p_raw = verify_calc_presence("세액 차이는 15.84만원입니다.", [r])
    p_shown = verify_calc_presence("세액 차이는 15.8만원입니다.", [r])
    assert p_raw.passed
    assert p_shown.passed


# ════════════════════════════════════════════════════════════════
# F50 · 공적연금 전용 질의에 **사적연금 계산 슬롯·함정**이 그대로 만들어져
#       사적연금 세율이 국민연금 세율인 것처럼 답변된 결함 (2026-09-06)
# ════════════════════════════════════════════════════════════════
#
# F46은 "공적연금 금액이 사적연금 조건 키로 새는 것"을 막았다. 그런데 막힌
# 것은 **값**뿐이었다. 슬롯과 함정은 그대로 만들어져, 검색이 연금계좌 세율
# 문서를 끌어오고 답변이 그 문서의 원천징수율·감면율(70%/60%)을 국민연금
# 세율처럼 제시했다 — 인용한 doc39 자신이 "공적연금은 제외한다"고 적고
# 있는데도. 답변이 자기 근거 문서로 자신을 반박한 상태였다(UI 평가 1번).
#
# ⚠️ 이 파일에는 이미 모듈 수준 헬퍼들이 있다. 새 이름만 쓴다
#    (CLAUDE.md — 테스트 파일에 절을 덧붙일 때 기존 헬퍼 이름을 재정의하지 말 것).

_F50_PUBLIC_ONLY = [
    "국민연금을 매달 200만원 받습니다. 세금은 얼마나 내나요",
    "공무원연금 받는데 종합과세 되나요",
    "군인연금 월 400만원 받는데 세금 얼마나 떼나요",
]


@pytest.mark.parametrize("q", _F50_PUBLIC_ONLY)
def test_공적연금_전용_질의에는_사적연금_계산슬롯이_생기지_않는다(q):
    """★ 값만 막고 슬롯을 남기면 검색이 대상이 다른 근거를 끌어온다
    (평가지표 '근거 완전성' — 대상이 다른 근거를 배제했는가)."""
    spec = rule_based_spec(q)
    calc_fns = {c["function"] for c in spec["planned_calls"]}
    assert "사적연금_원천징수_계산" not in calc_fns, calc_fns
    assert "과세방식_비교_계산" not in calc_fns, calc_fns


@pytest.mark.parametrize("q,expected_fn", [
    ("국민연금 말고 연금저축에서 받는 연금은 세금 얼마인가요",
     "사적연금_원천징수_계산"),
    ("IRP에서 연 2000만원 받으면 분리과세가 나을까요",
     "과세방식_비교_계산"),
])
def test_사적연금이_함께_언급되면_계산슬롯은_그대로_만들어진다(q, expected_fn):
    """★ 게이트가 좁다는 것을 고정한다 — 사적연금 명사가 하나라도 있으면
    F50은 발동하지 않는다. 넓히면 정상 계산 질의가 통째로 죽는다."""
    spec = rule_based_spec(q)
    calc_fns = {c["function"] for c in spec["planned_calls"]}
    assert expected_fn in calc_fns, calc_fns


@pytest.mark.parametrize("q,gone", [
    ("공무원연금 받는데 종합과세 되나요", "C1"),
    ("국민연금 세액공제 되나요", "C4"),
    ("국민연금 중도인출 할 수 있나요", "A2"),
])
def test_연금계좌_전용_함정은_공적연금_질의에서_발화하지_않는다(q, gone):
    """★ critical 함정은 최후에 **강제 삽입**된다. 공적연금 질의에 연금계좌
    규정 교정문을 강제로 붙이면 교정이 아니라 오답의 주입이 된다."""
    from app.core.trap_rules import detect_traps

    assert gone not in {t.id for t in detect_traps(q)}


def test_공적연금_세제혼동_함정이_발화하고_사적연금_질의는_건드리지_않는다():
    """★ C7 — 트리거 비대칭을 없앤다. C2는 사용자가 '1,500만원'을 말해야
    켜지는데, 시스템은 사용자가 말하지 않아도 사적연금 세제를 적용한다."""
    from app.core.trap_rules import detect_traps

    hit = {t.id for t in detect_traps("국민연금을 매달 200만원 받습니다. "
                                      "세금은 얼마나 내나요")}
    assert "C7" in hit
    # 제도만 묻는 질의(세금 맥락 없음)에는 붙지 않는다
    assert "C7" not in {t.id for t in detect_traps("국민연금이 뭐예요?")}
    # 사적연금 명사가 있으면 붙지 않는다
    assert "C7" not in {t.id for t in detect_traps(
        "국민연금 말고 연금저축에서 받는 연금은 세금 얼마인가요")}


def test_배선_공적연금_질의_답변에_사적연금_규정_비적용이_명시된다():
    """★ 배선 테스트 — 부품만 고쳐 놓고 파이프라인이 안 쓰면 그대로 샌다
    (CLAUDE.md '배선을 검사하는 테스트는 배선을 지나가야 한다')."""
    from app.pipeline import answer_question

    r = answer_question("F50-WIRE",
                        "국민연금을 매달 200만원 받습니다. 세금은 얼마나 내나요")
    ans = r["answer"]
    # 연금계좌 규정이 적용되지 않는다는 사실이 답변에 실려야 한다
    assert "적용되지 않" in ans, ans
    # 그리고 맨몸 거절이 아니라 역질문이 함께 나가야 한다
    #  (평가지표 '정보한계 대응' — 한계 고지 또는 역질문)
    assert "연금저축" in ans or "IRP" in ans, ans


# ════════════════════════════════════════════════════════════════
# F55 · 나이 표현이 여럿이면 첫 매치(과거 시작 나이)를 그대로 써서
#        현재 나이 판정이 전부 틀어지던 결함 (2026-09-07 실측, UI-006)
# ════════════════════════════════════════════════════════════════
#
# "25세부터 30년간 IRP에 납입했고 지금 55세인데"에서 `parse_age`가 첫
# 매치인 25(시작 나이)를 반환했다. 실제 현재 나이는 55인데, 그 결과
# `audit_fitness`의 AGE_CONTEXT 감사가 "55세 미만인데 연금수령을
# 전제로 서술함"이라는 **거짓** 경고를 붙였다 — 자격을 충족한 55세
# 이용자에게 나이 미달 경고가 나가는 상태였다.

def test_시작나이와_현재나이가_함께_있으면_현재나이를_고른다():
    from app.analysis.units import parse_age

    q = ("25세부터 30년간 irp에 연마다 300만원씩 납입했고 지금 55세인데, "
         "연금수령으로 인출가능한 최대 금액이 얼마인가요")
    assert parse_age(q) == 55


@pytest.mark.parametrize("q,expected", [
    ("58세인데 65세 되면 국민연금 받을 수 있나요", 58),
    ("국민연금은 65세부터 받을 수 있는데 저는 지금 58세입니다", 58),
    ("만 55세부터 연금 받을 수 있는 거 맞죠? 저는 58세인데", 58),
])
def test_나이_표현_여럿_중_현재나이_판정_다양한_어순(q, expected):
    from app.analysis.units import parse_age

    assert parse_age(q) == expected


def test_나이_표현이_하나뿐이면_기존_동작과_같다():
    """★ 회귀 방지 — 압도적 다수인 단일 나이 표현 질의는 영향받지 않는다."""
    from app.analysis.units import parse_age

    assert parse_age("24살이고 연금 계획 좀") == 24
    assert parse_age("30대 초반인데 연금저축 지금 시작하는 게 좋을까요") is None


def test_안내서_고정_회귀질의는_그대로_58세로_파싱된다():
    """★ CLAUDE.md에 회귀 테스트로 고정된 안내서 예시 질의
    ("58세인데, 크게 잃지 않으면서...")가 이 변경으로 깨지지 않는다."""
    from app.analysis.units import parse_age

    assert parse_age("58세인데, 크게 잃지 않으면서 굴릴 상품 하나 "
                     "추천해 주세요.") == 58


def test_배선_나이_오분류로_인한_거짓_AGE_CONTEXT_경고가_사라진다():
    """★ 배선 테스트 — parse_age만 고쳐도 conditions.py가 그 값을
    실제로 쓰는지 확인한다."""
    from app.analysis.conditions import derive_conditions

    q = ("25세부터 30년간 irp에 연마다 300만원씩 납입했고 지금 55세인데, "
         "연금수령으로 인출가능한 최대 금액이 얼마인가요")
    c = derive_conditions(q)
    assert c.get("age") == 55
