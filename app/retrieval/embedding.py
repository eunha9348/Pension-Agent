"""임베딩 — CLOVA Studio 임베딩 API (bge-m3).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 대회 제약 1: "LLM은 HyperCLOVA X만 사용".
 임베딩 모델 사용 가능 여부를 주최측에 확인했고 **허용**을 받았다.
 다만 **CLOVA Studio 임베딩만** 쓴다 — 네이버 클라우드가 제공하는 모델이라
 제약 안에 확실히 들어온다. 외부 오픈소스 임베딩(sentence-transformers 등)은
 허용 여부가 다시 불확실해지므로 도입하지 않는다.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

API 규격
  POST {endpoint}/{model}
    headers: Authorization: Bearer <키> · X-NCP-CLOVASTUDIO-REQUEST-ID
    body   : {"text": "..."}                ← **한 번에 한 건**
    resp   : {"status": {...}, "result": {"embedding": [...]}}

호출이 건당 1회라는 점이 설계를 지배한다. 코퍼스 8,195청크면 8,195회다.
그래서 청크 벡터는 인제스트와 분리된 별도 단계에서 한 번만 만들고
(app/ingest/build_embeddings.py), 본문 해시로 증분 갱신한다.
질의 벡터만 요청 시점에 1회 계산한다.

실패는 삼키지 않되 서비스를 죽이지도 않는다 — 벡터를 못 얻으면
BM25 단독으로 축퇴한다. 검색이 조금 나빠지는 것이 답을 못 하는 것보다 낫다.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from app.config import get_settings

log = logging.getLogger("embedding")

# 질의 임베딩 캐시 — 같은 질의가 반복되면 호출을 아낀다.
# 평가는 세션이 없으므로 프로세스 생존 기간 동안만 유효하면 충분하다.
_QUERY_CACHE: dict[str, list[float]] = {}
_QUERY_CACHE_MAX = 512


class EmbeddingError(RuntimeError):
    """임베딩 호출 실패. 상위에서 BM25 축퇴를 판단한다."""


def embedding_enabled() -> bool:
    """임베딩 경로 사용 여부."""
    s = get_settings()
    return bool(s.use_embedding and s.clova_api_key)


def _headers() -> dict[str, str]:
    """인증 방식은 채팅 API와 같다 — 키 접두사로 신형/구형을 가른다.

    (clova.py의 _headers()와 같은 규칙이다. 두 곳이 어긋나면 채팅은 되는데
     임베딩만 401이 나는 혼란스러운 상황이 되므로, 규칙을 바꿀 때는
     반드시 두 파일을 함께 고칠 것.)
    """
    s = get_settings()
    key = s.clova_api_key
    if key.startswith("nv-"):
        h = {"Authorization": f"Bearer {key}"}
    else:
        h = {"X-NCP-CLOVASTUDIO-API-KEY": key}
        if s.clova_apigw_key:
            h["X-NCP-APIGW-API-KEY"] = s.clova_apigw_key
    h["Content-Type"] = "application/json"
    h["Accept"] = "application/json"
    if s.clova_request_id:
        h["X-NCP-CLOVASTUDIO-REQUEST-ID"] = s.clova_request_id
    return h


class RateLimitError(EmbeddingError):
    """429 — 인증·데이터 문제가 아니라 호출 속도 문제. 회복 가능하다."""


# 429는 재시도해도 0.4초 뒤엔 또 걸린다 — 실제로 발생한 사고다
# (build_embeddings 8,195건 중 대부분이 연속 429로 실패했다). 지수 백오프로
# 충분히 쉬어야 풀린다. 초당 호출 제한이 명시돼 있지 않아 보수적으로 잡는다.
_RATE_LIMIT_BACKOFF = (3.0, 6.0, 12.0, 20.0, 20.0)   # 초 단위, 재시도 순서대로


def embed_one(text: str, *, timeout: Optional[float] = None,
              max_retry: int = 1) -> list[float]:
    """텍스트 1건 → 벡터. 실패하면 EmbeddingError를 올린다.

    429(Too Many Requests)는 max_retry와 별도로, 그 자체 재시도 예산
    (_RATE_LIMIT_BACKOFF)을 쓴다. 인증 실패처럼 다시 시도해도 똑같이 실패할
    오류가 아니라, 충분히 쉬면 회복되는 오류이기 때문이다.
    """
    import httpx

    s = get_settings()
    endpoint = s.clova_embedding_endpoint
    if not endpoint:
        raise EmbeddingError("CLOVA_EMBEDDING_ENDPOINT 가 비어 있습니다.")

    body = {"text": (text or "").strip()}
    if not body["text"]:
        raise EmbeddingError("빈 텍스트는 임베딩할 수 없습니다.")

    last: Optional[Exception] = None
    rate_limit_attempt = 0
    attempt = 0
    while attempt <= max_retry:
        try:
            with httpx.Client(timeout=timeout or s.clova_timeout_sec) as c:
                resp = c.post(endpoint, headers=_headers(), json=body)

            if resp.status_code == 429:
                if rate_limit_attempt >= len(_RATE_LIMIT_BACKOFF):
                    raise RateLimitError(
                        f"HTTP 429 — {len(_RATE_LIMIT_BACKOFF)}번 쉬어도 여전히 "
                        f"속도 제한: {resp.text[:150]}")
                wait = _RATE_LIMIT_BACKOFF[rate_limit_attempt]
                log.warning("[embedding] 429 — %.0f초 대기 후 재시도 (%d/%d)",
                           wait, rate_limit_attempt + 1, len(_RATE_LIMIT_BACKOFF))
                time.sleep(wait)
                rate_limit_attempt += 1
                continue      # attempt는 그대로 — 일반 재시도 예산을 깎지 않는다

            if resp.status_code != 200:
                raise EmbeddingError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            code = str((data.get("status") or {}).get("code", ""))
            if code and not code.startswith("2"):
                raise EmbeddingError(
                    f"CLOVA status {code}: {(data.get('status') or {}).get('message')}")
            vec = ((data.get("result") or {}).get("embedding")
                   or (data.get("result") or {}).get("embeddings"))
            if not vec:
                raise EmbeddingError(f"응답에 embedding이 없습니다: {str(data)[:200]}")
            return [float(v) for v in vec]
        except RateLimitError:
            raise
        except Exception as e:      # noqa: BLE001 — 재시도 후 그대로 올린다
            last = e
            attempt += 1
            if attempt <= max_retry:
                time.sleep(0.4 * attempt)
    raise EmbeddingError(str(last))


def embed_texts(texts: Sequence[str]) -> Optional[list[list[float]]]:
    """텍스트 목록 → 벡터 목록. 하나라도 실패하면 None(=BM25 축퇴).

    질의 임베딩처럼 소수 건에 쓰는 경로다. 코퍼스 전체 임베딩은
    app/ingest/build_embeddings.py 가 진행 상황을 보여주며 따로 처리한다.
    """
    if not embedding_enabled() or not texts:
        return None

    out: list[list[float]] = []
    for t in texts:
        key = (t or "").strip()
        if key in _QUERY_CACHE:
            out.append(_QUERY_CACHE[key])
            continue
        try:
            vec = embed_one(key)
        except EmbeddingError as e:
            log.warning("[embedding] 실패 → BM25 단독으로 축퇴: %s", e)
            return None
        if len(_QUERY_CACHE) < _QUERY_CACHE_MAX:
            _QUERY_CACHE[key] = vec
        out.append(vec)
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
