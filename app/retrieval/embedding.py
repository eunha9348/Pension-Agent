"""임베딩 훅 — **격리 지점**.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 대회 제약 1: "LLM은 HyperCLOVA X만 사용". 임베딩·NLI 모델이 이 범위에
 포함되는지 **확인되지 않았다**. 확인 전까지 가장 보수적으로 —
 임베딩 없이 BM25 단독으로 동작한다.

 허용이 확인되면 **이 파일의 embed_texts() 하나만** 구현하고
 .env 의 USE_EMBEDDING=true 로 바꾸면 된다.
 임베딩에 의존하는 코드는 전부 이 파일 안에 있다.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

구현할 때 주의:
  · CLOVA Studio 임베딩 API를 쓴다면 HyperCLOVA X 계열이므로 제약을 만족할
    가능성이 높다. 외부 오픈소스 임베딩 모델은 확인 전까지 쓰지 말 것.
  · 인제스트에서 청크 벡터를 미리 만들어 두어야 한다(질의 시 계산하면 느리다).
    sql/schema.sql 의 chunks.embedding 컬럼이 그 자리다.
"""

from __future__ import annotations

from typing import Optional, Sequence

from app.config import get_settings


def embedding_enabled() -> bool:
    """임베딩 경로 사용 여부. 기본값 False(보수적 가정)."""
    return bool(get_settings().use_embedding)


def embed_texts(texts: Sequence[str]) -> Optional[list[list[float]]]:
    """텍스트 목록 → 벡터 목록.

    ★ 여기에 임베딩 호출을 구현하십시오 ★
    현재는 미구현 상태를 명시적으로 알리기 위해 None을 반환한다.
    None이면 상위 계층(hybrid.py)이 BM25 단독 경로로 자동 축퇴한다.
    """
    if not embedding_enabled():
        return None

    # ─────────────────────────────────────────────────────────
    # TODO(팀 확인 후 구현): CLOVA Studio 임베딩 엔드포인트 호출
    #   from app.llm.clova import get_client
    #   ...
    # ─────────────────────────────────────────────────────────
    return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
