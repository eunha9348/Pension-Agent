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
