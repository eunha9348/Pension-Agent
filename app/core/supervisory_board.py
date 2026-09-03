"""
감독 계층 (Supervisory Board)
==============================
기업의 법무팀 + 사외이사에 해당하는 계층.
답변을 '생성'하는 것이 아니라 '감독'한다.

기존 검증 계층과의 차이
----------------------
· 검증(verification) : 숫자가 근거에 있는가 — 기계적 대조, 참/거짓
· 감독(supervision)  : 이 답변을 내보내도 되는가 — 판단, 그리고 시정 지시

감독은 판정만 하지 않고 **무엇을 어떻게 고칠지 지시**한다.
이 지시 능력이 시스템에 실질적 의사결정을 부여한다.

4대 감사 영역
-------------
1. 준법 감사 (Compliance)  — 단정 표현, 근거 없는 추천, 고지 누락
2. 이상치 감사 (Anomaly)   — 계산 결과가 도메인 상식에 맞는가
3. 적합성 감사 (Fitness)   — 사용자 조건과 답변이 정합하는가
4. 부담 감사 (Burden)      — 역질문이 과도하지 않은가

판정
----
APPROVE   승인
REVISE    시정 후 재생성 (구체적 지시 동반)
DOWNGRADE 답변 등급 강등 (ANSWER→PARTIAL→ASK_BACK)
BLOCK     차단, fallback 템플릿으로 대체
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.law.schema import focus_snippets

log = logging.getLogger(__name__)


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    DOWNGRADE = "DOWNGRADE"
    BLOCK = "BLOCK"


_SEVERITY_ORDER = {Verdict.APPROVE: 0, Verdict.REVISE: 1,
                   Verdict.DOWNGRADE: 2, Verdict.BLOCK: 3}


@dataclass
class Finding:
    """감사 지적사항."""
    auditor: str
    code: str
    severity: Verdict
    detail: str
    directive: str = ""          # 시정 지시 — Supervisor 재생성 시 프롬프트에 주입

    def __str__(self):
        s = f"[{self.auditor}/{self.code}] {self.detail}"
        return s + (f" → {self.directive}" if self.directive else "")


# ════════════════════════════════════════════════════════════════
# 1. 준법 감사 — 법무팀 역할
# ════════════════════════════════════════════════════════════════

# 금융소비자보호법상 '단정적 판단 제공'에 해당할 소지가 있는 표현
_ASSERTIVE_PATTERNS = [
    (re.compile(r'가장\s*(유리|좋|낫|적합|우수)'), "최상급 단정"),
    (re.compile(r'(반드시|무조건|틀림없이|확실히)\s*(이득|유리|수익)'), "확실성 단정"),
    (re.compile(r'(추천|권장)(합니다|드립니다|해\s*드립니다)'), "직접 추천"),
    (re.compile(r'(하세요|하시면\s*됩니다)\s*$', re.M), "행위 지시"),
    (re.compile(r'수익률(이|은)?\s*\d+(\.\d+)?%\s*(예상|전망|기대)'), "수익 예측 단정"),
    (re.compile(r'손실\s*(없|않)'), "손실 부인"),
]

# 조건부 서술로 인정하는 표현 (이것이 있으면 단정 완화로 본다)
_CONDITIONAL_MARKERS = [
    "경우에는", "조건이라면", "이라면", "에 따라 다릅니다", "확인이 필요",
    "상황에 따라", "가정", "달라질 수 있", "일 수 있",
]

# 개인정보로 보이는 패턴 (답변에 그대로 반복 노출되면 지적)
_PII_PATTERNS = [
    (re.compile(r'\d{6}\s*[-–]\s*\d{7}'), "주민등록번호 형식"),
    (re.compile(r'\d{2,3}-\d{3,4}-\d{4}'), "전화번호 형식"),
    (re.compile(r'\d{3,6}-\d{2,6}-\d{4,8}'), "계좌번호 형식"),
]


def audit_compliance(answer: str, citations: list = (),
                      has_calculation: bool = False) -> list[Finding]:
    """준법 감사. 단정 표현·근거 없는 추천·고지 누락·개인정보를 점검."""
    findings: list[Finding] = []
    has_conditional = any(m in answer for m in _CONDITIONAL_MARKERS)

    for pat, label in _ASSERTIVE_PATTERNS:
        if pat.search(answer):
            # 조건부 서술이 함께 있으면 경미하게, 없으면 시정 대상
            sev = Verdict.REVISE if not has_conditional else Verdict.APPROVE
            if sev == Verdict.REVISE:
                findings.append(Finding(
                    "준법", "ASSERTIVE", Verdict.REVISE,
                    f"단정적 표현 감지 ({label})",
                    "해당 문장을 '~한 조건이라면 ~입니다' 형태의 조건부 서술로 바꾸고, "
                    "확인이 필요한 전제를 함께 제시할 것",
                ))
                break

    for pat, label in _PII_PATTERNS:
        if pat.search(answer):
            findings.append(Finding(
                "준법", "PII", Verdict.BLOCK,
                f"답변에 개인정보 형식 문자열 포함 ({label})",
                "해당 문자열을 제거하고 개인정보는 답변에 반복하지 않을 것",
            ))

    if has_calculation and not citations:
        findings.append(Finding(
            "준법", "NO_BASIS", Verdict.DOWNGRADE,
            "계산 결과를 제시하면서 근거 문서가 없음",
            "근거를 확보하지 못했다면 수치를 제시하지 말고 확인 필요 사항으로 전환할 것",
        ))

    return findings


# ════════════════════════════════════════════════════════════════
# 2. 이상치 감사 — 계산이 도메인 상식에 맞는가
# ════════════════════════════════════════════════════════════════

# 도메인 상한/하한. 금액 단위는 만원.
_DOMAIN_BOUNDS = {
    "세율":       (0.0, 0.50),      # 50% 초과 세율은 이상
    "금액":       (0.0, 1_000_000),  # 100억(만원 단위) 초과는 이상 의심
    "연차":       (0, 60),
    "연령":       (0, 120),
    "근속연수":   (0, 60),
}

_RATE_KEYS = ("rate", "세율", "r_", "공제율", "감면")
# ⚠️ "공제"·"급여"·"과세표준"이 없으면 '근속연수공제=5500'(만원)이 키에 '근속'이
#    들어있다는 이유로 근속연수(0~60)로 분류돼 BLOCK된다. 실제로 doc52 원문
#    예시와 일치하는 정답(5,500만원)이 감독에서 차단되는 오탐이 있었다.
#    금액 판정을 연수 판정보다 먼저 하고, 금액 어휘를 넓힌다.
_AMOUNT_KEYS = ("한도", "세액", "금액", "공제", "급여", "과세표준", "산출",
                "limit", "tax", "T_", "A_", "P_", "C_")


# 수치이긴 하나 범위 감사 대상이 아닌 키 (연도·식별자 등)
_NON_METRIC_KEYS = {"tax_year", "year", "doc_id", "source", "rate_source"}


def _classify_key(key: str) -> Optional[str]:
    k = key.lower()
    if k in _NON_METRIC_KEYS:
        # 과세연도(2024)를 금액이나 연차로 분류하면 엉뚱한 이상치가 잡힌다
        return None
    if any(t in key or t in k for t in _RATE_KEYS):
        return "세율"
    if any(t in key or t in k for t in _AMOUNT_KEYS):
        return "금액"
    # 영문 키도 분류한다 — 계산함수가 pension_year 같은 이름을 쓰므로
    # 한글 키만 보면 연차 이상치가 감사를 그대로 통과한다.
    # ⚠️ 단, tax_year(과세연도)는 연차가 아니다. 이름을 명시적으로 나열한다.
    if "연차" in key or k in ("pension_year", "actual_receipt_year"):
        return "연차"
    if "연령" in key or "age" in k:
        return "연령"
    if "근속" in key or k in ("service_years",):
        return "근속연수"
    return None


def _flatten_variants(results: Optional[list]) -> list[dict]:
    """variants 구조를 펼쳐 실제 계산 dict만 돌려준다.

    ━━ 왜 필요한가 (2026-09-03 실측) ━━
    소득을 모르면 calc_params가 세율 구간별로 결과를 나눠 담는다
    (`{"variants": [{"label": ..., "result": {...}}, ...]}`). 그런데
    audit_anomaly는 최상위 키만 훑어서, **이 구조에서는 findings가
    0건**이 됐다. 신규 판정뿐 아니라 기존 LIMIT_EXCEEDED·CREDIT_EXCEEDS·
    LIMIT_RATIO까지 통째로 눈이 멀었다.

    실측 대조: 같은 내용을 평면 구조로 주면 2건 발화, variants로 주면 0건.
    소득을 밝히지 않는 질의는 흔하므로 파급이 크다.

    `numeric_verifier._presence_targets`는 이미 variants를 재귀로 훑는다 —
    같은 구조를 보는 두 계층이 서로 다른 기준을 쓰고 있었던 것이다.
    """
    out: list[dict] = []
    for r in results or ():
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("variants"), list):
            for v in r["variants"]:
                inner = v.get("result") if isinstance(v, dict) else None
                if isinstance(inner, dict):
                    out.append(inner)
            continue
        out.append(r)
    return out


def audit_anomaly(calc_results: list[dict],
                   user_conditions: Optional[dict] = None) -> list[Finding]:
    """계산 이상치 감사.

    수치 대조 검증은 '근거에 있는 숫자인가'만 본다.
    이 감사는 '그 숫자가 말이 되는가'를 본다 — 둘은 다른 문제다.
    """
    findings: list[Finding] = []
    user_conditions = user_conditions or {}

    # ⚠️ variants 를 펼쳐서 본다 — 안 펼치면 이 감사 전체가 불발한다
    #    (_flatten_variants 주석 참조).
    for r in _flatten_variants(calc_results):
        if not isinstance(r, dict):
            continue

        for key, val in r.items():
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue

            kind = _classify_key(key)
            if kind is None:
                continue
            lo, hi = _DOMAIN_BOUNDS[kind]

            if val < lo:
                findings.append(Finding(
                    "이상치", "NEGATIVE", Verdict.BLOCK,
                    f"{key}={val} — {kind}이(가) 음수",
                    "계산 입력을 재확인하고, 확정할 수 없으면 수치를 제시하지 말 것",
                ))
            elif val > hi:
                # 단위 혼동(원 ↔ 만원, 1만배) 가능성을 우선 의심
                unit_suspect = kind == "금액" and val > hi * 10
                findings.append(Finding(
                    "이상치", "UNIT_SUSPECT" if unit_suspect else "OUT_OF_RANGE",
                    Verdict.BLOCK,
                    f"{key}={val} — {kind} 통상 범위({lo}~{hi}) 초과"
                    + (" · 원/만원 단위 혼동 의심" if unit_suspect else ""),
                    "단위 체계를 확인하고, 해소되지 않으면 해당 수치를 답변에서 제외할 것",
                ))

        # ── 항목 간 정합성 ──
        credit = r.get("A_tax_credit")
        if credit is not None and user_conditions.get("annual_contribution"):
            if credit > user_conditions["annual_contribution"]:
                findings.append(Finding(
                    "이상치", "CREDIT_EXCEEDS", Verdict.BLOCK,
                    f"세액공제액({credit})이 납입액을 초과",
                    "세액공제 계산 입력을 재확인할 것",
                ))

        if r.get("IsLimitExceeded") is True:
            findings.append(Finding(
                "이상치", "LIMIT_EXCEEDED", Verdict.REVISE,
                "납입한도 초과 상태가 계산에 반영됨",
                "한도 초과분은 세액공제 대상이 아니라는 점을 답변에 명시할 것",
            ))
        # ⚠️ 위와 별개 판정이다 — IsLimitExceeded는 연간 총납입한도(1,800만원)
        #    만 본다. "연금저축에 900만원 넣으면 다 공제되나요?"는 900이
        #    1,800을 안 넘으니 위 REVISE가 안 뜨지만, 실제로는 연금저축
        #    단독 한도(600만원)를 넘겼다. 그 사실을 알리는 신호가 없으면
        #    "다 공제됩니다"라는 오답이 그대로 나간다(2026-09-03 실측 E-03).
        if r.get("IsPensionSavingLimitExceeded") is True:
            findings.append(Finding(
                "이상치", "PENSION_SAVING_LIMIT_EXCEEDED", Verdict.REVISE,
                "연금저축 단독 세액공제 한도(600만원) 초과 상태가 계산에 반영됨",
                "연금저축 단독으로는 600만원까지만 세액공제되고 초과분은 "
                "대상이 아니라는 점을 답변에 명시할 것",
            ))
        if r.get("IsCombinedLimitExceeded") is True:
            findings.append(Finding(
                "이상치", "COMBINED_LIMIT_EXCEEDED", Verdict.REVISE,
                "연금저축+IRP 합산 세액공제 한도(900만원) 초과 상태가 계산에 반영됨",
                "연금저축과 IRP를 합쳐도 900만원까지만 세액공제되고 초과분은 "
                "대상이 아니라는 점을 답변에 명시할 것",
            ))

        limit = r.get("limit")
        if limit is not None and isinstance(limit, (int, float)):
            base = user_conditions.get("account_value")
            if base and limit > base * 1.2 + 1e-6 and not r.get("unlimited"):
                findings.append(Finding(
                    "이상치", "LIMIT_RATIO", Verdict.REVISE,
                    f"연금수령한도({limit})가 평가액의 120%를 초과",
                    "연차 입력값을 재확인할 것",
                ))

    # ⚠️ variants를 펼치면 세율 구간마다 같은 지적이 반복된다("소득 구간별로
    #    나눠 계산했다"는 사정은 사용자 잘못이 아니고, 같은 말을 두 번 하면
    #    시정 지시가 지저분해진다). 내용이 똑같은 것만 합친다 — 값이 달라
    #    detail이 다른 지적은 각각 살려 둔다.
    deduped: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for f in findings:
        key = (f.code, f.detail)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped


# ════════════════════════════════════════════════════════════════
# 3. 적합성 감사 — 사용자에게 맞는 정보인가
# ════════════════════════════════════════════════════════════════

# 계좌유형별로 답변에 등장하면 안 되는 표현
_ACCOUNT_MISMATCH = {
    "연금저축": ["퇴직연금 전용", "IRP 전용", "근로자퇴직급여보장법에 따라 인출"],
    "퇴직연금": ["사유와 무관하게 언제든 인출", "연금저축 전용"],
}


def _required_terms(check: dict, limit: int = 3) -> list[str]:
    """이 함정이 '해소됐다'고 판정받으려면 답변에 있어야 할 표현.

    `unaddressed_traps`가 쓰는 판정 기준(verify_any)을 그대로 돌려준다.
    재생성 지시에 실어 **합격 조건을 명시**하기 위한 것이다 — 기준을
    바꾸는 것이 아니라 밝히는 것이므로 감사의 엄격성은 그대로다.
    """
    return [t for t in (check.get("verify_any") or []) if t][:limit]


def audit_fitness(answer: str,
                   user_conditions: Optional[dict] = None,
                   mentioned_products: Optional[list[dict]] = None,
                   trap_ids: Optional[list[str]] = None,
                   trap_checks: Optional[list[dict]] = None) -> list[Finding]:
    """적합성 감사. 사용자 조건과 답변 내용이 어긋나지 않는지 본다.

    trap_checks : 규칙별 검증 정보([{id, severity, verify_any, ...}]).
                  주어지면 함정 해소 여부를 **규칙 단위로** 판정한다.
                  없으면 trap_ids만으로 예전 방식(전체 일괄)으로 돌아간다.
    """
    findings: list[Finding] = []
    uc = user_conditions or {}

    acct = uc.get("account_type")
    if acct:
        for bad in _ACCOUNT_MISMATCH.get(acct, []):
            if bad in answer:
                findings.append(Finding(
                    "적합성", "ACCOUNT_MISMATCH", Verdict.REVISE,
                    f"{acct} 이용자에게 부적합한 서술: '{bad}'",
                    f"{acct} 계좌 기준으로 서술을 교정할 것",
                ))

    # 가입 불가 상품이 답변에 등장했는가
    for p in (mentioned_products or []):
        if p.get("eligible") is False and p.get("name", "") in answer:
            findings.append(Finding(
                "적합성", "INELIGIBLE_PRODUCT", Verdict.REVISE,
                f"가입 자격이 없는 상품이 답변에 포함됨: {p.get('name')}",
                "해당 상품을 제외하거나, 가입 요건 때문에 대상이 아님을 명시할 것",
            ))

    # 연령 조건과 연금수령 서술의 정합
    age = uc.get("age")
    if isinstance(age, (int, float)) and age < 55 and "연금수령" in answer:
        if not any(k in answer for k in ("55세", "만 55세", "이후")):
            findings.append(Finding(
                "적합성", "AGE_CONTEXT", Verdict.REVISE,
                "55세 미만 이용자에게 연금수령을 전제로 서술",
                "연금수령 개시 요건(만 55세 등)을 함께 명시할 것",
            ))

    # 조건 미확인 상태에서 특정 상품을 지목했는가
    #
    # ⚠️ 2026-08-29 — **강등하지 않는다.** 예전에는 DOWNGRADE였는데,
    #    그것이 "자격을 모르니 일단 추천하고 알려주시면 좁혀 드린다"는
    #    방향과 정면으로 충돌했다. 계좌 유형을 모른다는 이유로 답변 등급을
    #    깎으면, 짧은 질의(대부분이 그렇다)는 영영 상품 얘기를 못 한다.
    #
    #    자격이 **확정적으로 불가한** 상품은 파이프라인의 합류 barrier가
    #    이미 걷어냈다(_eligibility_barrier). 여기까지 온 것은 '모르는'
    #    상품이므로, 조건부로 제시하되 확인을 함께 요청하면 충분하다.
    #    그래서 판정을 APPROVE로 낮추고 지시만 남긴다.
    if not acct and mentioned_products:
        findings.append(Finding(
            "적합성", "UNVERIFIED_RECOMMENDATION", Verdict.APPROVE,
            "계좌 유형이 확인되지 않은 상태에서 특정 상품을 제시 "
            "(추천은 유지하고 확인을 함께 요청한다)",
            "유형별로 갈리는 부분을 조건부로 서술하고, 계좌 유형 확인을 "
            "함께 요청할 것",
        ))

    # ── 감지된 함정이 답변에서 실제로 다뤄졌는가 ──────────────
    #
    # ⚠️ 예전에는 "다릅니다·구분·별개·주의·아닙니다" 중 아무거나 하나만
    #    있으면 감지된 함정 **전부**가 해소된 것으로 봤다. 그래서
    #    "유동성 관리에는 주의가 필요합니다" 한 문장이 전혀 무관한
    #    D2(운용사 간 위험등급 비교)까지 통과시켰다(Q-002 실패).
    #    지금은 규칙마다 자기 핵심어로 따로 판정한다.
    if trap_checks:
        from app.core.trap_rules import unaddressed_traps

        missed = unaddressed_traps(answer, trap_checks)
        if missed:
            crit = [m["id"] for m in missed if m.get("severity") == "critical"]
            high = [m["id"] for m in missed if m.get("severity") == "high"]
            rest = [m["id"] for m in missed
                    if m.get("severity") not in ("critical", "high")]
            # 무엇을 어떻게 바로잡아야 하는지까지 준다 — 지시가 구체적이어야
            # 재생성이 성공한다. 예전의 뭉뚱그린 지시는 재생성도 실패했다.
            #
            # ⚠️ 해소 판정 기준(verify_any)을 지시에 함께 싣는다 (2026-09-02).
            #    실측에서 재생성이 계속 기각됐다 — 시정 지시는 correction 문장
            #    뿐이라, 모델이 취지는 반영하면서 다른 말로 바꿔 쓰면
            #    `unaddressed_traps`가 여전히 미해소로 판정했다. 즉 **합격
            #    조건을 알려주지 않은 채 다시 쓰라고 시킨 것**이다. 그 결과
            #    L5' 재생성도 Sub-Agent 구제도 같은 자리에서 떨어졌고
            #    (L6_재생성_기각 → SubAgent_구제_기각), 답변은 축퇴로 갔다.
            #    검사 기준을 밝히는 것은 감사를 무르는 것이 아니다 — 판정
            #    로직은 그대로고, 무엇을 써야 통과하는지를 알려줄 뿐이다.
            directive = " / ".join(
                f"[{m['id']}] {m.get('correction') or m.get('title', '')}"
                + (f" (반드시 '{'‧'.join(_required_terms(m))}' 중 한 표현을 "
                   f"답변 본문에 그대로 쓸 것)" if _required_terms(m) else "")
                for m in missed[:3])
            # ⚠️ high도 REVISE다. 예전에는 critical만 REVISE고 high는
            #    DOWNGRADE였는데, DOWNGRADE는 **재생성을 타지 않는다**
            #    (pipeline은 REVISE에서만 L5'로 되돌린다). 그래서 감사가
            #    "[E3] 개인계좌로 직접 수령 가능 / [E4] 60일 내면 환급 가능"
            #    이라는 구체적 시정 지시를 만들어 놓고도 그것을 버린 채
            #    등급 라벨만 바꿔 원본을 그대로 내보냈다. 실측에서 확인됐다
            #    (2026-09-01) — 사용자에게는 E3·E4가 통째로 빠진 답변이
            #    나갔고, 감사가 문제를 정확히 짚었다는 사실은 어디에도
            #    드러나지 않았다. CLAUDE.md의 "감사가 있다는 주장은 결과가
            #    반영될 때만 참이다"를 정면으로 위반한 상태였다.
            #
            #    high는 '틀리면 세금 계산이 달라지는' 등급이다. 지시를
            #    만들어 놓고 쓰지 않을 이유가 없다. medium은 뉘앙스라
            #    DOWNGRADE로 남긴다 — 재생성 비용에 값하지 않는다.
            #
            #    비용 실측(298건, mock 파이프라인): 함정 감지 149건 중
            #    high만 미해소는 2건(L14·O10). 즉 재생성이 새로 붙는 질의는
            #    전체의 0.67%다. 지연에 실질적인 영향이 없다.
            severe = crit + high
            findings.append(Finding(
                "적합성", "TRAP_UNADDRESSED",
                Verdict.REVISE if severe else Verdict.DOWNGRADE,
                f"감지된 함정 중 답변에서 다뤄지지 않은 것: "
                f"{crit + high + rest} "
                f"(critical {len(crit)}건 · high {len(high)}건)",
                f"다음을 답변에 명시적으로 반영할 것 — {directive}",
            ))
    elif trap_ids:
        # 규칙별 정보가 없을 때의 예전 경로 (하위 호환)
        correction_markers = ("다릅니다", "구분", "별개", "주의", "아닙니다", "해당하지 않")
        if not any(m in answer for m in correction_markers):
            findings.append(Finding(
                "적합성", "TRAP_UNADDRESSED", Verdict.REVISE,
                f"함정 규칙 {trap_ids} 감지됐으나 답변에 교정 취지가 보이지 않음",
                "감지된 혼동 지점을 답변에서 명시적으로 바로잡을 것",
            ))

    return findings


# ════════════════════════════════════════════════════════════════
# 4. 부담 감사 — 사용자에게 과도한 요구를 하는가
# ════════════════════════════════════════════════════════════════

MAX_ASK_BACK = 2          # 단일 턴 평가에서 사용자가 감당 가능한 확인 항목 수
MAX_ANSWER_CHARS = 1800


def audit_burden(answer: str,
                  ask_back_items: Optional[list[str]] = None,
                  answerability: str = "ANSWER",
                  partial_answer_possible: bool = False) -> tuple[list[Finding], list[str]]:
    """부담 감사.

    역질문은 정확성을 높이지만 과하면 서비스가 성립하지 않는다.
    "확인이 필요하다"를 다섯 개 나열하는 답변은 사실상 답변이 아니다.

    반환: (지적사항, 추려낸 역질문 목록)
    """
    findings: list[Finding] = []
    items = list(ask_back_items or [])

    if len(items) > MAX_ASK_BACK:
        findings.append(Finding(
            "부담", "TOO_MANY_QUESTIONS", Verdict.REVISE,
            f"확인 요청 항목이 {len(items)}건 — 사용자 부담 과다",
            f"가장 결정적인 {MAX_ASK_BACK}건만 남기고, 나머지는 조건부 서술로 흡수할 것",
        ))
        items = items[:MAX_ASK_BACK]

    if answerability == "ASK_BACK" and partial_answer_possible:
        findings.append(Finding(
            "부담", "AVOIDABLE_ASKBACK", Verdict.REVISE,
            "부분 답변이 가능한데 역질문만 하고 있음",
            "확인 가능한 범위는 먼저 답하고, 남은 조건만 확인 요청할 것",
        ))

    if len(answer) > MAX_ANSWER_CHARS:
        findings.append(Finding(
            "부담", "TOO_LONG", Verdict.REVISE,
            f"답변 길이 {len(answer)}자 — 핵심 파악이 어려움",
            "결론을 앞에 두고 부차적 설명을 줄일 것",
        ))

    if not answer.strip():
        findings.append(Finding(
            "부담", "EMPTY", Verdict.BLOCK,
            "답변이 비어 있음", "fallback 템플릿으로 대체할 것",
        ))

    return findings, items


# ════════════════════════════════════════════════════════════════
# 5. 감독 이사회 — 종합 판정 및 시정 지시
# ════════════════════════════════════════════════════════════════

@dataclass
class SupervisionResult:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    revised_ask_back: list[str] = field(default_factory=list)
    downgraded_answerability: Optional[str] = None

    def as_trace(self) -> str:
        if self.verdict == Verdict.APPROVE and not self.findings:
            return "감독 심사 승인 — 지적사항 없음"
        lines = [f"감독 심사 {self.verdict.value} — 지적 {len(self.findings)}건"]
        lines += [f"  · {f}" for f in self.findings]
        if self.downgraded_answerability:
            lines.append(f"  · 답변 등급 강등 → {self.downgraded_answerability}")
        return "\n".join(lines)


_DOWNGRADE_CHAIN = {"ANSWER": "PARTIAL", "PARTIAL": "ASK_BACK", "ASK_BACK": "ASK_BACK"}


# ════════════════════════════════════════════════════════════════
# 5. 정합성 감사 — 답변이 스스로 모순되지 않는가 (2026-08-29 신설)
# ════════════════════════════════════════════════════════════════
#
# 감독을 '로직 정합성 점검 위주'로 재조정하면서 추가했다. 의미 감사(LLM)에
# 맡기던 것 중 **규칙으로 확정할 수 있는 모순**을 결정론적으로 먼저 잡는다.
# LLM에 맡기면 실행마다 판정이 달라지는데, 아래 셋은 그럴 이유가 없다.

# 서로 함께 있으면 모순인 표현 쌍.
#
# ⚠️ 부분문자열로 비교하면 안 된다. "불가능합니다"는 "가능합니다"를 **포함**하므로
#    단순 `in` 검사는 부정문 하나만 있어도 스스로 발화한다. 실측에서 정확히 이
#    형태로 오탐이 났다(E14 "명예퇴직금도 퇴직소득으로 보나요?" — 답변에
#    "불가능합니다"만 있는데 모순으로 잡혔다). 그래서 경계를 준 정규식으로 쓴다.
#
# '한도가 없' ↔ '한도는' 쌍은 **의도적으로 뺐다.** "한도는 없습니다"가 그 자체로
# 한도 없음을 뜻하는 정상 문장이라, 문자열만으로는 모순인지 아닌지 결정할 수
# 없다. 결정 불가능한 것을 결정론적 규칙에 넣으면 그건 규칙이 아니라 추측이다.
_CONTRADICTIONS: tuple[tuple[re.Pattern, re.Pattern, str], ...] = (
    (re.compile(r'해당하지\s*않습니다'), re.compile(r'해당합니다'), "해당 여부"),
    (re.compile(r'(?<!불)가능합니다'), re.compile(r'불가능합니다'), "가능 여부"),
    (re.compile(r'포함되지\s*않습니다'), re.compile(r'포함됩니다'), "포함 여부"),
)

# 문장을 가르는 기준 — 모순 판정은 **한 문장 안에서만** 한다(아래 설명 참조)
_SENTENCE_SPLIT = re.compile(r'[.!?\n·]')

# ── "모른다고 해 놓고 단정한다"는 왜 여기에 없는가 (2026-08-29) ──
#
# 전제–결론 정합("확인되지 않았다"고 적어 놓고 결론은 하나로 단정)은 분명히
# 잡아야 할 결함이다. 그런데 **결정론적으로는 잡을 수 없다.** 두 번 시도해
# 실측으로 확인했다:
#   1차 — 유보 표지 + _ASSERTIVE_PATTERNS 조합: audit_compliance가 이미 보는
#         패턴을 완화 없이 다시 보는 것이라 정상 답변을 이중으로 깎았다.
#   2차 — 유보 표지 + 조건부 표현 부재: 298건 중 26건(8.7%)이 오탐이었다.
#         "납입액과 소득 구간에 따라 달라집니다"는 실질적으로 경우를 나눈
#         문장인데 표지 목록에 없어서 걸렸다. 목록을 늘리면 다음 표현에서
#         또 걸린다 — 표현의 가짓수가 유한하지 않다.
#
# 여기서 오탐은 미탐보다 **엄격히 나쁘다.** 권한 계층상 LLM 감사는 심각도를
# 올릴 수만 있고 결정론적 판정을 완화하지 못하므로(merge_supervision),
# 결정론적 오탐은 되돌릴 방법이 없는 강제 강등이 된다. 그래서 결정론 계층에는
# **확실히 옳은 규칙만** 둔다.
#
# 이 판단은 버리는 게 아니라 제자리로 보낸 것이다. LLM_AUDIT_SYSTEM_PROMPT의
# 2번 항목("전제–결론 정합")이 정확히 이걸 본다. 의미 판단은 의미 감사가 한다.


def audit_coherence(answer: str,
                    ask_back_items: Optional[list[str]] = None
                    ) -> list[Finding]:
    """정합성 감사. 답변 내부의 모순을 결정론적으로 찾는다.

    ⚠️ 여기서 보는 것은 **말이 되는가**이지 문장이 매끄러운가가 아니다.
       문체·표현 취향은 판정 대상이 아니다 — L5'가 자연스러운 문장으로
       쓰도록 설계돼 있으므로, 형식으로 지적하면 정상 동작을 깎게 된다.

    ⚠️ 상반된 서술이 **답변 전체**에 흩어져 있는 것은 모순이 아니다.
       "DC 계좌면 가능하고, 급여계좌면 불가능합니다"는 우리가 설계로
       요구한 조건별 결론(CLAUDE.md — 단정적 추천 금지)이지 결함이 아니다.
       그래서 같은 문장 안에서, 조건 표현 없이 맞붙은 경우만 잡는다.
    """
    findings: list[Finding] = []
    a = answer or ""

    # ── 내부 모순: 한 문장 안에서 조건 구분 없이 상반된 서술이 맞붙었다 ──
    for sentence in _SENTENCE_SPLIT.split(a):
        if any(m in sentence for m in _CONDITIONAL_MARKERS):
            continue                       # 경우를 나눈 문장은 모순이 아니다
        hit = next(((t) for l, r, t in _CONTRADICTIONS
                    if l.search(sentence) and r.search(sentence)), None)
        if hit:
            findings.append(Finding(
                "정합성", "SELF_CONTRADICTION", Verdict.REVISE,
                f"한 문장 안에서 {hit}에 대해 상반된 서술이 함께 있음",
                f"{hit}를 하나로 정리하고, 경우가 갈리면 조건을 명시할 것"))
            break

    # 전제–결론 정합은 여기서 보지 않는다 — 위 주석 참조(의미 감사 소관).
    return findings


def supervise(answer: str,
              calc_results: Optional[list[dict]] = None,
              citations: Optional[list] = None,
              user_conditions: Optional[dict] = None,
              mentioned_products: Optional[list[dict]] = None,
              ask_back_items: Optional[list[str]] = None,
              answerability: str = "ANSWER",
              trap_ids: Optional[list[str]] = None,
              trap_checks: Optional[list[dict]] = None,
              partial_answer_possible: bool = False) -> SupervisionResult:
    """5대 감사를 실행하고 종합 판정 + 시정 지시를 산출한다.

    준법 · 이상치 · 적합성 · 부담 · **정합성**.

    이 함수는 검사만 하지 않는다. 무엇을 고칠지 지시하고,
    필요하면 답변 등급을 스스로 강등시킨다 — 이것이 감독이다.
    """
    calc_results = calc_results or []
    findings: list[Finding] = []

    findings += audit_compliance(answer, citations or [], has_calculation=bool(calc_results))
    findings += audit_anomaly(calc_results, user_conditions)
    findings += audit_fitness(answer, user_conditions, mentioned_products,
                              trap_ids, trap_checks)
    findings += audit_coherence(answer, ask_back_items)
    burden_findings, revised_items = audit_burden(
        answer, ask_back_items, answerability, partial_answer_possible)
    findings += burden_findings

    if not findings:
        return SupervisionResult(Verdict.APPROVE, revised_ask_back=revised_items)

    worst = max(findings, key=lambda f: _SEVERITY_ORDER[f.severity]).severity
    directives = [f.directive for f in findings if f.directive]

    downgraded = None
    if worst == Verdict.DOWNGRADE:
        downgraded = _DOWNGRADE_CHAIN.get(answerability, answerability)
    elif worst == Verdict.REVISE and answerability == "ANSWER":
        # ⚠️ REVISE는 "고쳐서 내라"는 뜻이지 "이대로 확신 있게 내라"가 아니다.
        #    재생성이 성공하면 판정이 갱신되면서 이 강등도 함께 사라진다.
        #    실패하면 강등된 등급이 남아, 확인 조건을 함께 제시하게 된다.
        #    (CLAUDE.md — "모르면 되묻는다", 최고 가중치 지표)
        downgraded = _DOWNGRADE_CHAIN.get(answerability, answerability)

    return SupervisionResult(
        verdict=worst,
        findings=findings,
        directives=directives,
        revised_ask_back=revised_items,
        downgraded_answerability=downgraded,
    )


def build_remediation_prompt(result: SupervisionResult, original_answer: str) -> str:
    """REVISE 판정 시 Supervisor에게 전달할 재생성 지시문.

    감독 계층이 LLM에게 '무엇을 어떻게 고치라'고 지시하는 부분.

    ━━ 지시는 강제력이 있어야 한다 (2026-09-02 보강) ━━
    실측에서 재생성이 반복적으로 기각됐다. 원인은 지시가 약해서였다 —
    "다음 지적사항에 따라 수정하십시오"만으로는 모델이 **원본 문장을
    대부분 그대로 두고 표현만 다듬는다.** 그러면 미해소 함정 집합이
    그대로라 `_is_improvement`도 통과하지 못하고, 답변은 축퇴로 간다.

    그래서 세 가지를 못박는다:
      · 지적된 문장은 **삭제하거나 바로잡는다** (그대로 두면 안 된다)
      · 지시에 '그대로 쓸 것'이라고 표시된 표현은 **본문에 그대로** 넣는다
      · 지적사항을 반영했는지 **스스로 대조한 뒤** 출력한다
    """
    if result.verdict != Verdict.REVISE or not result.directives:
        return ""
    lines = [
        # ⚠️ 이 지시문에 마크다운(**굵게**)을 쓰지 말 것 — HCX가 그대로
        #    따라 써서 사용자 화면에 노출된 이력이 있다(2026-09-01).
        "아래 원본 답변은 내부 감사에서 반려됐습니다. 지적사항을 반영해 "
        "답변을 처음부터 다시 작성하십시오.",
        "",
        "[반드시 지킬 것]",
        "· 지적된 내용과 어긋나는 원본 문장은 그대로 두지 말고 삭제하거나 "
        "바로잡으십시오. 표현만 다듬는 수정은 반려됩니다.",
        "· 지시에 \"그대로 쓸 것\"이라고 표시된 표현은 답변 본문에 그 표현 "
        "그대로 포함시키십시오.",
        "· 새로운 수치를 만들지 말고, 기존 계산 결과와 근거 문서에 있는 "
        "값만 사용하십시오.",
        "· 출력 전에 아래 지적사항을 하나씩 되짚어, 각 항목이 답변에 "
        "반영됐는지 확인하십시오.",
        "",
        "[지적사항]",
    ]
    for i, d in enumerate(result.directives, 1):
        lines.append(f"{i}. {d}")
    lines += ["", "── 원본 답변 ──", original_answer]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 7. 계획 감사 — L1 산출물을 실행 전에 심사
# ════════════════════════════════════════════════════════════════
"""
L1이 산출한 실행 계획(슬롯 · 호출할 계산함수 · 계획 단계)은
현재 아무도 검사하지 않고 곧바로 실행된다.

L1이 잘못된 함수를 고르거나 슬롯을 빠뜨리면 이후 전 단계가 그 위에서 진행되고,
L6 감독은 이미 만들어진 답변만 본다 — 원인이 아니라 증상을 보는 셈이다.

계획 감사는 **실행 전에** 계획을 심사한다. LLM 호출 없이 결정론적으로 수행한다.
"""


def audit_plan(query_spec: dict,
               registry_keys: set[str] | list[str],
               grounding=None) -> tuple[list[Finding], dict]:
    """L1 실행 계획 감사. 반환: (지적사항, 교정된 query_spec)

    registry_keys : CALC_REGISTRY에 등록된 함수명 집합
    grounding     : GroundingResult. 있으면 도메인 커버리지와 대조한다.

    교정된 spec은 미등록 함수 호출을 제거한 안전한 버전이다.
    """
    findings: list[Finding] = []
    registry = set(registry_keys)
    spec = dict(query_spec or {})

    asked_for = spec.get("asked_for") or []
    planned = spec.get("planned_calls") or []
    plan_steps = spec.get("plan") or []

    # ── 요구사항이 아예 없는 경우 ──
    if not asked_for:
        findings.append(Finding(
            "계획감사", "NO_SLOTS", Verdict.DOWNGRADE,
            "요구사항 슬롯이 추출되지 않음 — 질문 의도가 구조화되지 않았음",
            "질의를 다시 분석하거나, 확인이 필요한 사항을 되물을 것",
        ))

    # ── 미등록 함수 호출 (화이트리스트 위반) ──
    safe_calls = []
    for call in planned:
        fn = (call or {}).get("function")
        if fn not in registry:
            findings.append(Finding(
                "계획감사", "UNKNOWN_FUNCTION", Verdict.REVISE,
                f"등록되지 않은 계산함수 호출 계획: '{fn}'",
                "해당 호출을 제거하고 매핑 테이블 기반 결정론적 선택으로 대체할 것",
            ))
            continue
        if not isinstance(call.get("args"), dict):
            findings.append(Finding(
                "계획감사", "BAD_ARGS", Verdict.REVISE,
                f"'{fn}' 호출 인자가 dict 형식이 아님",
                "인자 구조를 확인하고, 확정 불가 시 해당 슬롯을 확인 요청으로 전환할 것",
            ))
            continue
        safe_calls.append(call)
    spec["planned_calls"] = safe_calls

    # ── 계산 슬롯인데 호출 계획이 없는 경우 ──
    calc_slots = [s.get("id") for s in asked_for
                  if isinstance(s, dict) and s.get("type") == "calculation"]
    if calc_slots and not safe_calls:
        findings.append(Finding(
            "계획감사", "MISSING_CALC", Verdict.REVISE,
            f"계산이 필요한 슬롯({calc_slots})이 있으나 호출 계획이 없음",
            "해당 슬롯에 대응하는 계산함수를 선택하거나, 계산 불가 사유를 명시할 것",
        ))

    # ── 도메인 커버리지와 대조 ──
    if grounding is not None and not getattr(grounding, "domain_covered", True):
        if safe_calls:
            findings.append(Finding(
                "계획감사", "PLAN_WITHOUT_COVERAGE", Verdict.DOWNGRADE,
                "제공 자료에서 관련 영역을 찾지 못했는데 계산을 계획함",
                "근거 없는 계산을 수행하지 말고 한계를 고지할 것",
            ))

    # ── 계획 단계 과다 (타임아웃 리스크) ──
    if len(plan_steps) > 8:
        findings.append(Finding(
            "계획감사", "PLAN_TOO_LONG", Verdict.REVISE,
            f"계획 단계 {len(plan_steps)}건 — 단일 요청 시간 예산 초과 위험",
            "핵심 단계로 축약할 것",
        ))

    return findings, spec


def supervise_plan(query_spec: dict,
                   registry_keys: set[str] | list[str],
                   grounding=None) -> tuple[SupervisionResult, dict]:
    """계획 감사 실행 및 판정. 반환: (감독 결과, 교정된 spec)"""
    findings, safe_spec = audit_plan(query_spec, registry_keys, grounding)
    if not findings:
        return SupervisionResult(Verdict.APPROVE), safe_spec

    worst = max(findings, key=lambda f: _SEVERITY_ORDER[f.severity]).severity
    downgraded = None
    if worst == Verdict.DOWNGRADE:
        downgraded = "PARTIAL"

    return SupervisionResult(
        verdict=worst,
        findings=findings,
        directives=[f.directive for f in findings if f.directive],
        downgraded_answerability=downgraded,
    ), safe_spec


# ════════════════════════════════════════════════════════════════
# 8. 의미 감사 — HyperCLOVA X 참여 계층
# ════════════════════════════════════════════════════════════════
"""
결정론적 감사만으로는 잡을 수 없는 영역이 있다.

  · 숫자는 맞고 설명이 틀린 경우 (연차 2종 혼동 등) — 수치 대조를 통과한다
  · 답변 태도의 적절성 — 평가지표에 명시돼 있으나 규칙화가 불가능하다
  · 형식적으로는 답했으나 실제 의도를 빗나간 경우
  · 틀리지 않았으나 오해를 유발하는 서술

이 영역은 의미 판단이므로 HyperCLOVA X가 감사에 참여한다.

━━ 권한 계층 (핵심 안전장치) ━━
LLM 감사는 **심각도를 올릴 수만 있고 내릴 수 없다.**

  결정론적 BLOCK     → LLM이 무엇이라 해도 BLOCK 확정
  결정론적 REVISE    → LLM이 BLOCK을 주장하면 BLOCK, APPROVE를 주장해도 REVISE 유지
  결정론적 APPROVE   → LLM이 문제를 발견하면 상향, 아니면 APPROVE

이 단조성(monotonicity) 덕분에 LLM 감사를 추가해도 시스템이
기존보다 관대해지는 일은 구조적으로 발생하지 않는다.

━━ 감사 독립성 ━━
감사자에게는 **생성 과정을 보여주지 않는다.**
최종 답변 · 근거 문서 · 계산 결과만 전달한다.
생성 시의 추론을 함께 보면 그 논리에 동조하게 되므로,
독자의 입장에서 결과물만 보고 판단하게 만든다.

━━ 남는 한계 (정직하게 기록) ━━
동일 모델이 자신의 출력을 감사하므로 blind spot이 상관될 수 있다.
따라서 LLM 감사는 결정론적 감사를 **대체하지 않고 보완**한다.
결정론적 계층이 여전히 1차 방어선이다.
"""

# 법령 조문을 페이로드에 실을 때의 상한. 평가는 시간 예산이 있는 단일
# GET이므로 무제한으로 넣을 수 없다.
_MAX_LAW_ARTICLES = 10
_MAX_LAW_CHARS = 1200

LLM_AUDIT_SYSTEM_PROMPT = """당신은 연금 상담 답변의 **논리 정합성**을 점검하는 감사자입니다.
답변을 작성하지 말고, 심사만 하십시오.

━━ 무엇을 보는가 ━━
문장이 매끄러운지가 아니라 **말이 되는지**를 봅니다. 아래 순서로,
앞의 것을 더 중하게 보십시오.

1. 수치–서술 정합 — 제시된 숫자와 그것을 설명하는 문장이 서로 맞는가.
   수치는 맞는데 그 수치가 무엇인지 잘못 말하고 있지는 않은가.
   (예: 연금수령연차로 계산한 값을 연금실제수령연차라고 서술)
   **세율을 엉뚱한 과세방식에 붙이고 있지는 않은가**를 특히 보십시오.
   종합과세는 다른 소득과 합산해 누진세율로 과세하는 것이고,
   5.5~3.3%·4.4%·16.5%는 분리과세·원천징수 쪽 세율표입니다.
   "종합과세되면 5.5~3.3%가 적용된다" 같은 서술은 틀린 것입니다.
2. 전제–결론 정합 — 앞에서 "확인되지 않았다"고 한 조건을 뒤에서
   아는 것처럼 쓰고 있지는 않은가. 조건을 나눠 놓고 결론은 하나로
   내리고 있지는 않은가.
3. 근거–주장 정합 — 인용한 근거가 실제로 그 주장을 뒷받침하는가.
   근거는 A를 말하는데 결론은 B를 말하고 있지는 않은가.
4. 내부 모순 — 같은 답변 안에서 서로 어긋나는 서술이 있는가.
   (예: "한도가 없습니다"와 "한도는 1,200만원입니다"가 함께 있음)
5. 질문 전제의 검증 — **질문에 이미 틀린 전제가 들어 있는데 답변이 그것을
   그대로 받아들이고 있지는 않은가.** 사용자가 "○○ 맞죠?", "○○라던데요"
   라고 물었다면, 그 전제가 자료와 맞는지부터 확인해야 합니다.
   틀린 전제 위에 쌓은 조언은 그 자체로 손해가 됩니다.
   (예: 퇴직급여를 재원으로 하는 연금소득까지 1,500만원 한도에 포함된다고
    전제한 질문에, 그대로 "1,500만원 이하로 조정하라"고 답하는 경우)
6. 의도 충족 — 형식적으로 답했더라도 질문자가 실제로 알고자 한 것을
   다뤘는가. 되묻는 항목이 정말 결정적인가, 답할 수 있는데 미루는가.
   질문이 준 조건(나이·출생연도·기간 등)을 **쓰지 않고 넘어가지는 않았는가.**
7. 답변 태도 — 과장·불안 조장·지나친 단정·무책임한 회피가 없는가.

━━ 판정 기준 ━━
· 1~5(정합성·전제)에서 어긋남을 찾으면 REVISE 이상으로 판정하십시오.
  숫자가 맞아도 논리가 어긋나면 그 답변은 틀린 답변입니다.
· 6~7은 정합성이 무너지지 않았다면 지적하되 판정은 신중히 하십시오.
· **문체나 표현 취향으로 판정하지 마십시오.** 자연스럽게 이어지는
  문장으로 쓰도록 설계돼 있으므로, 구획이 없다는 이유로 지적하면 안 됩니다.

반드시 아래 JSON 형식으로만 답하십시오. 다른 텍스트를 덧붙이지 마십시오.

{
  "verdict": "APPROVE" | "REVISE" | "BLOCK",
  "findings": [
    {"code": "위 7개 항목 중 하나", "detail": "무엇이 무엇과 어긋나는지",
     "directive": "구체적 시정 지시"}
  ],
  "most_critical_questions": ["확인 요청 항목 중 가장 결정적인 것 최대 2개"]
}

문제가 없으면 verdict를 APPROVE로 하고 findings를 빈 배열로 두십시오."""


def build_llm_audit_payload(answer: str,
                            evidence_texts: list[str],
                            calc_results: list[dict],
                            question: str,
                            ask_back_items: Optional[list[str]] = None,
                            law_articles: Optional[list] = None,
                            candidate_traps: Optional[list[dict]] = None) -> str:
    """의미 감사용 입력 구성.

    생성 과정(프롬프트·추론)은 의도적으로 제외한다 — 감사 독립성.

    law_articles가 주어지면 함정 적용 여부 판정도 **이 호출 안에서** 함께
    받는다. 별도 호출을 만들지 않는 이유는 LLM 호출을 3개소(L1·L5'·L6)로
    고정해 둔 설계 때문이다 — 여기에 하나를 더하면 단일 GET의 시간 예산이
    무너진다.
    """
    parts = [f"[질문]\n{question}", f"\n[심사 대상 답변]\n{answer}"]

    if calc_results:
        parts.append("\n[계산 결과 — 이 값만 사실로 간주]")
        for r in calc_results:
            parts.append(str(r))

    if evidence_texts:
        parts.append("\n[근거 문서]")
        for t in evidence_texts[:6]:
            parts.append(t[:600])

    if ask_back_items:
        parts.append("\n[답변이 확인 요청한 항목]\n" + " / ".join(ask_back_items))

    if law_articles:
        parts.append(
            "\n[법령 조문 원문 — 아래 조문만 근거로 쓸 수 있다]\n"
            "※ 인용은 반드시 아래 원문에서 **글자 그대로** 옮길 것. "
            "요약하거나 바꿔 쓰면 검증에서 폐기된다.")
        # ⚠️ 앞에서부터 자르면 안 된다. 각 호까지 담은 조문은 수천 자라,
        #    정작 필요한 조항이 잘려 나간다 — 소득세법 제14조 제3항의
        #    1,500만원 기준은 여덟 개 호를 지나 제9호 다목에 있다.
        #    감사자가 인용할 문장을 못 보면 검증도 통과할 수 없으므로,
        #    함정의 검증 용어 주변을 잘라 넣는다.
        focus = [t for c in (candidate_traps or [])
                 for t in (c.get("verify_any") or [])]
        for a in law_articles[:_MAX_LAW_ARTICLES]:
            body = a.text
            if len(body) > _MAX_LAW_CHARS:
                if snips := focus_snippets(body, focus, width=400, limit=3):
                    body = "\n".join(snips)
                else:
                    body = body[:_MAX_LAW_CHARS]
            parts.append(f"\n<{a.ref}> (시행 {a.effective_date})\n{body}")

    if candidate_traps:
        parts.append(
            "\n[판정 대상 함정 — 각 항목이 이 질의에 실제로 적용되는지 "
            "조문 근거로 판정할 것]")
        for t in candidate_traps:
            parts.append(f"  · {t.get('id')}: {t.get('title')}")
        parts.append(
            "\n판정은 law_judgements 배열로 낼 것. 각 항목은 "
            "{trap_id, applies, law_ref, quote, rationale} 이며 "
            "quote는 위 조문 원문에서 그대로 옮긴 12자 이상의 문장이어야 한다. "
            "근거가 될 조문을 찾지 못하면 그 항목은 아예 내지 말 것 — "
            "지어낸 인용은 폐기되고 기록에 남는다.")

    return "\n".join(parts)


def _load_audit_json(raw: str) -> Optional[dict]:
    """감사 응답에서 JSON 객체를 꺼낸다. 실패하면 None."""
    import json
    text = (raw or "").strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def law_judgements_from_audit(raw: str) -> list:
    """감사 응답에 실린 함정 판정(law_judgements)을 꺼낸다.

    ⚠️ 여기서 나온 판정은 **아직 검증되지 않았다.** 반드시
    citation_guard.verify_judgements()를 통과시킨 뒤에 쓸 것.
    """
    from app.law.citation_guard import parse_law_judgements

    data = _load_audit_json(raw)
    if not data:
        return []
    return parse_law_judgements(data.get("law_judgements"))


def parse_llm_audit(raw: str) -> tuple[Verdict, list[Finding], list[str]]:
    """LLM 감사 응답 파싱. 파싱 실패는 '판정 없음'으로 처리한다.

    감사자가 응답을 못 주는 것과 '문제없음'은 다르다.
    파싱 실패 시 APPROVE로 간주하되, 감사가 수행되지 않았음을 별도 기록한다.
    """
    data = _load_audit_json(raw)
    if data is None:
        return Verdict.APPROVE, [Finding(
            "의미감사", "PARSE_FAIL", Verdict.APPROVE,
            "감사 응답을 해석하지 못함 — 의미 감사가 수행되지 않음", "")], []

    try:
        verdict = Verdict(str(data.get("verdict", "APPROVE")).upper())
    except ValueError:
        verdict = Verdict.APPROVE

    findings = [
        Finding("의미감사", str(f.get("code", "SEMANTIC")), verdict,
                str(f.get("detail", "")), str(f.get("directive", "")))
        for f in (data.get("findings") or []) if isinstance(f, dict)
    ]
    questions = [str(q) for q in (data.get("most_critical_questions") or [])][:MAX_ASK_BACK]
    return verdict, findings, questions


def merge_supervision(deterministic: SupervisionResult,
                      llm_verdict: Verdict,
                      llm_findings: list[Finding],
                      llm_questions: Optional[list[str]] = None,
                      answerability: str = "ANSWER") -> SupervisionResult:
    """결정론적 감사와 LLM 감사를 권한 계층에 따라 병합.

    LLM은 심각도를 **올릴 수만** 있다. 결정론적 판정을 완화하지 못한다.
    """
    final = deterministic.verdict
    if _SEVERITY_ORDER[llm_verdict] > _SEVERITY_ORDER[final]:
        final = llm_verdict

    findings = deterministic.findings + llm_findings
    directives = deterministic.directives + [f.directive for f in llm_findings if f.directive]

    # 확인 요청 항목은 LLM이 우선순위를 판단한 결과를 채택 (개수 상한은 유지)
    questions = deterministic.revised_ask_back
    if llm_questions:
        questions = llm_questions[:MAX_ASK_BACK]

    downgraded = deterministic.downgraded_answerability
    if final == Verdict.DOWNGRADE and not downgraded:
        downgraded = _DOWNGRADE_CHAIN.get(answerability, answerability)

    return SupervisionResult(
        verdict=final,
        findings=findings,
        directives=directives,
        revised_ask_back=questions,
        downgraded_answerability=downgraded,
    )


def _apply_law_judgements(raw: str,
                          answer: str,
                          det: SupervisionResult,
                          deterministic_kwargs: dict
                          ) -> tuple[SupervisionResult, list[str]]:
    """감사 응답의 법령 판정을 검증해 반영한다. (갱신된 판정, 기록)

    ━━ 왜 '재실행'인가 ━━
    검증을 통과한 판정은 함정 목록(trap_ids)을 바꾼다. 그런데 그 목록은
    **결정론적 감사의 입력**이다. 그래서 감사 결과를 나중에 손보는 대신,
    바뀐 입력으로 결정론적 감사를 **다시 돌린다**. 재실행은 LLM을 쓰지
    않으므로 호출 수는 그대로다.

    이 구조라야 원칙이 깨지지 않는다:
      · 최종 판정은 여전히 코드가 낸다 ("판단은 코드")
      · merge_supervision의 단조성도 그대로다 — LLM의 의미 감사 verdict는
        여전히 심각도를 올리기만 한다. 법령 판정이 바꾸는 것은 '무엇을
        판단할 것인가'(사실)이지 '어떻게 판단할 것인가'(판정)가 아니다.
      · 그 사실 변경조차 실재하는 조문 원문에 인용이 대조된 것만 통과한다
    """
    from app.law.citation_guard import apply_to_traps, verify_judgements
    from app.law.store import get_store

    trace: list[str] = []
    try:
        judgements = law_judgements_from_audit(raw)
        if not judgements:
            return det, trace

        kept, verify_trace = verify_judgements(get_store(), judgements)
        trace += verify_trace
        if not kept:
            return det, trace

        before = list(deterministic_kwargs.get("trap_ids") or [])
        after, apply_trace = apply_to_traps(before, kept)
        trace += apply_trace
        if set(after) == set(before):
            return det, trace

        # 함정이 빠졌으면 그 규칙의 검증 정보도 함께 빼야 한다.
        # 남겨 두면 적합성 감사가 없는 함정의 미해소를 계속 지적한다.
        checks = [c for c in (deterministic_kwargs.get("trap_checks") or [])
                  if c.get("id") in set(after)]
        redone = supervise(answer, **{**deterministic_kwargs,
                                      "trap_ids": after, "trap_checks": checks})
        redone.findings.append(Finding(
            "법령근거", "TRAP_ADJUSTED", redone.verdict,
            f"조문 근거로 함정 목록 조정: {before} → {after}", ""))
        trace.append(f"조문 근거 반영 후 결정론적 감사 재실행: "
                     f"{det.verdict.value} → {redone.verdict.value}")
        return redone, trace
    except Exception as e:                                   # noqa: BLE001
        # 법령 계층의 사고가 감사 전체를 죽이면 안 된다. 원래 판정을
        # 그대로 두되, 무슨 일이 있었는지는 반드시 남긴다.
        log.warning("법령 근거 판정 실패 — 결정론적 판정 유지: %s", e)
        trace.append(f"법령 근거 판정 실패(무시하고 진행): {e}")
        return det, trace


def supervise_hybrid(answer: str,
                     question: str,
                     llm_call,
                     evidence_texts: Optional[list[str]] = None,
                     skip_llm_on_block: bool = True,
                     law_articles: Optional[list] = None,
                     candidate_traps: Optional[list[dict]] = None,
                     **deterministic_kwargs) -> SupervisionResult:
    """결정론적 감사 + HyperCLOVA X 의미 감사를 함께 수행.

    llm_call : (system_prompt, user_payload) -> str 형태의 호출 함수.
               CLOVA Studio 클라이언트를 이 시그니처로 감싸서 주입한다.

    skip_llm_on_block : 결정론적 감사가 이미 BLOCK이면 LLM 호출을 생략한다.
                        어차피 판정이 바뀌지 않으므로 비용과 지연을 아낀다.

    law_articles / candidate_traps : 주어지면 함정 적용 여부 판정을 **이 같은
        호출 안에서** 함께 받아, 인용 검증을 통과한 것만 반영한다.
        LLM 호출은 여전히 1회다(L1·L5'와 합쳐 3개소 유지).
    """
    det = supervise(answer, **deterministic_kwargs)

    if skip_llm_on_block and det.verdict == Verdict.BLOCK:
        det.findings.append(Finding(
            "의미감사", "SKIPPED", Verdict.BLOCK,
            "결정론적 감사에서 이미 차단 — 의미 감사 생략", ""))
        return det

    payload = build_llm_audit_payload(
        answer=answer,
        evidence_texts=evidence_texts or [],
        calc_results=deterministic_kwargs.get("calc_results") or [],
        question=question,
        ask_back_items=deterministic_kwargs.get("ask_back_items"),
        law_articles=law_articles,
        candidate_traps=candidate_traps,
    )

    try:
        raw = llm_call(LLM_AUDIT_SYSTEM_PROMPT, payload)
    except Exception as e:
        det.findings.append(Finding(
            "의미감사", "CALL_FAIL", det.verdict,
            f"의미 감사 호출 실패: {e} — 결정론적 판정만 적용", ""))
        return det

    # 법령 판정을 **의미 감사 병합보다 먼저** 반영한다. 그래야 바뀐 함정
    # 목록으로 결정론적 감사가 다시 돌고, 그 결과 위에 의미 감사가 얹힌다.
    if law_articles and candidate_traps:
        det, law_trace = _apply_law_judgements(raw, answer, det,
                                               deterministic_kwargs)
        for line in law_trace:
            det.findings.append(Finding("법령근거", "TRACE", det.verdict,
                                        line, ""))

    llm_verdict, llm_findings, llm_questions = parse_llm_audit(raw)
    return merge_supervision(det, llm_verdict, llm_findings, llm_questions,
                             deterministic_kwargs.get("answerability", "ANSWER"))

