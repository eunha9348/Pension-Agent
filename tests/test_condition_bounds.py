"""L1 조건 병합 — 비금액 숫자 필드의 범위 검증 (2026-08-29 신설).

━━ 실측으로 발견된 결함 ━━
사용자가 "연간 연금수령액 2,000만원"이라고 물었는데, HCX가 그 "2000"을
pension_year(연금수령연차)에도 잘못 채웠다. calc_params.py의 _VALID
(1~60)가 계산 인자로 쓰일 때는 이 값을 걸렀지만, 이 질의의 계산이
pension_year를 쓰지 않아 그 경계를 타지 않았고, 사용자에게 보여주는
"조건으로 이해했습니다" 줄에는 검증 없이 "연금수령연차 2000.0"이
그대로 노출됐다.

원인: derive_conditions()의 LLM 병합 루프에서 _unit_confusion()은
`_manwon` 접미사가 있는 금액 필드만 본다. age·pension_year·
actual_receipt_year·service_years·years_elapsed는 숫자로 변환만
되고, 있을 수 없는 값(음수·수백~수천)이어도 그대로 저장됐다.

수치는 계산 인자에 쓰이는 _VALID(calc_params.py)와 같은 값을 쓴다 —
두 경계가 어긋나면 "계산에서는 막혔는데 화면에는 새는" 이 결함이
다른 필드에서 또 생긴다.
"""

from __future__ import annotations

from app.analysis.conditions import derive_conditions, describe_conditions

# ── 실측 재현 — 정확히 사용자가 겪은 그 사고 ────────────────────

def test_연금수령연차에_금액_숫자가_새어들어오면_버린다():
    """★ 실측 사고 재현 — '2,000만원'이 pension_year=2000으로 잘못 채워짐."""
    q = "연간 연금수령액 2,000만원 받는데 세금이 어떻게 되나요?"
    c = derive_conditions(q, {"private_pension_annual_manwon": 2000,
                              "pension_year": 2000})
    assert "pension_year" not in c
    desc = describe_conditions(c)
    assert "연금수령연차" not in desc, f"있을 수 없는 값이 화면에 노출됨: {desc!r}"
    assert "2,000만원" in desc            # 정상 금액 조건은 그대로 남는다


# ── 범위를 벗어나면 전부 버린다 (calc_params.py의 _VALID와 같은 경계) ──

def test_있을_수_없는_값은_전부_버려진다():
    cases = [
        ("age", -5), ("age", 500),
        ("pension_year", 0), ("pension_year", 2000),
        ("actual_receipt_year", 0), ("actual_receipt_year", 100),
        ("service_years", 0), ("service_years", 999),
        ("years_elapsed", -1), ("years_elapsed", 200),
    ]
    for key, bad_value in cases:
        c = derive_conditions("질문", {key: bad_value})
        assert key not in c, f"{key}={bad_value} 가 걸러지지 않았다"
        # ⚠️ 2026-09-06 변경 — 기록 채널이 condition_notes → diagnostic_notes로
        #    분리됐다. 불변식("버린 값이 조용히 사라지지 않는다")은 그대로이고,
        #    다만 이 기록은 **내부 진단**이라 고객 문장에 실리지 않아야 한다.
        #    실사용에서 "분석 결과(50,000,000,000만원)가 … 반영하지 않았습니다"가
        #    답변에 그대로 노출된 사고가 있었다(F21과 같은 계열).
        assert c.get("diagnostic_notes"), f"{key}={bad_value} 가 조용히 버려졌다"
        assert not c.get("condition_notes"), \
            f"{key}={bad_value} 의 내부 진단이 고객 문장(condition_notes)에 실렸다"


def test_정상_범위는_그대로_통과한다():
    cases = [("age", 80), ("pension_year", 5), ("actual_receipt_year", 3),
             ("service_years", 25), ("years_elapsed", 10)]
    for key, ok_value in cases:
        c = derive_conditions("질문", {key: ok_value})
        assert c.get(key) == float(ok_value), f"{key}={ok_value} 가 정상인데 걸러졌다"


def test_경계값은_통과한다():
    """1과 60처럼 하한·상한 자체는 유효한 값이다."""
    assert derive_conditions("질문", {"pension_year": 1}).get("pension_year") == 1.0
    assert derive_conditions("질문", {"pension_year": 60}).get("pension_year") == 60.0
    assert derive_conditions("질문", {"years_elapsed": 0}).get("years_elapsed") == 0.0


# ── 규칙 파싱 값이 있으면 그 값을 지킨다 ─────────────────────────

def test_규칙값이_있으면_지어낸_LLM값_대신_규칙값을_쓴다():
    """"5년차"는 규칙이 5로 정확히 읽는다. LLM이 엉뚱한 값을 줘도 규칙을 믿는다."""
    c = derive_conditions("연금 5년차인데 한도가 얼마인가요", {"pension_year": 9999})
    assert c["pension_year"] == 5.0


# ── 표시 형식 — 소수점이 새지 않는다 ────────────────────────────

def test_정수로_떨어지면_소수점_없이_표시한다():
    """"연금수령연차 5.0"은 사람이 쓰는 표현이 아니다."""
    for key, label in [("pension_year", "연금수령연차"),
                       ("actual_receipt_year", "연금실제수령연차"),
                       ("service_years", "근속연수"),
                       ("years_elapsed", "가입 후 경과연수")]:
        c = derive_conditions("질문", {key: 5})
        desc = describe_conditions(c)
        assert f"{label} 5" in desc
        assert f"{label} 5.0" not in desc


# ── 배선 — 계산 인자 경계(calc_params)와 조건 표시 경계가 같은 수치를 쓰는가 ──

def test_조건_경계와_계산_인자_경계가_어긋나지_않는다():
    """두 경계표가 따로 놀면, 계산에서는 막혔는데 화면에는 새는 이번 결함이
    다른 필드에서 다시 생긴다."""
    from app.analysis.calc_params import _VALID
    from app.analysis.conditions import _NUMERIC_CONDITION_BOUNDS

    # calc_params.py 쪽 키 이름은 일부 다르다(Age ↔ age) — 겹치는 것만 대조
    shared = {"pension_year", "actual_receipt_year", "service_years",
             "years_elapsed"}
    for key in shared:
        cond_lo, cond_hi = _NUMERIC_CONDITION_BOUNDS[key]
        calc_lo, calc_hi, _ = _VALID[key]
        assert (cond_lo, cond_hi) == (calc_lo, calc_hi), (
            f"{key}: 조건 경계({cond_lo}, {cond_hi}) != "
            f"계산 인자 경계({calc_lo}, {calc_hi})")
