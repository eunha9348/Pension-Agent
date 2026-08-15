"""거절 판정 (REFUSE) — 결정론적.

━━ 왜 중요한가 ━━
평가지표 중 최고 가중치가 "정보한계 대응"이다. 모르는 것을 아는 척하는 답변이
가장 크게 감점된다. 그런데 기존 `decide_answerability`에는 REFUSE 반환 경로가
아예 없었다 — 무엇을 물어도 ANSWER/PARTIAL/ASK_BACK 중 하나가 나왔다.

━━ 거절과 되묻기는 다르다 ━━
  ASK_BACK : 답할 수 있는 영역인데 **사용자 조건**이 모자란다 → 확인 요청
  REFUSE   : 애초에 답할 수 없다 (영역 밖 / 근거 없음 / 응해선 안 되는 요구)

이 구분을 흐리면 두 지표가 동시에 나빠진다. 영역 밖 질의에 되물으면
대화가 이어질 것처럼 오해를 주고, 답할 수 있는 질의를 거절하면 회피가 된다.

━━ LLM을 쓰지 않는다 ━━
"판단은 코드" 원칙. 거절 여부를 LLM 재량에 맡기면 프롬프트 인젝션에
그대로 뚫린다 — 거절 판정 자체가 공격 대상이 되기 때문이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.core.grounding_retrieval import DOMAIN_AREAS, OUT_OF_SCOPE_SIGNALS


@dataclass
class RefusalCheck:
    refuse: bool
    code: str = ""
    reason: str = ""          # 사용자에게 보여줄 문장
    detail: str = ""          # think_trace에 남길 근거

    def __bool__(self) -> bool:
        return self.refuse


OK = RefusalCheck(False)


# ── 1. 개인정보·계좌 조회 요구 ────────────────────────────────
# 이 에이전트는 문서 기반 안내만 한다. 개인 계좌·신원 정보에 접근하지 않는다.
_PII_REQUEST = [
    re.compile(r'주민(등록)?번호'),
    re.compile(r'계좌\s*번호'),
    re.compile(r'비밀\s*번호|패스워드|password', re.I),
    # ⚠️ 명사를 '연금'까지 넓히면 "내 연금저축 세액공제 얼마나 되는지 알려줘"
    #    같은 정상 질의까지 거절하게 된다. 계좌 데이터 어휘로만 한정한다.
    re.compile(r'(내|제|본인)\s*(계좌|잔고|적립금|평가액|수익률|납입\s*내역)'
               r'[^.?!]{0,12}?(조회|확인|알려|보여|얼마)'),
    re.compile(r'(고객|타인|남의|다른\s*사람)\s*(정보|계좌|연금)\s*(를)?\s*(조회|확인|알려)'),
    re.compile(r'카드\s*번호|CVC', re.I),
]

# ── 2. 프롬프트 인젝션 ────────────────────────────────────────
_INJECTION = [
    re.compile(r'(이전|위|앞)의?\s*(지시|명령|규칙|프롬프트).{0,6}(무시|잊)'),
    re.compile(r'시스템\s*(프롬프트|메시지|지시)'),
    re.compile(r'(너|당신)의?\s*(프롬프트|지시문|설정|규칙).{0,8}(알려|보여|출력|말해)'),
    re.compile(r'ignore\s+(all\s+)?(previous|prior|above)', re.I),
    re.compile(r'(system\s*prompt|developer\s*message)', re.I),
    re.compile(r'역할을?\s*(무시|바꿔|벗어)'),
    re.compile(r'제한\s*(없이|해제)|규칙\s*(없이|무시)'),
]

# ── 3. 문서 범위 밖 ───────────────────────────────────────────
_DOMAIN_KEYWORDS = {k for kws in DOMAIN_AREAS.values() for k in kws}
# 연금 도메인의 주변 어휘 — 이게 있으면 도메인 밖이라 단정하지 않는다
_SOFT_DOMAIN = {"연금", "퇴직", "노후", "수령", "납입", "적립", "세액", "과세",
                "IRP", "irp", "계좌", "가입", "인출", "공제"}


def check_refusal(question: str,
                  grounding=None,
                  evidence_count: Optional[int] = None) -> RefusalCheck:
    """질의를 거절해야 하는지 판정.

    grounding : GroundingResult (L0 산출물). 있으면 도메인 커버리지를 함께 본다.
    evidence_count : L3 검색 결과 건수. 0이면 근거 없음 경로.
    """
    q = question or ""

    if not q.strip():
        return RefusalCheck(True, "EMPTY_QUERY",
                            "질문 내용이 비어 있어 답변드릴 수 없습니다.",
                            "질의 문자열이 비어 있음")

    # ── 개인정보 ──
    for pat in _PII_REQUEST:
        if pat.search(q):
            return RefusalCheck(
                True, "PII_REQUEST",
                "개인 계좌나 신원 정보는 확인해 드릴 수 없습니다. "
                "제도·세제·상품 자료에 근거한 일반 안내만 제공합니다.",
                f"개인정보/계좌조회 요구 패턴 감지: {pat.pattern}")

    # ── 프롬프트 인젝션 ──
    for pat in _INJECTION:
        if pat.search(q):
            return RefusalCheck(
                True, "PROMPT_INJECTION",
                "요청하신 내용은 답변 범위를 벗어납니다. "
                "연금 제도·세제·상품에 대해 문의해 주세요.",
                f"지시 무시/시스템 프롬프트 노출 요구 감지: {pat.pattern}")

    # ── 명시적 도메인 밖 신호 ──
    for signal in OUT_OF_SCOPE_SIGNALS:
        if signal in q:
            return RefusalCheck(
                True, "OUT_OF_DOMAIN",
                f"'{signal}'은(는) 제공 자료가 다루는 연금 영역 밖입니다.",
                f"도메인 밖 신호 '{signal}' 감지")

    # ── L0 접지 결과 ──
    if grounding is not None and getattr(grounding, "early_refuse", False):
        return RefusalCheck(True, "L0_EARLY_REFUSE",
                            grounding.refuse_reason or
                            "제공 자료에서 관련 내용을 찾지 못했습니다.",
                            grounding.trace)

    # ── 도메인 어휘가 전혀 없다 ──
    has_domain = any(k in q for k in _DOMAIN_KEYWORDS) or any(k in q for k in _SOFT_DOMAIN)
    if not has_domain and evidence_count == 0:
        return RefusalCheck(
            True, "NO_DOMAIN_NO_EVIDENCE",
            "제공된 연금 자료 범위에서는 답변드리기 어려운 질문입니다.",
            "도메인 어휘 없음 + 검색 근거 0건")

    # ── 근거가 하나도 없다 ──
    if evidence_count == 0:
        return RefusalCheck(
            True, "NO_EVIDENCE",
            "제공된 자료에서 이 질문을 뒷받침할 근거를 찾지 못했습니다.",
            "L3 정밀 검색 결과 0건 — 근거 없이 답변하지 않음")

    return OK
