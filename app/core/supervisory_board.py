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

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


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


def audit_anomaly(calc_results: list[dict],
                   user_conditions: Optional[dict] = None) -> list[Finding]:
    """계산 이상치 감사.

    수치 대조 검증은 '근거에 있는 숫자인가'만 본다.
    이 감사는 '그 숫자가 말이 되는가'를 본다 — 둘은 다른 문제다.
    """
    findings: list[Finding] = []
    user_conditions = user_conditions or {}

    for r in calc_results:
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

        limit = r.get("limit")
        if limit is not None and isinstance(limit, (int, float)):
            base = user_conditions.get("account_value")
            if base and limit > base * 1.2 + 1e-6 and not r.get("unlimited"):
                findings.append(Finding(
                    "이상치", "LIMIT_RATIO", Verdict.REVISE,
                    f"연금수령한도({limit})가 평가액의 120%를 초과",
                    "연차 입력값을 재확인할 것",
                ))

    return findings


# ════════════════════════════════════════════════════════════════
# 3. 적합성 감사 — 사용자에게 맞는 정보인가
# ════════════════════════════════════════════════════════════════

# 계좌유형별로 답변에 등장하면 안 되는 표현
_ACCOUNT_MISMATCH = {
    "연금저축": ["퇴직연금 전용", "IRP 전용", "근로자퇴직급여보장법에 따라 인출"],
    "퇴직연금": ["사유와 무관하게 언제든 인출", "연금저축 전용"],
}


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
    if not acct and mentioned_products:
        findings.append(Finding(
            "적합성", "UNVERIFIED_RECOMMENDATION", Verdict.DOWNGRADE,
            "계좌 유형이 확인되지 않은 상태에서 특정 상품을 제시",
            "계좌 유형 확인을 먼저 요청하고, 유형별 후보를 조건부로 제시할 것",
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
            rest = [m["id"] for m in missed if m.get("severity") != "critical"]
            # 무엇을 어떻게 바로잡아야 하는지까지 준다 — 지시가 구체적이어야
            # 재생성이 성공한다. 예전의 뭉뚱그린 지시는 재생성도 실패했다.
            directive = " / ".join(
                f"[{m['id']}] {m.get('correction') or m.get('title', '')}"
                for m in missed[:3])
            findings.append(Finding(
                "적합성", "TRAP_UNADDRESSED",
                Verdict.REVISE if crit else Verdict.DOWNGRADE,
                f"감지된 함정 중 답변에서 다뤄지지 않은 것: "
                f"{crit + rest} (critical {len(crit)}건)",
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
    """4대 감사를 실행하고 종합 판정 + 시정 지시를 산출한다.

    이 함수는 검사만 하지 않는다. 무엇을 고칠지 지시하고,
    필요하면 답변 등급을 스스로 강등시킨다 — 이것이 감독이다.
    """
    calc_results = calc_results or []
    findings: list[Finding] = []

    findings += audit_compliance(answer, citations or [], has_calculation=bool(calc_results))
    findings += audit_anomaly(calc_results, user_conditions)
    findings += audit_fitness(answer, user_conditions, mentioned_products,
                              trap_ids, trap_checks)
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
    """
    if result.verdict != Verdict.REVISE or not result.directives:
        return ""
    lines = ["아래 답변을 다음 지적사항에 따라 수정하십시오.",
             "새로운 수치를 만들지 말고, 기존 계산 결과와 근거 문서만 사용하십시오.", ""]
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

LLM_AUDIT_SYSTEM_PROMPT = """당신은 연금 상담 답변을 심사하는 감사자입니다.
답변을 작성하지 말고, 심사만 하십시오.

다음 5개 항목을 점검하십시오.

1. 의미 정합성 — 수치는 맞더라도 설명이 그 수치의 의미를 잘못 서술하지 않았는가.
   특히 유사하지만 다른 개념을 혼동하지 않았는지 확인하십시오.
2. 답변 태도 — 금융 상담으로서 신뢰할 수 있는 태도인가.
   과장, 불안 조장, 지나친 단정, 무책임한 회피가 없는가.
3. 의도 충족 — 형식적으로 답했더라도 질문자가 실제로 알고자 한 것을 다뤘는가.
4. 오해 유발 — 틀린 서술은 없더라도 읽는 사람이 오해할 소지가 있는가.
5. 확인 요청의 타당성 — 되묻는 항목이 정말 결정적인가, 아니면 답할 수 있는데
   불필요하게 미루고 있는가.

반드시 아래 JSON 형식으로만 답하십시오. 다른 텍스트를 덧붙이지 마십시오.

{
  "verdict": "APPROVE" | "REVISE" | "BLOCK",
  "findings": [
    {"code": "위 5개 항목 중 하나", "detail": "발견한 문제", "directive": "구체적 시정 지시"}
  ],
  "most_critical_questions": ["확인 요청 항목 중 가장 결정적인 것 최대 2개"]
}

문제가 없으면 verdict를 APPROVE로 하고 findings를 빈 배열로 두십시오."""


def build_llm_audit_payload(answer: str,
                            evidence_texts: list[str],
                            calc_results: list[dict],
                            question: str,
                            ask_back_items: Optional[list[str]] = None) -> str:
    """의미 감사용 입력 구성.

    생성 과정(프롬프트·추론)은 의도적으로 제외한다 — 감사 독립성.
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

    return "\n".join(parts)


def parse_llm_audit(raw: str) -> tuple[Verdict, list[Finding], list[str]]:
    """LLM 감사 응답 파싱. 파싱 실패는 '판정 없음'으로 처리한다.

    감사자가 응답을 못 주는 것과 '문제없음'은 다르다.
    파싱 실패 시 APPROVE로 간주하되, 감사가 수행되지 않았음을 별도 기록한다.
    """
    import json
    text = (raw or "").strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()

    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            return Verdict.APPROVE, [Finding(
                "의미감사", "PARSE_FAIL", Verdict.APPROVE,
                "감사 응답을 해석하지 못함 — 의미 감사가 수행되지 않음",
                "")], []
        try:
            data = json.loads(m.group())
        except Exception:
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


def supervise_hybrid(answer: str,
                     question: str,
                     llm_call,
                     evidence_texts: Optional[list[str]] = None,
                     skip_llm_on_block: bool = True,
                     **deterministic_kwargs) -> SupervisionResult:
    """결정론적 감사 + HyperCLOVA X 의미 감사를 함께 수행.

    llm_call : (system_prompt, user_payload) -> str 형태의 호출 함수.
               CLOVA Studio 클라이언트를 이 시그니처로 감싸서 주입한다.

    skip_llm_on_block : 결정론적 감사가 이미 BLOCK이면 LLM 호출을 생략한다.
                        어차피 판정이 바뀌지 않으므로 비용과 지연을 아낀다.
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
    )

    try:
        raw = llm_call(LLM_AUDIT_SYSTEM_PROMPT, payload)
    except Exception as e:
        det.findings.append(Finding(
            "의미감사", "CALL_FAIL", det.verdict,
            f"의미 감사 호출 실패: {e} — 결정론적 판정만 적용", ""))
        return det

    llm_verdict, llm_findings, llm_questions = parse_llm_audit(raw)
    return merge_supervision(det, llm_verdict, llm_findings, llm_questions,
                             deterministic_kwargs.get("answerability", "ANSWER"))

