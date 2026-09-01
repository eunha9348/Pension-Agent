"""
근거 인용 시스템 (Citation System)
===================================
답변 하단에 사용된 근거 문서를 각주 형태로 제시한다.

일반화 설계 원칙
----------------
현재 프로젝트에 첨부된 문서는 전체 코퍼스(약 8,000건)의 일부다.
따라서 **문서 ID 화이트리스트에 의존하지 않는다.**

  · 문서 유형    : ID 패턴이 아니라 본문 내용 신호로 추론 (실패 시 '미분류'로 인용은 유지)
  · 위치 표기    : 조항·섹션 헤더를 정규식으로 추출 (없으면 생략, 인용은 유지)
  · 신뢰도 플래그: 구법 수치·외부출처 등을 자동 표시
  · 미지의 문서  : 규칙에 걸리지 않아도 절대 누락시키지 않는다 (누락 > 오분류)

핵심 원칙 — 인용은 '검색된 것'이 아니라 '사용된 것'
--------------------------------------------------
검색된 근거를 전부 나열하면 평가지표 '근거 완전성'
("질의 대상과 무관하거나 대상이 다른 근거를 배제했는가")에서 감점된다.
따라서 실제로 답변 문장을 뒷받침한 근거만 인용한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ════════════════════════════════════════════════════════════════
# 1. 문서 유형 추론 — 내용 신호 기반 (ID 패턴은 보조 신호로만)
# ════════════════════════════════════════════════════════════════

# 각 유형을 시사하는 본문 표현. 새 문서가 들어와도 이 표만 유지하면 동작한다.
_TYPE_SIGNALS: dict[str, list[str]] = {
    "투자설명서": [
        "투자설명서", "집합투자업자", "종류형 투자신탁", "판매회사", "신탁업자",
        "총보수", "위험등급", "환매", "수익증권", "가입자격",
    ],
    "제도안내": [
        "근로자퇴직급여보장법", "확정급여형", "확정기여형", "개인형퇴직연금",
        "퇴직연금제도", "제도전환", "중도인출", "가입자교육",
    ],
    "세제안내": [
        "세액공제", "연금소득세", "기타소득세", "퇴직소득세", "과세이연",
        "분리과세", "종합과세", "원천징수", "이연퇴직소득",
    ],
    "약관": [
        "약관", "제1조", "규약", "운용관리계약", "자산관리계약",
    ],
}

# ID 패턴 → 유형 힌트 (보조 신호. 단독으로 결정하지 않는다)
_ID_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'^R2_KR\w+', re.I), "투자설명서"),
]


def infer_doc_type(doc_id: str, text: str = "", override: Optional[str] = None) -> str:
    """문서 유형 추론. 명시적 override가 있으면 그것을 우선한다.

    내용 신호를 점수화하고, 동점이거나 신호가 없으면 ID 힌트로 보강한다.
    끝까지 판단이 안 되면 '미분류'를 반환하되 **인용 자체는 유지**한다.
    """
    if override:
        return override

    sample = text[:4000]  # 앞부분만 봐도 유형은 대개 드러난다
    scores = {t: sum(1 for kw in kws if kw in sample) for t, kws in _TYPE_SIGNALS.items()}
    best = max(scores, key=scores.get) if scores else None
    best_score = scores.get(best, 0) if best else 0

    if best_score >= 2:
        return best

    for pattern, hint in _ID_HINTS:
        if pattern.match(doc_id or ""):
            return hint

    return best if best_score == 1 else "미분류"


# ════════════════════════════════════════════════════════════════
# 2. 위치(locator) 추출 — 조항·섹션 헤더
# ════════════════════════════════════════════════════════════════

_LOCATOR_PATTERNS = [
    re.compile(r'제\s*\d+\s*조(?:의\s*\d+)?(?:\s*\([^)]{1,30}\))?'),   # 제47조의2 (연금소득공제)
    re.compile(r'【[^】]{1,40}】'),                                     # 【연금수령한도】
    re.compile(r'^\s*[■◆▶]\s*([^\n]{1,40})', re.M),                   # ■ 세제 관련
    re.compile(r'^\s*\d+[.)]\s*([^\n]{1,40})', re.M),                  # 3. 과세방식
]


def extract_locator(text: str, matched_span: Optional[str] = None) -> Optional[str]:
    """근거가 위치한 조항/섹션을 추출.

    matched_span이 주어지면 그 앞쪽에서 가장 가까운 헤더를 찾는다.
    찾지 못하면 None — 위치 없이 문서 단위로만 인용한다.
    """
    scope = text
    if matched_span and matched_span in text:
        idx = text.find(matched_span)
        scope = text[max(0, idx - 1200):idx]  # 근거 직전 구간만 역탐색

    found = None
    for pat in _LOCATOR_PATTERNS:
        matches = pat.findall(scope)
        if matches:
            last = matches[-1]
            last = last if isinstance(last, str) else last[0]
            found = last.strip()
    return found or None


# ════════════════════════════════════════════════════════════════
# 3. 인용 객체
# ════════════════════════════════════════════════════════════════

@dataclass
class Citation:
    doc_id: str
    marker: str = ""                       # 본문에 삽입될 표시 [1]
    doc_type: str = "미분류"
    locator: Optional[str] = None          # 제47조의2 등
    snippet: str = ""                      # 짧은 발췌
    supports: list[str] = field(default_factory=list)   # 뒷받침하는 항목
    flags: list[str] = field(default_factory=list)      # 구법의심 / 외부출처 등

    def render(self, show_snippet: bool = True, snippet_len: int = 60) -> str:
        """각주 한 줄 렌더링."""
        parts = [f"{self.marker} {self.doc_id}"]
        if self.doc_type and self.doc_type != "미분류":
            parts.append(f"({self.doc_type})")
        if self.locator:
            parts.append(self.locator)
        line = " ".join(parts)

        if self.supports:
            line += f" — {', '.join(self.supports[:2])}"
        if show_snippet and self.snippet:
            s = self.snippet.strip().replace("\n", " ")
            if len(s) > snippet_len:
                s = s[:snippet_len].rstrip() + "…"
            line += f'\n     "{s}"'
        if self.flags:
            line += f"\n     ⚠ {' · '.join(self.flags)}"
        return line


# ════════════════════════════════════════════════════════════════
# 4. 인용 조립
# ════════════════════════════════════════════════════════════════

def _snippet_for(text: str, keywords: Iterable[str], width: int = 90) -> str:
    """근거 텍스트에서 관련 키워드 주변을 발췌.

    발췌는 원문 확인용 최소 인용이며, 답변 본문을 대체하지 않는다.
    """
    for kw in keywords:
        if kw and kw in text:
            i = text.find(kw)
            start = max(0, i - width // 3)
            return text[start:start + width]
    return text[:width]


def build_citations(
    used_evidence: list[dict],
    calc_results: Iterable[Any] = (),
    doc_meta: Optional[dict[str, dict]] = None,
    external_sources: Optional[list[str]] = None,
    legacy_checker=None,
) -> list[Citation]:
    """실제로 사용된 근거만 인용으로 조립.

    used_evidence : [{"doc_id":..., "text":..., "supports":[...], "matched":"..."}]
                    supports는 이 근거가 뒷받침한 슬롯/주장 이름
    calc_results  : 계산 함수 반환값들 — 내부 source 필드에서 근거 문서를 추출
    doc_meta      : {doc_id: {"type":..., "title":...}} 형태의 선택적 메타데이터.
                    인제스트 단계에서 만들어두면 추론보다 우선 적용된다.
    external_sources: 제공자료 밖 근거(일반 세법 등)의 설명 문자열
    legacy_checker: detect_legacy_tax_content 같은 함수. 있으면 구법 플래그를 단다.
    """
    doc_meta = doc_meta or {}
    citations: dict[str, Citation] = {}

    # ── 검색 근거 ──
    for ev in used_evidence:
        did = ev.get("doc_id", "")
        if not did:
            continue
        text = ev.get("text", "")
        meta = doc_meta.get(did, {})

        if did not in citations:
            c = Citation(
                doc_id=did,
                doc_type=infer_doc_type(did, text, override=meta.get("type")),
                locator=extract_locator(text, ev.get("matched")),
                snippet=_snippet_for(text, [ev.get("matched", "")] + ev.get("supports", [])),
            )
            if legacy_checker:
                verdict = legacy_checker(text, did)
                if verdict.get("is_legacy_suspect"):
                    c.flags.append("법 개정 이전 수치 포함 가능 — 현행 기준 확인 필요")
            citations[did] = c

        for s in ev.get("supports", []):
            if s not in citations[did].supports:
                citations[did].supports.append(s)

    # ── 계산 함수가 명시한 근거 ──
    for r in calc_results:
        if not isinstance(r, dict):
            continue
        src = r.get("source") or r.get("rate_source")
        if not src or not isinstance(src, str):
            continue
        for did in re.split(r'[·,]\s*', src):
            did = did.strip()
            if not did or did.startswith("default"):
                continue
            if did not in citations:
                meta = doc_meta.get(did, {})
                citations[did] = Citation(
                    doc_id=did,
                    doc_type=infer_doc_type(did, "", override=meta.get("type")),
                    supports=["계산 근거"],
                )
            elif "계산 근거" not in citations[did].supports:
                citations[did].supports.append("계산 근거")

    ordered = list(citations.values())

    # ── 제공자료 밖 근거 ──
    for ext in (external_sources or []):
        ordered.append(Citation(
            doc_id=ext, doc_type="외부자료",
            flags=["제공 자료 외 일반 기준 — 개별 상황에 따라 달라질 수 있음"],
        ))

    for i, c in enumerate(ordered, 1):
        c.marker = f"[{i}]"
    return ordered


# ════════════════════════════════════════════════════════════════
# 5. 렌더링
# ════════════════════════════════════════════════════════════════

def render_footnotes(citations: list[Citation],
                     show_snippet: bool = True,
                     header: str = "근거 문서") -> str:
    """답변 하단에 붙일 각주 블록."""
    if not citations:
        return "근거 문서: 해당 없음 (제공 자료에서 확인된 근거가 없습니다)"
    lines = [f"─── {header} " + "─" * max(0, 30 - len(header))]
    lines += [c.render(show_snippet=show_snippet) for c in citations]
    return "\n".join(lines)


def attach_citations(answer: str, citations: list[Citation],
                     show_snippet: bool = True) -> str:
    """답변 본문 + 각주 블록 결합."""
    return answer.rstrip() + "\n\n" + render_footnotes(citations, show_snippet)


def citations_to_retrieved_context(citations: list[Citation],
                                    used_evidence: list[dict]) -> str:
    """평가 API의 retrieved_context 필드용 직렬화.

    각주는 사람이 읽는 요약본이고, retrieved_context는 채점자가 근거를
    직접 확인하는 필드이므로 원문을 더 길게 담는다.
    """
    by_id = {e.get("doc_id"): e.get("text", "") for e in used_evidence}
    blocks = []
    for c in citations:
        body = by_id.get(c.doc_id, "")
        head = f"[{c.doc_id}]"
        if c.doc_type != "미분류":
            head += f" ({c.doc_type})"
        if c.locator:
            head += f" {c.locator}"
        blocks.append(f"{head}\n{body}" if body else head)
    return "\n---\n".join(blocks)


# ════════════════════════════════════════════════════════════════
# 6. 인용 무결성 검사
# ════════════════════════════════════════════════════════════════

def verify_citation_integrity(answer: str, citations: list[Citation],
                               slots_used: Optional[list[str]] = None) -> dict:
    """인용이 요건을 충족하는지 검사.

    · 근거 없는 답변인데 수치·단정 표현이 있는가
    · 사용된 슬롯 중 인용이 붙지 않은 것이 있는가
    · 구법 의심 문서가 인용에 포함됐는가
    """
    issues = []

    # ⚠️ "근거 없이 수치를 제시함"(citations 0건 + 답변에 숫자 하나라도)을
    #    여기서 보던 것을 **제거했다**(2026-09-01). 두 가지 이유다.
    #
    #    ① 오탐이 압도적이다. 298건 실측에서 20건(6.7%)이 걸렸는데 거의
    #       전부 **질의에서 되읊은 숫자**였다 — "만 55세면 수령 가능한가요"에
    #       "55세"라고 답한 것, "만 65세가 연 1200만원 받으면"에 그 수치를
    #       그대로 확인해 준 것 등. 지어낸 수치가 아니다.
    #    ② 진짜 판정은 이미 `verify_numeric_grounding`이 한다. 그쪽은
    #       질의에 있던 숫자를 허용 집합에 넣고 대조하므로 위 오탐이 없다.
    #       즉 이 검사는 **중복이면서 더 느슨한 버전**이었다.
    #
    #    남겨 두면 "인용 무결성이 수치를 검사한다"는 잘못된 인상을 준다.
    #    실제로 trace에는 지적이 찍히는데 등급에는 반영되지 않아, 감사가
    #    작동하는 것처럼 보이지만 아무 일도 하지 않는 상태였다.
    #    지어낸 **상품명**은 `verify_product_grounding`이 따로 본다.

    if slots_used:
        covered = {s for c in citations for s in c.supports}
        missing = [s for s in slots_used if s not in covered and s != "계산 근거"]
        if missing:
            issues.append(f"인용이 연결되지 않은 항목: {missing}")

    legacy = [c.doc_id for c in citations if any("개정 이전" in f for f in c.flags)]
    if legacy:
        issues.append(f"구법 의심 문서가 인용됨: {legacy} — 현행 기준 문서로 대체 검토")

    external = [c.doc_id for c in citations if c.doc_type == "외부자료"]
    if external and not any(k in answer for k in ("일반 기준", "제공 자료 외", "별도 확인", "세법 기준")):
        issues.append("제공자료 외 근거를 사용했으나 답변에 그 사실이 명시되지 않음")

    return {"ok": not issues, "issues": issues,
            "citation_count": len(citations),
            "trace": ("인용 무결성 통과" if not issues
                      else f"인용 무결성 지적 {len(issues)}건: {issues}")}


# ════════════════════════════════════════════════════════════════
# 7. 상품명 접지 검사 — 근거에 없는 상품을 지어내지 않았는가
# ════════════════════════════════════════════════════════════════
#
# ━━ 왜 필요한가 (2026-09-01 실물 확인) ━━
# "은퇴 5년 남았는데 안전한 상품 추천해줘"에 대해, 근거 문서가 **0건인데도**
# 답변이 실존 펀드명을 콕 집어 추천했다. 과제 자료가 명시한 "근거 문서 고정"
# 원칙을 정면으로 위반한 hallucination이다.
#
# 기존 상품 감사(audit_fitness)는 이것을 잡을 수 없었다. 그 감사는
# `_products_in_answer()`가 넘긴 **검색 후보와 답변의 교집합**만 보기
# 때문이다. 근거가 0건이면 후보가 비고, 후보가 비면 교집합도 비어서
# **지어낸 이름은 애초에 검사 대상에 오르지 않는다.** 즉 "가입 불가 상품을
# 추천했는가"는 보면서 "존재 근거가 없는 상품을 지어냈는가"는 보지 않았다.
#
# ━━ 오탐을 어떻게 막는가 ━━
# ① 이름처럼 생긴 것만 본다 — 공백 없는 8자 이상의 토큰이 상품 접미사로
#    끝날 때만. "이 투자신탁은", "증권 투자신탁" 같은 일반 서술은 공백이
#    있거나 짧아서 걸리지 않는다.
# ② 대조는 공백을 무시한다 — 코퍼스 OCR은 같은 이름도 띄어쓰기가 제각각이다
#    ("미래에셋 퇴직연금" / "미래에셋퇴직연금").
# ③ 앞 8자만 맞으면 통과 — 근거 문서가 겹쳐 그려짐·판독 실패로 뒷부분이
#    깨져 있어도, 브랜드·시리즈 부분이 실재하면 지어낸 것이 아니다.
#    (오탐이 미탐보다 나쁘다는 원칙 — 결정론 계층이라 되돌릴 수 없다)

# 상품명으로 끝나는 접미사. 일반명사로도 쓰이므로 ①의 길이 조건과 함께 쓴다.
_PRODUCT_SUFFIX = ("자투자신탁", "증권투자신탁", "투자신탁", "상장지수펀드",
                   "증권펀드", "펀드", "변액연금보험", "연금보험")
# 이름 토큰 — 공백·구두점으로 끊기지 않는 한글/영숫자 덩어리
_NAME_TOKEN = re.compile(r'[가-힣A-Za-z0-9]{8,}')
# 브랜드·시리즈 식별에 쓰는 앞부분 길이
_NAME_PREFIX = 8


def _no_space(s: str) -> str:
    return re.sub(r'\s+', '', s or "")


# 상품 '종류'를 가리키는 일반명사. 고유명사가 아니므로 근거 대조 대상이
# 아니다. 길이 조건(8자)만으로는 "타깃데이트형펀드"처럼 긴 카테고리명이
# 걸리므로 명시적으로 제외한다.
_GENERIC_PRODUCT_TERMS = frozenset({
    "상장지수펀드", "타깃데이트펀드", "타깃데이트형펀드", "인덱스펀드",
    "채권형펀드", "주식형펀드", "혼합형펀드", "채권혼합형펀드",
    "연금저축펀드", "재간접펀드", "머니마켓펀드", "사모펀드", "공모펀드",
    "국공채형펀드", "원리금보장형펀드", "실적배당형펀드",
    "증권투자신탁", "집합투자기구", "변액연금보험", "연금보험",
})


def extract_product_names(answer: str) -> list[str]:
    """답변에서 상품명처럼 보이는 **고유명사**를 뽑는다.

    일반 서술("이 투자신탁은", "증권 투자신탁의")은 공백이 있거나 짧아
    걸리지 않고, 상품 종류를 가리키는 일반명사는 명시적으로 제외한다.
    """
    out: list[str] = []
    for m in _NAME_TOKEN.finditer(answer or ""):
        tok = m.group(0)
        if tok in _GENERIC_PRODUCT_TERMS:
            continue
        if any(tok.endswith(sfx) for sfx in _PRODUCT_SUFFIX):
            if tok not in out:
                out.append(tok)
    return out


def verify_product_grounding(answer: str,
                             evidence_texts: Iterable[str]) -> dict:
    """답변이 든 상품명이 근거 문서에 실재하는가.

    반환: {"passed": bool, "ungrounded": [이름...], "checked": n, "reason": str}
    """
    names = extract_product_names(answer)
    if not names:
        return {"passed": True, "ungrounded": [], "checked": 0,
                "reason": "답변에 상품명 표기가 없음"}

    haystack = _no_space(" ".join(evidence_texts or []))
    ungrounded = [n for n in names
                  if _no_space(n)[:_NAME_PREFIX] not in haystack]

    if not ungrounded:
        return {"passed": True, "ungrounded": [], "checked": len(names),
                "reason": f"상품명 {len(names)}건 전부 근거 문서에서 확인"}
    return {
        "passed": False, "ungrounded": ungrounded, "checked": len(names),
        "reason": (f"근거 문서에 없는 상품명 {len(ungrounded)}건: "
                   f"{', '.join(ungrounded)}"),
    }
