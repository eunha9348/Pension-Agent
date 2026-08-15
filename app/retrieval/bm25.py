"""BM25 검색 (순수 파이썬).

━━ 왜 직접 구현했는가 ━━
① 임베딩 모델을 쓸 수 있는지 확인되지 않아(대회 제약 1) 어휘 검색이
   1차 검색 수단이어야 한다. 여기가 막히면 검색 전체가 막힌다.
② rank_bm25 같은 패키지를 쓰면 의존성이 늘지만 로직은 30줄이다.
③ PostgreSQL tsvector로 전환할 때 이 클래스만 갈아끼우면 된다.

파라미터는 표준값(k1=1.5, b=0.75)을 쓴다.
TODO(팀 결정): 임계값·가중치 튜닝은 자체 평가셋 점수를 보고 정할 것.
               지금 임의로 정하면 근거 없는 숫자가 된다.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.retrieval.tokenize import index_terms

K1 = 1.5
B = 0.75


@dataclass
class BM25Index:
    """청크 텍스트에 대한 역색인."""
    doc_ids: list[str] = field(default_factory=list)          # chunk_id 목록
    lengths: list[int] = field(default_factory=list)
    postings: dict[str, dict[int, int]] = field(default_factory=dict)  # term → {idx: tf}
    avg_len: float = 0.0

    @property
    def size(self) -> int:
        return len(self.doc_ids)

    def add(self, key: str, text: str) -> None:
        idx = len(self.doc_ids)
        terms = index_terms(text)
        self.doc_ids.append(key)
        self.lengths.append(len(terms) or 1)
        for term, tf in Counter(terms).items():
            self.postings.setdefault(term, {})[idx] = tf

    def finalize(self) -> None:
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

    def search(self, query: str, top_k: int = 10,
               allowed: Optional[set[str]] = None) -> list[tuple[str, float]]:
        """BM25 점수 상위 top_k. 반환: [(key, score)]

        allowed: 지정하면 그 key들만 대상으로 한다(메타데이터 선필터용).
        """
        if not self.doc_ids:
            return []
        if self.avg_len <= 0:
            self.finalize()

        q_terms = index_terms(query)
        if not q_terms:
            return []

        n = len(self.doc_ids)
        scores: dict[int, float] = {}
        for term in set(q_terms):
            posting = self.postings.get(term)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for idx, tf in posting.items():
                dl = self.lengths[idx]
                denom = tf + K1 * (1 - B + B * dl / self.avg_len)
                scores[idx] = scores.get(idx, 0.0) + idf * (tf * (K1 + 1)) / denom

        items = [(self.doc_ids[i], s) for i, s in scores.items()]
        if allowed is not None:
            items = [(k, s) for k, s in items if k in allowed]
        items.sort(key=lambda x: (-x[1], x[0]))
        return items[:top_k]

    # ── 직렬화 ───────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "doc_ids": self.doc_ids,
            "lengths": self.lengths,
            "avg_len": self.avg_len,
            # JSON은 int 키를 못 쓰므로 문자열로 저장
            "postings": {t: {str(i): tf for i, tf in p.items()}
                         for t, p in self.postings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Index":
        idx = cls(
            doc_ids=list(data.get("doc_ids", [])),
            lengths=list(data.get("lengths", [])),
            avg_len=float(data.get("avg_len", 0.0)),
            postings={t: {int(i): tf for i, tf in p.items()}
                      for t, p in (data.get("postings") or {}).items()},
        )
        if idx.avg_len <= 0:
            idx.finalize()
        return idx


def build_index(items: Iterable[tuple[str, str]]) -> BM25Index:
    """(key, text) 목록으로 색인 생성."""
    idx = BM25Index()
    for key, text in items:
        idx.add(key, text)
    idx.finalize()
    return idx


def normalize_scores(hits: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """BM25 원점수를 0~1로 정규화.

    filter_irrelevant_evidence()가 score_threshold=0.35로 자르는데,
    BM25 원점수는 상한이 없어 그대로 쓰면 임계값이 의미를 잃는다.
    최고점 대비 상대 점수로 바꿔 '상대적으로 얼마나 관련 있는가'로 해석한다.
    """
    if not hits:
        return []
    top = max(s for _, s in hits) or 1.0
    return [(k, round(s / top, 6)) for k, s in hits]
