"""계산 결과 → 사람이 읽는 문장 조각.

LLM에게 넘길 컨텍스트도, 결정론적 템플릿 답변도 여기서 만든 문자열을 쓴다.
**숫자는 전부 계산 결과에서 나온다.** 여기서 새 수치를 만들지 않는다.
"""

from __future__ import annotations

from typing import Any

from app.analysis.units import format_manwon

# 계산함수 반환 키 → 사람이 읽는 이름
_LABELS = {
    "limit": "연금수령한도",
    "denominator": "적용 분모(11 − 연금수령연차)",
    "unlimited": "한도 없음",
    "pension_year": "연금수령연차",
    "special_rule_applied": "2013.3.1 이전 가입 특례 적용",
    "A_tax_credit": "세액공제액",
    "IsLimitExceeded": "연간 납입한도(1,800만원) 초과",
    "IsPensionSavingLimitExceeded": "연금저축 단독 세액공제 한도(600만원) 초과",
    "IsCombinedLimitExceeded": "연금저축+IRP 합산 세액공제 한도(900만원) 초과",
    "연금저축_단독_한도": "연금저축 단독 세액공제 한도",
    "연금저축_IRP_합산_한도": "연금저축+IRP 합산 세액공제 한도",
    "연간_총납입한도": "연간 총 납입한도",
    "r_withholding": "원천징수세율",
    "T_withholding": "원천징수세액",
    "P_private_excess": "1,500만원 초과분",
    "reduction_rate": "이연퇴직소득세 감면율",
    "applied_rate_of_original_tax": "이연퇴직소득세 적용률",
    "band": "해당 구간",
    "C_np_copay": "국민연금 월 본인부담금",
    "M_np_credit": "출산크레딧 인정 개월수",
    "P_np_monthly": "국민연금 월 수령액",
    "P_np_annual": "국민연금 연 수령액",
    "choice_required": "과세방식 선택 대상",
    "lower_tax_option": "세액이 낮은 쪽",
    "difference": "세액 차이",
    "national_rate": "국세 기준 세율",
    "rate_with_local_tax": "지방소득세 포함 세율",
    "eligible": "가입 가능",
    "comparable": "비교 가능한 클래스",
    "excluded": "가입 자격 미충족으로 제외",
    # 퇴직급여 적립액 (수리팀 산식)
    "퇴직급여_적립액": "퇴직급여 적립액(적립 원금 기준)",
    "평균월급": "퇴직 직전 3개월 평균월급",
    "근속연수": "근속연수",
}

# ── 단위 표기 ──────────────────────────────────────────────────
# ⚠️ 휴리스틱만으로 단위를 정하면 사고가 난다. 실제로 "적용 분모 = 10만원"처럼
#    개수를 금액으로 찍는 버그가 있었다. 아는 키는 명시적으로 못 박는다.
_UNIT_MANWON = {
    "연금저축_단독_한도", "연금저축_IRP_합산_한도", "연간_총납입한도",
    "limit", "A_tax_credit", "T_withholding", "P_private_excess", "difference",
    "C_np_copay", "P_np_monthly", "P_np_annual",
    "근속연수공제", "환산급여", "환산급여공제", "퇴직소득_과세표준",
    "환산산출세액", "산출세액", "합계", "과세표준", "연금소득공제",
    "사적연금_분리과세", "그외_종합과세",
    # 퇴직급여 적립액 (수리팀 산식 · DB/DC 공통 출력 키)
    "퇴직급여_적립액", "평균월급",
}
_UNIT_RATE = {
    "r_withholding", "reduction_rate", "applied_rate_of_original_tax",
    "national_rate", "rate_with_local_tax", "r_tax_credit", "r_np_premium",
    "r_irr", "separate_tax_rate_used",
    # ⚠️ 2026-09-05 외부 심사 리포트로 발견 — calc_private_contribution_limit()의
    # 실제 출력 키는 파라미터명 r_tax_credit이 아니라 "세액공제율"이다(호출
    # 측이 인자와 다른 이름으로 반환한다). 이 세트에 없으면 numeric_verifier의
    # _presence_targets가 이 키를 건너뛰어(금액·비율로 분류된 키만 요구) 소득
    # 구간별로 달라야 할 유일한 값(13.2%/16.5%)이 "계산 결과" 강제표기 대상에서
    # 통째로 빠지고, 소득과 무관해 두 구간이 항상 같은 한도 상수(600/900/1800)만
    # 중복 표기됐다(실사용 재현: 두 줄 모두 "600만원").
    "세액공제율",
}
_UNIT_PLAIN = {          # 금액도 비율도 아닌 값 (개수·연차 등)
    "denominator": "",
    "pension_year": "년차",
    "M_np_credit": "개월",
    "service_years": "년",
}

# 명시 목록에 없는 키를 위한 보조 힌트
_RATE_HINTS = ("rate", "율", "r_")
_AMOUNT_HINTS = ("limit", "한도", "세액", "금액", "공제", "T_", "A_", "P_", "C_", "차이")

# 답변에 그대로 노출하지 않는 내부 키
_SKIP_KEYS = {"source", "rate_source", "DEPRECATED", "note", "⚠️", "기준", "action",
              "doc_id", "markers", "is_legacy_suspect", "reason", "params", "label",
              "구간수"}      # 연차 구간 개수는 내부 계산 상세라 답변에 싣지 않는다

# ⚠️ False일 때는 아예 싣지 않는 플래그.
#    "연간 납입한도(1,800만원) 초과 = 아니오"는 아무 정보도 주지 않으면서
#    경고처럼 읽힌다. 실제로 900만원 납입 건에서 L5'가 이 줄을 보고
#    "연간 납입한도를 초과했습니다"라고 쓴 사례가 있다(300건 감사 A03).
#    ⚠️ eligible(가입 가능)처럼 **False가 곧 결론인 키는 넣지 말 것.**
#    "가입 가능 = 아니오"는 반드시 답변에 실려야 한다.
_SKIP_IF_FALSE = {"IsLimitExceeded", "special_rule_applied",
                  "IsPensionSavingLimitExceeded", "IsCombinedLimitExceeded"}


def _is_rate(key: str, value: float) -> bool:
    k = key.lower()
    if any(h in k for h in _RATE_HINTS) or "율" in key:
        return True
    return 0 < value < 1 and "만원" not in key


def format_value(key: str, value: Any) -> str:
    if value is None:
        return "해당 없음"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (int, float)):
        v = float(value)
        # 1) 명시 목록 우선
        if key in _UNIT_RATE:
            return f"{v * 100:.4g}%"
        if key in _UNIT_MANWON:
            return format_manwon(v)
        if key in _UNIT_PLAIN:
            suffix = _UNIT_PLAIN[key]
            return f"{value:,}{suffix}" if suffix else f"{value:,}"
        # 2) 보조 휴리스틱
        if _is_rate(key, v):
            return f"{v * 100:.4g}%"
        if any(h in key for h in _AMOUNT_HINTS):
            return format_manwon(v)
        return f"{value:,}"
    if isinstance(value, list):
        return f"{len(value)}건"
    return str(value)


def label_of(key: str) -> str:
    return _LABELS.get(key, key)


def _render_tax_choice(result: dict, indent: str) -> str:
    """compare_taxation_options()의 결과 전용 문장 렌더러.

    ⚠️ 왜 따로 두는가 — 이 함수만 유일하게 "선택지 두 개를 나눠 비교"하는
    중첩 구조(separate/comprehensive)를 돌려준다. 범용 key=value 나열로
    렌더링하면 "separate:"·"comprehensive:"가 번역 없이 그대로 노출되고
    (_LABELS에 없는 raw 키), 코드처럼 보인다(2026-08-29 실측 — 사용자가
    직접 지적). 숫자는 전부 result dict에서 그대로 가져온다 — 새로
    계산하거나 지어내지 않는다.
    """
    if not result.get("choice_required", False):
        note = result.get("note", "")
        return f"{indent}{note}" if note else f"{indent}선택 대상이 아닙니다."

    sep, comp = result.get("separate") or {}, result.get("comprehensive") or {}
    sep_total = format_manwon(sep.get("합계", 0))
    sep_private = format_manwon(sep.get("사적연금_분리과세", 0))
    sep_other = sep.get("그외_종합과세", 0)

    base = format_manwon(comp.get("과세표준", 0))
    deduction = format_manwon(comp.get("연금소득공제", 0))
    comp_total = format_manwon(comp.get("합계", 0))

    cheaper = "분리과세" if result.get("lower_tax_option") == "SEPARATE" else "종합과세"
    diff = format_manwon(result.get("difference", 0))
    basis = result.get("기준", "")

    sep_detail = (f"(사적연금 분리과세 {sep_private} + 그 외 소득 종합과세 "
                 f"{format_manwon(sep_other)})" if sep_other else "")

    lines = [
        f"{indent}사적연금 연 수령액이 1,500만원을 초과해 분리과세와 종합과세 "
        f"중 하나를 선택해야 합니다.",
        f"{indent}분리과세를 선택하면 세액이 {sep_total}{(' ' + sep_detail) if sep_detail else ''}이고, "
        f"종합과세를 선택하면 과세표준 {base}(연금소득공제 {deduction} 반영)에 "
        f"대해 세액이 {comp_total}입니다.",
        f"{indent}세액만 보면 {cheaper} 쪽이 {diff} 더 낮습니다"
        + (f"({basis} 기준)." if basis else "."),
    ]
    if isinstance(result.get("⚠️"), str):
        lines.append(f"{indent}※ {result['⚠️']}")
    return "\n".join(lines)


def render_calc_result(result: Any, indent: str = "  ") -> str:
    """계산 결과 dict를 줄 단위 텍스트로. variants 구조를 지원한다."""
    if not isinstance(result, dict):
        return f"{indent}{result}"

    if "choice_required" in result and ("separate" in result or "note" in result):
        return _render_tax_choice(result, indent)

    if "variants" in result and isinstance(result["variants"], list):
        variants = result["variants"]
        rendered = [render_calc_result(v.get("result"), indent)
                    for v in variants]
        # ⚠️ 조건별 결과가 전부 같으면 한 번만 찍는다. 같은 내용을 조건 딱지만
        #    바꿔 두 번 늘어놓으면 "소득 구간에 따라 한도가 다르다"는 **틀린
        #    인상**을 준다 — 실제로 세액공제 한도(600/900)는 소득과 무관하다.
        #    갈리는 건 공제율뿐이고, 납입액을 모르면 공제율은 쓰이지 않는다.
        #    다만 "조건을 따져봤다"는 사실까지 버리면 안 되므로 한 줄로 남긴다.
        if len(rendered) > 1 and len(set(rendered)) == 1:
            labels = " · ".join(str(v.get("label", "조건")) for v in variants)
            return f"{indent}※ 아래 값은 {labels} 모두 동일합니다\n" + rendered[0]
        blocks = []
        for v, body in zip(variants, rendered):
            blocks.append(f"{indent}· {v.get('label', '조건')}:")
            blocks.append(render_calc_result(v.get("result"), indent + "    "))
        return "\n".join(blocks)

    lines = []
    for k, v in result.items():
        if k in _SKIP_KEYS:
            continue
        if k in _SKIP_IF_FALSE and v is False:
            continue
        if isinstance(v, dict):
            lines.append(f"{indent}{label_of(k)}:")
            lines.append(render_calc_result(v, indent + "  "))
            continue
        lines.append(f"{indent}{label_of(k)} = {format_value(k, v)}")

    # note/⚠️ 는 조건이나 한계를 담고 있어 버리면 안 된다 — 뒤에 따로 붙인다
    for k in ("note", "⚠️"):
        if isinstance(result.get(k), str):
            lines.append(f"{indent}※ {result[k]}")
    if isinstance(result.get("기준"), str):
        lines.append(f"{indent}※ 기준: {result['기준']}")
    return "\n".join(lines)


def calc_sources(result: Any) -> list[str]:
    """계산 결과에 박힌 근거 문서 ID를 회수."""
    found: list[str] = []
    if isinstance(result, dict):
        if "variants" in result and isinstance(result["variants"], list):
            for v in result["variants"]:
                found.extend(calc_sources(v.get("result")))
            return found
        src = result.get("source") or result.get("rate_source")
        if isinstance(src, str) and src and not src.startswith("default"):
            found.append(src)
        for v in result.values():
            if isinstance(v, dict):
                found.extend(calc_sources(v))
    return found
