"""이상치 감사가 variants 구조에서 통째로 눈이 멀던 결함 (2026-09-03).

━━ 어떻게 발견했는가 ━━
F2(한도 초과 신호)를 넣고 298건 실경로 모사로 발화율을 재 보니
`PENSION_SAVING_LIMIT_EXCEEDED`가 **0건**이었다. A08("연금저축에 1200만원
넣으면 전부 공제되나요?")은 반드시 걸려야 하는 케이스인데 안 걸렸다.

추적해 보니 원인은 F2가 아니었다. 소득을 모르면 `calc_params`가 세율
구간별로 결과를 나눠 담는데(`{"variants": [{"label":…, "result":{…}}]}`),
`audit_anomaly`는 **최상위 키만** 훑고 있었다. 즉 variants 구조에서는
findings가 0건 — 신규 판정뿐 아니라 **기존 LIMIT_EXCEEDED·CREDIT_EXCEEDS·
LIMIT_RATIO까지 통째로** 불발했다. 소득을 밝히지 않는 질의는 흔하다.

`numeric_verifier._presence_targets`는 이미 variants를 재귀로 훑고 있었다 —
같은 구조를 보는 두 계층이 서로 다른 기준을 쓰고 있었던 셈이다.

CLAUDE.md — "감사가 있다는 주장은 결과가 반영될 때만 참이다."
"""

from __future__ import annotations

from app.core.supervisory_board import Verdict, _flatten_variants, audit_anomaly

_FLAT = {"연금저축_단독_한도": 600, "A_tax_credit": 99.0,
        "IsPensionSavingLimitExceeded": True, "IsLimitExceeded": True}

_VARIANTS = {"variants": [
    {"label": "총급여 5,500만원 이하", "result": {
        "연금저축_단독_한도": 600, "A_tax_credit": 99.0,
        "IsPensionSavingLimitExceeded": True, "IsLimitExceeded": True}},
    {"label": "총급여 5,500만원 초과", "result": {
        "연금저축_단독_한도": 600, "A_tax_credit": 79.2,
        "IsPensionSavingLimitExceeded": True, "IsLimitExceeded": True}},
]}


# ── 핵심 불변식 — 두 구조가 같은 판정을 받아야 한다 ───────────

def test_variants_구조도_평면_구조와_같은_판정을_받는다():
    """★ 이번 결함의 핵심 — 예전에는 variants면 0건이었다."""
    flat_codes = {f.code for f in audit_anomaly([_FLAT], {})}
    var_codes = {f.code for f in audit_anomaly([_VARIANTS], {})}
    assert var_codes == flat_codes, (
        f"구조에 따라 판정이 달라진다 — 평면 {flat_codes} vs variants {var_codes}")


def test_variants에서_기존_판정도_함께_살아난다():
    """★ 신규 판정만의 문제가 아니었다 — 기존 LIMIT_EXCEEDED도 불발했다."""
    codes = {f.code for f in audit_anomaly([_VARIANTS], {})}
    assert "LIMIT_EXCEEDED" in codes


def test_variants에서_신규_한도_판정이_발화한다():
    codes = {f.code for f in audit_anomaly([_VARIANTS], {})}
    assert "PENSION_SAVING_LIMIT_EXCEEDED" in codes


def test_판정_등급은_REVISE로_유지된다():
    for f in audit_anomaly([_VARIANTS], {}):
        assert f.severity == Verdict.REVISE


# ── 중복 정리 ────────────────────────────────────────────────

def test_세율_구간마다_같은_지적이_반복되지_않는다():
    """소득 구간을 나눠 계산한 사정 때문에 같은 말을 두 번 하면 안 된다."""
    findings = audit_anomaly([_VARIANTS], {})
    codes = [f.code for f in findings]
    assert len(codes) == len(set(codes)), f"중복 지적: {codes}"


def test_값이_달라_내용이_다른_지적은_각각_살린다():
    """★ 중복 정리가 서로 다른 지적을 삼키면 안 된다.

    CREDIT_EXCEEDS는 detail에 값이 들어가므로 구간별로 내용이 다르다.
    """
    variants = {"variants": [
        {"label": "구간A", "result": {"A_tax_credit": 500.0}},
        {"label": "구간B", "result": {"A_tax_credit": 700.0}},
    ]}
    findings = audit_anomaly([variants], {"annual_contribution": 100})
    details = {f.detail for f in findings if f.code == "CREDIT_EXCEEDS"}
    assert len(details) == 2, f"값이 다른 지적이 합쳐졌다: {details}"


# ── _flatten_variants 단위 ───────────────────────────────────

def test_평면_구조는_그대로_통과시킨다():
    assert _flatten_variants([_FLAT]) == [_FLAT]


def test_variants는_내부_result만_펼친다():
    out = _flatten_variants([_VARIANTS])
    assert len(out) == 2
    assert all("variants" not in r for r in out)
    assert out[0]["A_tax_credit"] == 99.0


def test_망가진_입력에도_예외를_내지_않는다():
    """감사 계층은 입력이 이상해도 죽으면 안 된다 — 죽으면 감사가 사라진다."""
    assert _flatten_variants(None) == []
    assert _flatten_variants([]) == []
    assert _flatten_variants(["문자열", 42, None]) == []
    assert _flatten_variants([{"variants": "리스트가 아님"}]) == [
        {"variants": "리스트가 아님"}]
    assert _flatten_variants([{"variants": [None, "x", {}]}]) == []


def test_평면과_variants가_섞여_있어도_모두_훑는다():
    out = _flatten_variants([_FLAT, _VARIANTS])
    assert len(out) == 3
