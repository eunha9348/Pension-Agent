"""환경 설정 로더.

외부 의존(python-dotenv 등) 없이 .env를 직접 읽는다.
설정값 해석 규칙을 한 곳에 모아, 각 모듈이 os.environ을 직접 뒤지지 않게 한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: str | Path = None) -> None:
    """.env 파일을 os.environ에 로드. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    p = Path(path) if path else REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    clova_api_key: str = ""
    clova_apigw_key: str = ""
    clova_request_id: str = ""
    clova_endpoint: str = ""
    llm_mode: str = "auto"           # auto | real | mock
    clova_timeout_sec: float = 15.0
    clova_max_retry: int = 1
    database_url: str = ""
    index_path: str = "data/index"
    use_embedding: bool = False
    # 임베딩 백엔드: "local"(기본) | "clova"
    #
    # 타사 임베딩 모델 사용은 2026-08-19 확인 완료(CLAUDE.md 제약 1 참고).
    # 검색 순위용 벡터일 뿐 답변 문장을 만들지 않으므로 제약 밖이다.
    # local을 기본으로 두는 이유는 단순하다 — CLOVA 임베딩은 건당 1회 호출에
    # 속도 제한이 걸려 8천 청크에 2시간 넘게 걸리고 429로 자주 끊긴다.
    # 로컬 모델은 한 번 내려받으면 제한 없이 배치로 몇 분 만에 끝난다.
    embedding_backend: str = "local"
    local_embedding_model: str = "intfloat/multilingual-e5-small"
    clova_embedding_endpoint: str = ""
    embedding_weight: float = 0.5    # RRF 융합 시 벡터 순위 가중치

    @property
    def llm_is_mock(self) -> bool:
        """실제 CLOVA 호출을 하지 못하는 상태인지.

        auto 모드에서 키가 없으면 mock으로 떨어진다 —
        키를 넣는 즉시 코드 수정 없이 실제 호출로 전환된다.
        """
        if self.llm_mode == "mock":
            return True
        if self.llm_mode == "real":
            return False
        return not bool(self.clova_api_key)


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        clova_api_key=os.environ.get("CLOVA_API_KEY", "").strip(),
        clova_apigw_key=os.environ.get("CLOVA_APIGW_KEY", "").strip(),
        clova_request_id=os.environ.get("CLOVA_REQUEST_ID", "").strip(),
        clova_endpoint=os.environ.get(
            "CLOVA_ENDPOINT",
            "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005",
        ).strip(),
        llm_mode=os.environ.get("LLM_MODE", "auto").strip().lower(),
        clova_timeout_sec=_float("CLOVA_TIMEOUT_SEC", 15.0),
        clova_max_retry=int(_float("CLOVA_MAX_RETRY", 1)),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        index_path=os.environ.get("INDEX_PATH", "data/index").strip(),
        use_embedding=_bool("USE_EMBEDDING", False),
        embedding_backend=os.environ.get(
            "EMBEDDING_BACKEND", "local").strip().lower(),
        local_embedding_model=os.environ.get(
            "LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small").strip(),
        clova_embedding_endpoint=os.environ.get(
            "CLOVA_EMBEDDING_ENDPOINT",
            "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2",
        ).strip(),
        embedding_weight=_float("EMBEDDING_WEIGHT", 0.5),
    )


SETTINGS = get_settings()
