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
}

# ── 단위 표기 ──────────────────────────────────────────────────
# ⚠️ 휴리스틱만으로 단위를 정하면 사고가 난다. 실제로 "적용 분모 = 10만원"처럼
#    개수를 금액으로 찍는 버그가 있었다. 아는 키는 명시적으로 못 박는다.
_UNIT_MANWON = {
    "limit", "A_tax_credit", "T_withholding", "P_private_excess", "difference",
    "C_np_copay", "P_np_monthly", "P_np_annual",
    "근속연수공제", "환산급여", "환산급여공제", "퇴직소득_과세표준",
    "환산산출세액", "산출세액", "합계", "과세표준", "연금소득공제",
    "사적연금_분리과세", "그외_종합과세",
}
_UNIT_RATE = {
    "r_withholding", "reduction_rate", "applied_rate_of_original_tax",
    "national_rate", "rate_with_local_tax", "r_tax_credit", "r_np_premium",
    "r_irr", "separate_tax_rate_used",
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
              "doc_id", "markers", "is_legacy_suspect", "reason", "params", "label"}


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


def render_calc_result(result: Any, indent: str = "  ") -> str:
    """계산 결과 dict를 줄 단위 텍스트로. variants 구조를 지원한다."""
    if not isinstance(result, dict):
        return f"{indent}{result}"

    if "variants" in result and isinstance(result["variants"], list):
        blocks = []
        for v in result["variants"]:
            blocks.append(f"{indent}· {v.get('label', '조건')}:")
            blocks.append(render_calc_result(v.get("result"), indent + "    "))
        return "\n".join(blocks)

    lines = []
    for k, v in result.items():
        if k in _SKIP_KEYS:
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
