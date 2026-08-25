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
    assert "[한계 고지]" in out


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
