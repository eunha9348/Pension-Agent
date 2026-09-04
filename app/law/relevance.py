"""답변과 관련된 조문 고르기 — 결정론적, LLM 없음.

━━ 왜 필요한가 (2026-09-04 실측) ━━
법령 계층은 지금까지 `_law_context()`가 **trap_ids로 게이팅**돼 있었다.
즉 L2가 함정을 감지하고, 그 함정에 사람이 확인한 앵커가 등재돼 있을 때만
조문이 감사 페이로드에 실렸다. 298건 실측:

    함정 0건             147건 (49.0%)  → 법령 계층 미가동
    함정 있으나 앵커 0    18건 ( 6.0%)  → 법령 계층 미가동
    법령 판정 대상 존재  135건 (45.0%)

**질의의 55%에서 조문이 한 줄도 실리지 않았다.** 그 상태에서는 답변이
법령에 어긋나는 말을 해도 대조할 대상 자체가 없다.

이 모듈은 그 게이팅과 무관하게, **답변 본문에서** 도메인 핵심어를 뽑아
관련 조문을 고른다. 함정이 하나도 안 잡힌 질의에서도 조문이 실린다.

━━ 앵커를 대체하지 않는다 ━━
앵커(anchors.ANCHORS)는 사람이 조문 원문을 눈으로 확인해 등재한 것이고,
여기서 고르는 것은 기계가 용어 겹침으로 고른 후보다. 신뢰도가 다르므로
**앵커가 우선이고 이쪽이 보충**이다. 호출부가 앵커를 먼저 싣고 남는
자리를 이 모듈이 채운다.

━━ 왜 LLM을 쓰지 않는가 ━━
"판단은 코드, 문장은 LLM". 어느 조문을 볼지가 실행마다 달라지면 같은
질의가 매번 다른 검사를 받게 되고, 재현도 디버깅도 불가능해진다.
정렬까지 결정론적으로 고정한다(점수 동률은 조문 참조 문자열 순).

━━ 비용 ━━
색인은 저장소당 한 번만 만든다. 실수집본은 7천 건 규모라 최초 1회에
수백 ms~수 초가 들 수 있으므로, 서버 기동 시 warm()으로 미리 만든다.
요청 중에 처음 만들면 그 요청 하나가 예산을 넘길 수 있다.
"""

from __future__ import annotations

import logging
import math
import time
import unicodedata
from dataclasses import dataclass

from app.analysis.vocab import DOMAIN_TERMS, domain_hits, key_terms
from app.law.schema import LawArticle
from app.law.store import LawStore

log = logging.getLogger(__name__)

# 조문 하나가 후보가 되려면 이만큼의 서로 다른 도메인 용어가 겹쳐야 한다.
# 1개만 요구하면 '연금'만 스친 조문이 전부 후보가 된다.
MIN_TERM_OVERLAP = 2

# 이 모듈이 고를 조문 수의 기본 상한. 앵커와 합쳐 페이로드에 실리므로
# 넉넉히 잡으면 단일 GET의 시간 예산을 잠식한다.
DEFAULT_LIMIT = 4

# 색인 대상 용어 — 매칭 기준을 다른 계층과 어긋나게 두지 않기 위해
# 슬롯 매칭·인용이 쓰는 것과 **같은 사전**을 쓴다(CLAUDE.md의
# "인용 기준과 매칭 기준을 어긋나게 두지 말 것"과 같은 이유다).
_VOCAB: tuple[str, ...] = tuple(sorted(DOMAIN_TERMS))


@dataclass
class _Index:
    """용어 → 조문 색인과 문서빈도. 저장소 하나에 하나씩."""

    article_terms: list[frozenset[str]]
    df: dict[str, int]
    n_articles: int

    def idf(self, term: str) -> float:
        """흔한 용어일수록 가중치가 낮다.

        '인출'·'연금'은 거의 모든 조문에 있어 관련성의 근거가 못 된다.
        반대로 '연금수령연차'가 겹치면 그것만으로 강한 신호다.
        """
        return math.log(self.n_articles / (1 + self.df.get(term, 0)))


# 색인을 담아 둘 저장소 속성 이름.
#
# ⚠️ 모듈 수준 dict에 id(store)로 캐시하지 않는다. 파이썬은 객체가 회수되면
#    같은 id를 재사용하므로, 수명이 다른 저장소가 남의 색인을 물려받을 수
#    있다. 조문을 잘못 고르는 것은 조용한 실패라 눈에 띄지 않는다.
#    저장소 객체에 직접 달아 두면 수명이 정확히 일치한다.
_INDEX_ATTR = "_relevance_index"


def _resolve_store(store: LawStore | None) -> LawStore:
    """저장소를 지금 시점에 해석한다.

    ⚠️ 모듈 최상단에서 `from app.law.store import get_store` 로 이름을 묶어
       두면 안 된다. 그러면 이 모듈이 처음 import된 순간의 함수 객체가
       박히고, 나중에 저장소를 갈아 끼워도 반영되지 않는다. 실제로 그렇게
       썼다가 테스트가 **먼저 실행된 시험용 저장소를 끝까지 물고 있는**
       상태를 만들었다(2026-09-04, 배선 테스트가 잡아냄).
    """
    if store is not None:
        return store
    from app.law.store import get_store
    return get_store()


def _terms_in(text: str) -> frozenset[str]:
    """조문 원문에 실제로 등장하는 도메인 용어.

    ⚠️ 여기서는 토큰화 대신 부분문자열 포함을 쓴다. 법령 원문은 조사와
       괄호가 촘촘히 붙어("연금계좌에서", "연금수령한도(제40조의2)") 토큰
       경계가 한국어 형태소와 어긋나기 때문이다. 부분문자열의 오탐 위험은
       용어집이 도메인 전문어로 한정돼 있어 낮고, 남는 오탐은 idf 가중과
       MIN_TERM_OVERLAP이 걸러 준다.

       조문 선택은 **후보를 고르는 일**이지 판정이 아니라는 점도 중요하다.
       여기서 잘못 고른 조문은 감사자가 인용하지 못하고, 인용하지 못하면
       저촉 판정 자체가 성립하지 않는다. 즉 이 단계의 오탐은 조용히
       사라지지 최종 판정에 도달하지 않는다.
    """
    body = unicodedata.normalize("NFKC", text or "")
    return frozenset(t for t in _VOCAB if t in body)


def build_index(store: LawStore) -> _Index:
    """저장소 전체를 훑어 색인을 만든다. 저장소당 한 번."""
    t0 = time.perf_counter()
    arts = store.articles()
    article_terms = [_terms_in(a.text) for a in arts]
    df: dict[str, int] = {}
    for terms in article_terms:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    idx = _Index(article_terms=article_terms, df=df,
                 n_articles=max(1, len(arts)))
    log.info("법령 관련성 색인 %d건 구축 (%.0fms)",
             len(arts), (time.perf_counter() - t0) * 1000)
    return idx


def get_index(store: LawStore | None = None) -> _Index:
    store = _resolve_store(store)
    idx = getattr(store, _INDEX_ATTR, None)
    if idx is None:
        idx = build_index(store)
        setattr(store, _INDEX_ATTR, idx)
    return idx


def warm(store: LawStore | None = None) -> int:
    """색인을 미리 만들어 둔다. 서버 기동 시 부른다.

    반환값은 색인된 조문 수. 저장소가 비어 있으면 0이고, 그 경우
    아무 일도 하지 않는다(수집 전 상태는 정상이다).
    """
    store = _resolve_store(store)
    if store.is_empty:
        return 0
    return get_index(store).n_articles


def query_terms(answer: str, question: str = "") -> set[str]:
    """조문을 고를 때 쓸 용어 집합.

    답변을 우선한다 — 우리가 검사하려는 것은 **답변이 무엇을 주장했는가**다.
    질의는 답변이 짧거나 되묻기만 한 경우를 위한 보조다.

    ⚠️ **조문 쪽과 같은 방식으로 뽑는다**(_terms_in). 한쪽은 토큰화,
       다른 쪽은 부분문자열로 뽑으면 같은 판단을 두 기준으로 하게 되고
       반드시 어긋난다 — CLAUDE.md의 "인용 기준과 매칭 기준을 어긋나게 두지
       말 것"과 같은 함정이다. 실제로 처음 구현이 그랬고, 답변의 '연금소득'
       하나만 겹쳐 관련 조문을 통째로 놓쳤다.

       여기에 동의어 확장(key_terms)을 **더한다.** 사용자와 답변은 구어를
       쓰고 법령은 법문을 쓰기 때문이다("퇴직금" → "퇴직급여").
       확장으로 들어온 용어는 조문에 실제로 있을 때만 겹치므로 손해가 없다.
    """
    terms: set[str] = set()
    for text in (answer or "", question or ""):
        terms |= set(_terms_in(text))
        terms |= domain_hits(key_terms(text))
    return terms


def select_relevant_articles(answer: str,
                             question: str = "",
                             store: LawStore | None = None,
                             limit: int = DEFAULT_LIMIT,
                             exclude_refs: frozenset[str] | set[str] = frozenset(),
                             ) -> tuple[list[LawArticle], str]:
    """답변과 관련된 조문을 고른다. (조문 목록, 사유)

    exclude_refs : 이미 앵커로 실린 조문. 중복해서 싣지 않는다.

    ⚠️ 고른 조문은 **판정이 아니라 후보**다. 여기 실렸다고 해서 저촉이
       있다는 뜻이 아니며, 실제 판정은 인용 검증을 통과한 것만 채택된다.
    """
    store = _resolve_store(store)
    if store.is_empty:
        return [], "법령 수집본이 비어 있어 관련 조문을 고르지 못함"

    terms = query_terms(answer, question)
    if not terms:
        return [], "답변에서 도메인 용어를 찾지 못해 관련 조문 없음"

    idx = get_index(store)
    arts = store.articles()
    excluded = {str(r) for r in exclude_refs}

    scored: list[tuple[float, int, str, LawArticle]] = []
    for i, art in enumerate(arts):
        if art.ref in excluded:
            continue
        hit = terms & idx.article_terms[i]
        if len(hit) < MIN_TERM_OVERLAP:
            continue
        score = sum(idx.idf(t) for t in hit)
        # 동률은 조문 참조 문자열로 깬다 — 실행마다 순서가 바뀌면
        # 같은 질의가 매번 다른 조문으로 검사받는다.
        scored.append((-score, -len(hit), art.ref, art))

    if not scored:
        return [], (f"답변 용어 {sorted(terms)[:6]} 와 {MIN_TERM_OVERLAP}개 이상 "
                    f"겹치는 조문이 없음")

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    picked = [a for _s, _h, _r, a in scored[:limit]]
    return picked, (f"답변 용어 기준으로 관련 조문 {len(picked)}건 선택 "
                    f"(후보 {len(scored)}건 중)")
