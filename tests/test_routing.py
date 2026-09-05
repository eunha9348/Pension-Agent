"""L1 경로 분류 테스트.

경로 선택은 **결정론적 코드**가 한다 — HCX 재량에 맡기면 같은 질의가
실행마다 다른 계층을 타서 재현도 디버깅도 불가능해진다.

이 파일이 지키는 두 가지:
  ① 개인 사정을 서술하며 방향을 묻는 질의가 ADVISORY로 간다
  ② 계산이 특정되는 질의는 GENERAL을 유지한다 (결정론적 계산이 더 정확)
"""

from __future__ import annotations

import pytest

from app.analysis.conditions import derive_conditions
from app.analysis.query_spec import rule_based_spec
from app.analysis.routing import classify_route


def _route(q: str):
    spec = rule_based_spec(q)
    return classify_route(q, spec.get("user_conditions"), spec.get("asked_for"))


# ── ADVISORY — 이번 개편의 목적 ──────────────────────────────
# 예전 같으면 거절되거나 빈 계산 카드를 받았을 질의들이다.

@pytest.mark.parametrize("q", [
    "나는 24살이고 부동산은 없고 현금 3500만원이 있는데 연금계획을 어떻게 세워야할까?",
    "나 몇살인데 연금 계획 좀",
    "주택청약이 400만원 있는데 노후 대비를 어떻게 해야할까요?",
    "30대 직장인인데 연금 뭐부터 시작해야 하나요?",
    "그럼 어떻게 해야 하나요?",
])
def test_개인_서술형_상담질의는_ADVISORY로_간다(q):
    d = _route(q)
    assert d.is_advisory, f"{q}\n  → {d.as_trace()}"


# ── GENERAL — 결정론적 계산이 더 정확한 질의 ────────────────

@pytest.mark.parametrize("q", [
    "연금저축에 600만원 넣으면 세액공제 얼마인가요?",
    "계좌에 1억원 있고 연금수령 1년차인데 얼마까지 인출할 수 있나요?",
    "퇴직금 2억원 받았고 근속 25년입니다. 퇴직소득세 얼마인가요?",
    "80세면 연금소득세율이 몇 퍼센트인가요?",
    "연금저축이랑 IRP 합쳐서 세액공제 얼마까지 받을 수 있나요?",
])
def test_수치가_주어진_계산질의는_GENERAL을_유지한다(q):
    d = _route(q)
    assert not d.is_advisory, f"{q}\n  → {d.as_trace()}"


@pytest.mark.parametrize("q", [
    "C-P 클래스는 IRP로 가입할 수 있나요?",
    "연금저축은 IRP로 옮길 수 있나요?",
])
def test_계좌유형_클래스가_있으면_GENERAL이다(q):
    """기존 자격·비교 로직이 돌아야 한다."""
    assert not _route(q).is_advisory, q


@pytest.mark.parametrize("q", [
    "1500만원 넘으면 분리과세 선택해야 하나요?",
    "IRP 중도인출 사유가 뭔가요?",
    "연금수령 개시 신청은 어떻게 하나요?",
    "종합소득세 신고는 어떻게 하나요?",
])
def test_정답이_정해진_제도_절차_질의는_GENERAL이다(q):
    """'어떻게 하나요'는 상담 신호가 아니다 — 문서에 답이 있는 절차 질의다.

    '어떻게 하'를 상담 신호로 넣었더니 이런 질의가 전부 끌려왔다(실측).
    """
    assert not _route(q).is_advisory, q


# ── 범용 금액이 판단을 좌우하지 않는다 ──────────────────────

def test_범용_금액은_계산조건으로_치지_않는다():
    """amount_manwon은 '금액이 하나 있으면 담는' 폴백이다.

    이걸 계산 조건으로 세면 "현금 3,500만원이 있는데 연금계획을…" 같은
    질의가 계산 경로로 끌려간다 — 범용 신호가 특정 판단을 좌우하는 형태로,
    이번 개편이 걷어내려는 과최적화 그 자체다.
    """
    from app.analysis.routing import _CALC_CONDITION_KEYS

    assert "amount_manwon" not in _CALC_CONDITION_KEYS

    cond = derive_conditions("현금 3500만원이 있는데 연금계획을 어떻게 세워야 할까요?")
    assert cond.get("amount_manwon") == 3500      # 파싱은 된다
    assert classify_route("현금 3500만원이 있는데 연금계획을 어떻게 세워야 할까요?",
                          cond).is_advisory       # 그러나 계산 경로로 가지 않는다


# ── 판정은 추적 가능해야 한다 ────────────────────────────────

def test_판정_사유가_항상_남는다():
    for q in ["나 몇살인데 연금 계획 좀", "연금저축에 600만원 넣으면 세액공제 얼마인가요?"]:
        d = _route(q)
        assert d.reason, q
        assert d.route in ("GENERAL", "ADVISORY")
        assert "경로" in d.as_trace()


def test_같은_질의는_항상_같은_경로다():
    """결정론성 — 이게 깨지면 재현도 디버깅도 불가능해진다."""
    q = "나는 24살이고 현금 3500만원이 있는데 연금계획을 어떻게 세워야할까?"
    routes = {_route(q).route for _ in range(5)}
    assert len(routes) == 1


# ── 라우팅이 기존 평가셋을 흔들지 않는다 ────────────────────

def test_평가셋_대부분은_GENERAL을_유지한다():
    """개편으로 기존 계산 경로가 무너지면 안 된다."""
    from tests.eval_set import EVAL_CASES

    adv = [c.id for c in EVAL_CASES if _route(c.question).is_advisory]
    assert len(adv) <= 2, f"평가셋이 과도하게 ADVISORY로 샜다: {adv}"


# ── 나이 파싱 — 구어체 '살' ──────────────────────────────────

@pytest.mark.parametrize("q,expected", [
    ("24살이고 연금 계획", 24),
    ("만 65세가 연금으로 받으면", 65),
    ("나이는 55살입니다", 55),
    ("만 80살이면 세율이 얼마인가요", 80),
])
def test_구어체_살도_나이로_읽는다(q, expected):
    """문서는 '세'로 쓰지만 사람은 '살'로 말한다.

    이게 빠져 있어서 "24살이고 연금 계획 좀"이 나이 미확인으로 처리됐고,
    연령별 차등과세가 걸린 질의에서 조건이 통째로 비었다.
    """
    assert derive_conditions(q).get("age") == expected, q


# ════════════════════════════════════════════════════════════════
# F41 · 계산 조건 없이 계산함수만 지정되면 상담 신호를 이기지 못한다
# ════════════════════════════════════════════════════════════════
#
# 실측 (UI-020, 2026-09-06) — "우리 아빠가 모아둔 자산이 부동산은 20억
# 정도 군인연금은 월 400정도를 받는데 노후를 어떻게 대비해야할까"에서
# L1(HCX)이 뒷받침하는 조건 하나 없이 '국민연금_수령액_계산'을 슬롯에
# 지정했다. supervise_plan은 이 함수가 CALC_REGISTRY에 **등록만** 돼
# 있으면 통과시킨다(관련성은 검증하지 않는다). has_calc_cond=False인데도
# has_calc_slot=True만으로 GENERAL이 강제됐고, 계산은 필수 인자 부족으로
# 곧바로 실패해 "확인이 필요합니다"라는 무의미한 답변만 남았다 — 질문
#자체는 전형적인 ADVISORY 문구("노후를 어떻게 대비")였는데도.
#
# 두 가지를 함께 고쳤다:
# ① classify_route의 판정 순서 — "상담 신호 + 개인 서술"이 "계산함수
#    지정"보다 우선하도록 옮겼다. has_calc_cond(진짜 수치)가 있으면
#    여전히 최우선으로 GENERAL이 확정되므로 정상적인 계산 질의는
#    영향받지 않는다.
# ② _ADVISORY_SIGNALS에 '어떻게 대비'·'노후 준비'를 추가했다 — '노후'와
#    '대비' 사이에 조사('를')가 끼는 흔한 어순이라 기존 '노후 대비'
#    리터럴로는 안 걸렸다(①만 고쳐도 이 실측 질의 자체는 여전히
#    통과하지 못했다 — 상담신호=0이라 애초에 advisory_signal 자체가
#    비어 있었다).

def test_실측_질의가_ADVISORY로_간다():
    """★ 실측 재현 — UI-020 그대로."""
    q = ("우리 아빠가 모아둔 자산이 부동산은 20억 정도 군인연금은 월 "
         "400정도를 받는데 노후를 어떻게 대비해야할까")
    assert _route(q).is_advisory, _route(q).as_trace()


def test_뒷받침_없는_계산함수_지정은_상담신호를_이기지_못한다():
    """★ 핵심 불변식 — L1이 근거 없이 계산함수를 지정해도 상담 신호가 이긴다."""
    from app.analysis.conditions import derive_conditions
    from app.analysis.routing import classify_route

    q = "우리 아빠가 부동산 20억 있고 군인연금 받는데 노후를 어떻게 대비해야할까"
    cond = derive_conditions(q)  # has_calc_cond=False (뒷받침 수치 없음)
    slots = [{"id": "s1", "calc_function": "국민연금_수령액_계산"}]
    d = classify_route(q, cond, slots)
    assert d.is_advisory, d.as_trace()


def test_계산_조건이_실제로_있으면_계산함수_지정이_여전히_이긴다():
    """대조군 — has_calc_cond가 있으면 순서 변경과 무관하게 GENERAL이어야 한다."""
    from app.analysis.routing import classify_route

    q = "월 300만원 받는데 어떻게 준비해야 할까"  # 상담 신호 + 개인서술 있음
    cond = {"private_pension_monthly_manwon": 300}  # 그러나 진짜 계산 조건도 있음
    slots = [{"id": "s1", "calc_function": "사적연금_원천징수_계산"}]
    d = classify_route(q, cond, slots)
    assert not d.is_advisory, d.as_trace()


@pytest.mark.parametrize("q", [
    "노후를 어떻게 대비해야 할까요",
    "노후 준비 어떻게 시작하면 좋을까요",
])
def test_조사가_낀_노후_대비_표현도_상담신호로_잡힌다(q):
    """★ 트리거 확장 — '노후'와 '대비/준비' 사이에 조사가 끼는 흔한 어순."""
    from app.analysis.routing import _ADVISORY_SIGNALS

    assert any(s in q for s in _ADVISORY_SIGNALS), q
