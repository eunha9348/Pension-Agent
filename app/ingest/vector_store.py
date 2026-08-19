"""청크 임베딩 영속 저장소.

━━ 왜 별도 파일인가 ━━
CLOVA 임베딩 API는 텍스트 1건당 1회 호출이다. 코퍼스 8,195청크면
호출도 8,195회다. 인덱스를 다시 만들 때마다 이걸 반복하면 시간도 크레딧도
감당이 안 된다. 그래서 벡터는 chunks.json과 분리해 따로 두고,
**청크 본문 해시가 같으면 재사용**한다(증분 임베딩).

━━ 왜 JSON이 아닌가 ━━
8,195 × 1024차원 float를 JSON으로 쓰면 70MB를 넘고 로딩도 느리다.
stdlib `array`로 float32 이진 저장하면 33MB에 로딩이 즉시 끝난다.
numpy를 새로 넣지 않기 위해 이 방식을 골랐다(requirements 최소 유지).

파일 구성
  vectors.bin   float32 벡터를 순서대로 이어붙인 것
  vectors.json  {model, dim, order[], hashes{}}  — bin의 해석 방법
"""

from __future__ import annotations

import hashlib
import json
import math
from array import array
from pathlib import Path
from typing import Iterable, Optional, Sequence

BIN_NAME = "vectors.bin"
META_NAME = "vectors.json"


def text_hash(text: str) -> str:
    """청크 본문 해시 — 문서가 안 바뀌었으면 임베딩을 재사용하기 위한 키."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


class VectorStore:
    """chunk_id → 벡터. 조회는 메모리, 저장은 이진 파일."""

    def __init__(self, model: str = "", dim: int = 0):
        self.model = model
        self.dim = dim
        self._order: list[str] = []
        self._index: dict[str, int] = {}
        self._data: array = array("f")
        self.hashes: dict[str, str] = {}
        # 코사인 계산을 매번 다시 하지 않도록 노름을 캐시한다
        self._norms: dict[str, float] = {}

    # ── 기본 조작 ────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, chunk_id: str) -> bool:
        return chunk_id in self._index

    def get(self, chunk_id: str) -> Optional[Sequence[float]]:
        pos = self._index.get(chunk_id)
        if pos is None:
            return None
        return self._data[pos * self.dim:(pos + 1) * self.dim]

    def add(self, chunk_id: str, vector: Sequence[float], h: str = "") -> None:
        if not self.dim:
            self.dim = len(vector)
        if len(vector) != self.dim:
            raise ValueError(
                f"임베딩 차원 불일치: {chunk_id} 는 {len(vector)}차원인데 "
                f"저장소는 {self.dim}차원이다. 모델을 바꿨다면 "
                f"벡터 파일을 지우고 다시 만들어야 한다.")
        if chunk_id in self._index:
            pos = self._index[chunk_id]
            self._data[pos * self.dim:(pos + 1) * self.dim] = array("f", vector)
        else:
            self._index[chunk_id] = len(self._order)
            self._order.append(chunk_id)
            self._data.extend(array("f", vector))
        if h:
            self.hashes[chunk_id] = h
        self._norms.pop(chunk_id, None)

    def needs_embedding(self, chunk_id: str, text: str) -> bool:
        """이 청크를 다시 임베딩해야 하는가 (신규이거나 본문이 바뀐 경우)."""
        if chunk_id not in self._index:
            return True
        return self.hashes.get(chunk_id) != text_hash(text)

    def prune(self, keep: Iterable[str]) -> int:
        """인덱스에 더 이상 없는 청크의 벡터를 버린다. 반환값은 버린 개수."""
        keep_set = set(keep)
        stale = [c for c in self._order if c not in keep_set]
        if not stale:
            return 0
        rebuilt = VectorStore(self.model, self.dim)
        for cid in self._order:
            if cid in keep_set:
                rebuilt.add(cid, self.get(cid) or [], self.hashes.get(cid, ""))
        self._order, self._index = rebuilt._order, rebuilt._index
        self._data, self.hashes = rebuilt._data, rebuilt.hashes
        self._norms.clear()
        return len(stale)

    # ── 유사도 ───────────────────────────────────────────────
    def _norm(self, chunk_id: str) -> float:
        if chunk_id not in self._norms:
            vec = self.get(chunk_id) or []
            self._norms[chunk_id] = math.sqrt(sum(v * v for v in vec)) or 1.0
        return self._norms[chunk_id]

    def rank(self, query_vec: Sequence[float],
             allowed: Optional[set[str]] = None,
             top_k: int = 50) -> list[tuple[str, float]]:
        """질의 벡터와의 코사인 유사도 상위 top_k."""
        if not query_vec or not self.dim:
            return []
        qn = math.sqrt(sum(v * v for v in query_vec)) or 1.0
        d = self.dim
        out: list[tuple[str, float]] = []
        for cid, pos in self._index.items():
            if allowed is not None and cid not in allowed:
                continue
            base = pos * d
            dot = 0.0
            for i in range(d):
                dot += self._data[base + i] * query_vec[i]
            out.append((cid, dot / (qn * self._norm(cid))))
        out.sort(key=lambda kv: -kv[1])
        return out[:top_k]

    # ── 저장 / 로드 ──────────────────────────────────────────
    def save(self, index_dir: str | Path) -> Path:
        d = Path(index_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / BIN_NAME, "wb") as f:
            self._data.tofile(f)
        (d / META_NAME).write_text(json.dumps({
            "model": self.model,
            "dim": self.dim,
            "order": self._order,
            "hashes": self.hashes,
        }, ensure_ascii=False), encoding="utf-8")
        return d / META_NAME

    @classmethod
    def load(cls, index_dir: str | Path) -> "VectorStore":
        """없거나 깨졌으면 빈 저장소를 준다 — 검색은 BM25로 계속 돌아야 한다."""
        d = Path(index_dir)
        meta_p, bin_p = d / META_NAME, d / BIN_NAME
        if not meta_p.exists() or not bin_p.exists():
            return cls()
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            store = cls(meta.get("model", ""), int(meta.get("dim") or 0))
            store._order = list(meta.get("order") or [])
            store.hashes = dict(meta.get("hashes") or {})
            store._index = {c: i for i, c in enumerate(store._order)}
            data = array("f")
            with open(bin_p, "rb") as f:
                data.fromfile(f, len(store._order) * store.dim)
            store._data = data
            return store
        except (ValueError, OSError, EOFError, json.JSONDecodeError):
            # 손상된 벡터 파일 때문에 서비스가 죽으면 안 된다.
            # 벡터 없이 BM25로 도는 편이 낫다.
            return cls()
