"""임베딩 — 로컬 모델(기본) 또는 CLOVA Studio API.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 대회 제약(CLAUDE.md 1번)은 **답변을 만드는 LLM**에 관한 것이다.
 임베딩은 검색 순위용 벡터일 뿐 답변 문장을 만들지 않으므로 제약 밖이며,
 **타사 모델 사용이 확인 완료됐다(2026-08-19).**

 답변 문장을 만드는 L1·L5'·L6 세 호출만은 HyperCLOVA X 전용이다.
 거기에 타사 모델을 넣으면 실격이므로 절대 바꾸지 말 것.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

백엔드 두 가지 (EMBEDDING_BACKEND)

  local (기본)  sentence-transformers 로컬 모델
                · 호출 제한 없음 — 8천 청크를 배치로 몇 분에 끝낸다
                · API 키 불필요, 비용 없음, 결과 재현 가능
                · 첫 실행 때 모델을 내려받는다(수백 MB)

  clova         CLOVA Studio 임베딩 API (bge-m3)
                POST {endpoint} · body {"text": "..."} ← **한 번에 한 건**
                resp {"status": {...}, "result": {"embedding": [...]}}
                · 건당 1회 호출이라 8,195청크면 8,195회
                · 속도 제한(429)이 걸려 실측 2시간 이상, 중간에 자주 끊김
                → 그래서 기본값을 local로 두었다

어느 백엔드든 청크 벡터는 인제스트와 분리된 별도 단계에서 만들고
(app/ingest/build_embeddings.py) 본문 해시로 증분 갱신한다.
질의 벡터만 요청 시점에 계산한다.

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


def backend() -> str:
    return (get_settings().embedding_backend or "local").lower()


def embedding_enabled() -> bool:
    """임베딩 경로 사용 여부.

    clova 백엔드는 키가 있어야 하지만, local 백엔드는 키가 필요 없다.
    """
    s = get_settings()
    if not s.use_embedding:
        return False
    if backend() == "clova":
        return bool(s.clova_api_key)
    return True


# ── 로컬 백엔드 ──────────────────────────────────────────────

_LOCAL_MODEL = None


def _local_model():
    """모델을 프로세스당 1회만 적재한다(수백 MB — 매번 읽으면 안 된다).

    ⚠️ 스레드 수를 1로 고정한다. torch는 기본적으로 CPU 코어 수만큼 스레드를
    띄우는데, 스레드마다 별도 연산 버퍼를 할당해서 코어가 여럿이면 그만큼
    메모리를 더 먹는다. 메모리가 빠듯한 소형 인스턴스(예: EC2 t3.small,
    총 2GB)에서 실제로 이것 때문에 OOM(커널 강제종료, exit 137)이 났다.
    스레드를 1로 묶으면 느려지는 대신 메모리 사용이 훨씬 예측 가능해진다.
    """
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        import torch
        torch.set_num_threads(1)
        from sentence_transformers import SentenceTransformer
        name = get_settings().local_embedding_model
        log.info("[embedding] 로컬 모델 적재: %s", name)
        _LOCAL_MODEL = SentenceTransformer(name, device="cpu")
    return _LOCAL_MODEL


def _prep(texts: Sequence[str], is_query: bool) -> list[str]:
    """모델별 입력 규약을 맞춘다.

    e5 계열은 문서에 "passage: ", 질의에 "query: " 접두를 요구한다.
    이걸 빠뜨리면 검색 품질이 눈에 띄게 떨어지는데, 에러가 나지 않아
    알아채기 어렵다 — 그래서 코드에서 자동으로 붙인다.
    """
    name = (get_settings().local_embedding_model or "").lower()
    if "e5" not in name:
        return [t or "" for t in texts]
    prefix = "query: " if is_query else "passage: "
    return [prefix + (t or "") for t in texts]


def embed_local(texts: Sequence[str], *, is_query: bool = False,
                batch_size: int = 32) -> list[list[float]]:
    """로컬 모델로 한 번에 여러 건을 임베딩한다. 호출 제한이 없다."""
    model = _local_model()
    vecs = model.encode(_prep(texts, is_query), batch_size=batch_size,
                        show_progress_bar=False, normalize_embeddings=True)
    return [[float(x) for x in v] for v in vecs]


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

# 429를 겪었는지 알리는 신호. 대량 생성 도구(build_embeddings)가 이걸 보고
# 요청 간격을 스스로 늘린다 — 백오프로 넘겼더라도 한계에 근접했다는 뜻이므로,
# 다음 호출을 같은 속도로 쏘면 또 걸린다.
_RATE_LIMIT_SEEN = False


def rate_limit_seen(reset: bool = True) -> bool:
    """직전 호출에서 429를 겪었는가. 읽으면 기본적으로 표시를 지운다."""
    global _RATE_LIMIT_SEEN
    seen = _RATE_LIMIT_SEEN
    if reset:
        _RATE_LIMIT_SEEN = False
    return seen


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
                global _RATE_LIMIT_SEEN
                _RATE_LIMIT_SEEN = True
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


def embed_texts(texts: Sequence[str], *,
                is_query: bool = True) -> Optional[list[list[float]]]:
    """텍스트 목록 → 벡터 목록. 실패하면 None(=BM25 축퇴).

    질의 임베딩처럼 소수 건에 쓰는 경로다(그래서 is_query 기본값이 True).
    코퍼스 전체 임베딩은 app/ingest/build_embeddings.py 가 따로 처리한다.
    """
    if not embedding_enabled() or not texts:
        return None

    # 캐시 히트 확인 (질의는 반복되는 경우가 많다)
    keys = [(t or "").strip() for t in texts]
    if all(k in _QUERY_CACHE for k in keys):
        return [_QUERY_CACHE[k] for k in keys]

    try:
        if backend() == "clova":
            vecs = [_cached_clova(k) for k in keys]
        else:
            vecs = embed_local(keys, is_query=is_query)
    except EmbeddingError as e:
        log.warning("[embedding] 실패 → BM25 단독으로 축퇴: %s", e)
        return None
    except Exception as e:      # noqa: BLE001 — 모델 적재 실패 등
        log.warning("[embedding] 로컬 모델 사용 불가 → BM25 단독으로 축퇴: %s", e)
        return None

    for k, v in zip(keys, vecs):
        if len(_QUERY_CACHE) < _QUERY_CACHE_MAX:
            _QUERY_CACHE[k] = v
    return vecs


def _cached_clova(key: str) -> list[float]:
    if key in _QUERY_CACHE:
        return _QUERY_CACHE[key]
    return embed_one(key)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
