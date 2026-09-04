"""법령 조문 데이터 구조와 인용 대조.

이 모듈의 존재 이유
------------------
함정 규칙 26종의 `fact`는 지금까지 **제공문서 ID**만 출처로 달고 있었다.
그 서술이 실제 법령과 일치하는지는 아무도 대조하지 않았다. 외부 법령을
수집해 그 대조를 가능하게 만드는 것이 이 계층의 목적이다.

⚠️ 이 모듈의 최상위 불변식 — **조문 원문은 절대 가공하지 않는다.**
   요약·의역·정규화 저장 모두 금지다. 원문을 그대로 보관해야만
   "LLM이 말한 근거가 실재하는가"를 문자열 대조로 판정할 수 있다.
   가공된 텍스트와 대조하면 그 대조 자체가 신뢰를 잃는다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

# 인용 대조 시 허용하는 유일한 정규화 — 공백 계열의 통일.
#
# ━━ 왜 공백만인가 ━━
# LLM이 조문을 인용할 때 줄바꿈·들여쓰기가 달라지는 것은 흔하고, 그것까지
# 불일치로 보면 정탐을 전부 잃는다. 반면 문장부호·조사·어미를 정규화하면
# **의역이 통과**한다. 의역이 통과하는 순간 이 대조는 할루시네이션을 막는
# 기능을 잃으므로, 공백 외에는 아무것도 건드리지 않는다.
_WS = re.compile(r'\s+')

# 인용이 이보다 짧으면 대조가 무의미하다.
# "연금", "소득세" 같은 두세 글자는 어느 조문에나 있어서 항상 통과한다.
MIN_QUOTE_CHARS = 12

# 답변 쪽 인용(저촉으로 지목된 문장)의 최소 길이.
# 조문 인용보다 짧게 잡는 이유는 답변 문장이 조문보다 짧기 때문이다.
# 다만 "입니다", "600만원" 같은 조각은 답변 어디에나 있어 대조가
# 무의미하므로 하한을 둔다.
MIN_SPAN_CHARS = 10


def normalize_for_match(text: str) -> str:
    """인용 대조 전용 정규화. 공백 통일과 유니코드 정준화까지만 한다.

    NFKC는 전각/반각 숫자·괄호 차이를 흡수한다(법령 원문에 실제로 섞여 있다).
    의미를 바꾸지 않으므로 허용 범위 안이다.
    """
    return _WS.sub(' ', unicodedata.normalize('NFKC', text or '')).strip()


def focus_snippets(text: str, terms: list[str], width: int = 240,
                   limit: int = 3) -> list[str]:
    """주어진 용어 주변만 잘라낸다.

    ━━ 왜 필요한가 ━━
    각 호까지 담은 조문은 수천 자가 된다. 앞에서부터 잘라 쓰면 정작 필요한
    조항이 밖으로 밀린다 — 소득세법 제14조 제3항이 그렇다. 1,500만원 기준은
    여덟 개 호를 지나 제9호 다목에 있어서, 머리말만 넘기면 감사자가 인용할
    문장을 볼 수 없다. 인용할 수 없으면 검증도 통과할 수 없으므로,
    법령 근거 판정이 통째로 무력해진다.
    """
    norm = normalize_for_match(text)
    spans: list[list[int]] = []
    for t in terms:
        tn = normalize_for_match(t)
        if tn and (i := norm.find(tn)) >= 0:
            spans.append([max(0, i - width // 2),
                          min(len(norm), i + len(tn) + width // 2)])
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 20:      # 가까우면 한 덩어리로
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [("…" if s > 0 else "") + norm[s:e].strip()
            + ("…" if e < len(norm) else "")
            for s, e in merged[:limit]]


@dataclass(frozen=True)
class LawArticle:
    """법령의 조문 하나. text는 수집 원문 그대로다."""

    law_name: str          # "소득세법"
    article_no: str        # "제59조의3"
    clause_no: str         # "제1항" — 항 단위로 안 쪼갠 경우 빈 문자열
    text: str              # ★ 원문 그대로. 절대 가공 금지
    effective_date: str    # "2024-01-01" — 시행일. 구법 판별의 근거
    source_url: str
    fetched_at: str

    @property
    def ref(self) -> str:
        """사람이 읽고 LLM이 인용하는 표준 식별자."""
        parts = [self.law_name, self.article_no]
        if self.clause_no:
            parts.append(self.clause_no)
        return ' '.join(parts)

    @property
    def checksum(self) -> str:
        """원문 무결성 지문. 저장본이 바뀌었는지 확인하는 데 쓴다."""
        return hashlib.sha256(self.text.encode('utf-8')).hexdigest()[:16]

    def contains_verbatim(self, quote: str) -> bool:
        """인용문이 이 조문 안에 **글자 그대로** 있는가.

        여기가 할루시네이션 차단선이다. 참이 되려면 LLM이 지어낼 수 없는
        것 — 실제 조문 문자열 — 을 제시해야 한다.
        """
        q = normalize_for_match(quote)
        if len(q) < MIN_QUOTE_CHARS:
            return False
        return q in normalize_for_match(self.text)

    def to_dict(self) -> dict:
        return {
            "law_name": self.law_name,
            "article_no": self.article_no,
            "clause_no": self.clause_no,
            "text": self.text,
            "effective_date": self.effective_date,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "checksum": self.checksum,
        }

    @staticmethod
    def from_dict(d: dict) -> "LawArticle":
        art = LawArticle(
            law_name=str(d.get("law_name", "")),
            article_no=str(d.get("article_no", "")),
            clause_no=str(d.get("clause_no", "")),
            text=str(d.get("text", "")),
            effective_date=str(d.get("effective_date", "")),
            source_url=str(d.get("source_url", "")),
            fetched_at=str(d.get("fetched_at", "")),
        )
        # 저장본이 손상됐는지 즉시 안다. 조용히 넘어가면 손상된 원문과
        # 대조하게 되고, 그러면 정상 인용이 기각된다.
        saved = d.get("checksum")
        if saved and saved != art.checksum:
            raise ValueError(
                f"조문 원문이 저장 시점과 다릅니다: {art.ref} "
                f"(저장 {saved} ≠ 현재 {art.checksum})")
        return art


@dataclass
class CitationCheck:
    """인용 검증 결과. ok=False면 그 판정은 폐기된다."""

    ok: bool
    reason: str
    article: LawArticle | None = None
    matched_quote: str = ""

    def as_trace(self) -> str:
        head = "인용 검증 통과" if self.ok else "인용 검증 실패"
        ref = self.article.ref if self.article else "(조문 미상)"
        return f"{head} — {ref}: {self.reason}"


@dataclass
class LawJudgement:
    """HCX가 조문을 근거로 내린 함정 적용 판정 (검증 전)."""

    trap_id: str
    applies: bool
    law_ref: str
    quote: str
    rationale: str = ""
    # 검증 통과 후에만 채워진다
    verified: bool = False
    check: CitationCheck | None = field(default=None)


@dataclass
class LawConflict:
    """HCX가 지목한 **답변–조문 저촉** (검증 전).

    ━━ LawJudgement와 무엇이 다른가 ━━
    LawJudgement는 "이 함정이 이 질의에 적용되는가"를 묻는다. 질의에 대한
    판정이므로 답변 내용과는 무관하다. 그래서 답변이 조문에 어긋나는
    말을 해도 그 판정으로는 잡히지 않는다 — 애초에 묻지 않기 때문이다.

    이 구조체는 그 빈자리를 메운다. 묻는 것은 "**이 답변의 이 문장**이
    이 조문에 저촉되는가"이며, 따라서 답변 쪽 지목 문장(answer_span)이
    반드시 함께 온다.

    ━━ 차단선이 두 겹인 이유 ━━
    조문 인용만 대조하면, 모델이 실재하는 조문을 정확히 인용해 놓고
    **답변이 하지도 않은 말**을 저촉이라고 지어낼 수 있다. 그러면 멀쩡한
    답변이 강제로 재생성되고 강등 고지까지 붙는다 — 결정론 계층의 오탐은
    되돌릴 수 없으므로(CLAUDE.md) 미탐보다 나쁘다. 그래서 답변 쪽도
    글자 그대로 대조한다.

      quote       → 조문 원문에 그대로 있어야 한다
      answer_span → 답변 본문에 그대로 있어야 한다

    둘 중 하나라도 실패하면 그 저촉 주장은 폐기된다.
    """

    law_ref: str
    quote: str             # 조문 원문 그대로
    answer_span: str       # 답변 본문 그대로
    conflict: str = ""     # 무엇이 어떻게 어긋나는지
    # 검증 통과 후에만 채워진다
    verified: bool = False
    check: CitationCheck | None = field(default=None)
    span_ok: bool = False
    reason: str = ""

    def as_directive(self) -> str:
        """재생성에 실을 시정 지시. 조문 근거를 함께 밝힌다."""
        ref = self.check.article.ref if (self.check and self.check.article) \
            else self.law_ref
        return (f"답변의 \"{self.answer_span}\" 부분이 {ref}에 저촉됩니다. "
                f"조문은 \"{self.quote}\"라고 정하고 있습니다. "
                f"{self.conflict} 이 부분을 조문에 맞게 고쳐 쓰십시오.")
