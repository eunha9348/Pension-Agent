"""수집한 법령 조문 저장소.

조문은 `data/law/articles.json`에 원문 그대로 보관한다. 이 저장소는
읽기 전용이며, 조회 실패를 조용히 넘기지 않는다 — 없는 조문을 인용한
판정은 반드시 실패로 떨어져야 하기 때문이다.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

from app.law.schema import LawArticle

log = logging.getLogger(__name__)

DEFAULT_STORE = Path(__file__).resolve().parents[2] / "data" / "law" / "articles.json"

# 조문 참조 표기 흔들림 흡수용.
# "소득세법 제59조의3 제1항", "소득세법제59조의3제1항", "소득세법 59조의3 1항"을
# 같은 키로 모은다. 표기 차이로 실재하는 조문을 못 찾으면, 맞는 판정이
# 인용 검증에서 억울하게 기각된다.
_REF_NOISE = re.compile(r'[\s·「」（）()]+')


def canon_ref(ref: str) -> str:
    """조문 참조를 대조용 표준형으로. 의미를 바꾸지 않는 표기 차이만 흡수한다."""
    s = unicodedata.normalize('NFKC', ref or '')
    s = _REF_NOISE.sub('', s)
    # '제'는 있어도 없어도 같은 조문을 가리킨다
    s = re.sub(r'제(?=\d)', '', s)
    return s


class LawStore:
    """조문 조회. 참조 문자열 → LawArticle."""

    def __init__(self, articles: list[LawArticle]):
        self._articles = list(articles)
        self._by_ref: dict[str, LawArticle] = {}
        # 조 단위 색인 — 항 표기 없이 조만 대면 그 조의 항 전체가 후보가 된다.
        self._by_article: dict[str, list[LawArticle]] = {}
        for a in self._articles:
            self._by_ref.setdefault(canon_ref(a.ref), a)
            key = canon_ref(f"{a.law_name} {a.article_no}")
            self._by_article.setdefault(key, []).append(a)

    # ── 조회 ─────────────────────────────────────────────────
    def get(self, ref: str) -> LawArticle | None:
        """참조로 조문 하나를 찾는다. 없으면 None — 예외를 던지지 않는다.

        호출부(인용 검증)가 '없음'을 실패 사유로 기록해야 하므로,
        여기서 던지면 그 기록이 사라진다.
        """
        cands = self.get_candidates(ref)
        return cands[0] if cands else None

    def get_candidates(self, ref: str) -> list[LawArticle]:
        """참조에 해당할 수 있는 조문들. 인용 대조는 이 중 하나만 맞으면 된다.

        ━━ 왜 목록인가 ━━
        LLM은 항 표기를 흘리는 일이 잦다("제10조 제1항"을 "제10조"로).
        저장소는 항 단위로 쪼개 두었으므로, 조만 주어지면 그 조의 항 전부가
        후보다. 원문이 그중 어디엔가 실재하면 통과시키는 것이 맞다 —
        표기 습관 때문에 실재하는 근거를 기각하면 정탐을 잃는다.
        반대로 조가 아예 없으면 후보도 없어 그대로 폐기된다.
        """
        key = canon_ref(ref)
        if (a := self._by_ref.get(key)) is not None:
            return [a]
        if (group := self._by_article.get(key)):
            return list(group)
        # 항까지 붙여 물었는데 저장본이 조 단위인 경우 — 항을 떼고 한 번 더.
        stripped = re.sub(r'\d+항$', '', key)
        if stripped != key:
            if (a := self._by_ref.get(stripped)) is not None:
                return [a]
            if (group := self._by_article.get(stripped)):
                return list(group)
        return []

    def search(self, *terms: str) -> list[LawArticle]:
        """원문에 주어진 용어가 모두 들어간 조문. 앵커 후보 제안용."""
        out = []
        for a in self._articles:
            body = unicodedata.normalize('NFKC', a.text)
            if all(unicodedata.normalize('NFKC', t) in body for t in terms if t):
                out.append(a)
        return out

    # ── 상태 ─────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._articles)

    @property
    def is_empty(self) -> bool:
        return not self._articles

    @property
    def law_names(self) -> list[str]:
        return sorted({a.law_name for a in self._articles})

    def articles(self) -> list[LawArticle]:
        return list(self._articles)

    # ── 적재 ─────────────────────────────────────────────────
    @staticmethod
    def load(path: Path | str | None = None) -> "LawStore":
        """저장소를 읽는다. 파일이 없으면 **빈 저장소**를 돌려준다.

        빈 저장소는 정상 상태다 — 아직 수집하지 않았을 뿐이다. 이때
        법령 근거 판정은 전부 비활성화되고 기존 결정론적 경로만 돈다.
        없다고 죽으면 법령 없이도 돌아가야 할 평가 경로가 막힌다.
        """
        p = Path(path or DEFAULT_STORE)
        if not p.exists():
            log.info("법령 저장소가 없습니다(%s) — 법령 근거 판정을 건너뜁니다", p)
            return LawStore([])
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # 손상된 저장소를 빈 것으로 넘기면 원인 모를 기능 실종이 된다.
            raise RuntimeError(f"법령 저장소를 읽지 못했습니다: {p}") from e

        items = raw.get("articles") if isinstance(raw, dict) else raw
        arts = [LawArticle.from_dict(d) for d in (items or [])]
        log.info("법령 조문 %d건 적재 (%s)", len(arts),
                 ", ".join(sorted({a.law_name for a in arts})) or "없음")
        return LawStore(arts)

    @staticmethod
    def save(articles: list[LawArticle], path: Path | str | None = None) -> Path:
        p = Path(path or DEFAULT_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "count": len(articles),
            "articles": [a.to_dict() for a in articles],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        return p


_CACHED: LawStore | None = None


def get_store(reload: bool = False) -> LawStore:
    """프로세스 수명 동안 한 번만 읽는다. 조문은 요청 중 바뀌지 않는다."""
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = LawStore.load()
    return _CACHED
