"""평가용 API 서버.

    GET /answer?question_id={id}&question={질의}
    GET /health

━━ 절대 규칙 ━━
어떤 오류가 나도 **5필드 JSON을 200으로 반환한다.**
평가는 세션 없는 단일 GET 요청이라, 500을 던지면 그 문항은 그대로 0점이다.
스택트레이스를 노출하는 것도 안 된다 — 대신 think_trace에 사유를 남긴다.

━━ 개인정보 ━━
질의 내용을 파일이나 DB에 저장하지 않는다. 로그에는 question_id와
처리 시간, 판정 결과만 남긴다.
"""

from __future__ import annotations

import logging
import time
import traceback

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import SETTINGS
from app.ingest.store import get_store
from app.llm.clova import USAGE, get_client
from app.pipeline import answer_question, health_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("pension-agent")

REQUIRED_FIELDS = ("question_id", "question", "retrieved_context",
                   "think_trace", "answer")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    yield


app = FastAPI(
    title="연금 Agent",
    description="제10회 미래에셋증권 AI Festival · 연금 Agent 트랙",
    version="0.1.0",
    lifespan=lifespan,
)


def _startup() -> None:
    """기동 시 인덱스·LLM 상태를 로그로 남긴다.

    mock으로 돌고 있는 걸 모르고 제출하는 사고를 막기 위해
    경고를 눈에 띄게 찍는다.
    """
    store = get_store(reload=True)
    client = get_client(force_reload=True)

    log.info("검색 인덱스: 문서 %d건 · 청크 %d건 (코퍼스: %s)",
             len(store.docs), len(store.chunks), store.corpus_kind)
    if store.is_empty:
        log.error("★ 인덱스가 비어 있습니다 — `python -m app.ingest.build_index` 실행 필요")
    elif store.corpus_kind == "mock":
        log.warning("★" * 30)
        log.warning("★ mock 코퍼스로 동작 중입니다 — 실제 제공 문서가 아닙니다")
        log.warning("★ data/corpus/ 에 실물 zip을 넣고 재빌드하십시오")
        log.warning("★" * 30)

    if getattr(client, "is_mock", False):
        log.warning("★" * 30)
        log.warning("★ CLOVA Studio 실연동 없이 mock 모드로 동작 중입니다")
        log.warning("★ .env 의 CLOVA_API_KEY 를 채우면 자동으로 실호출로 전환됩니다")
        log.warning("★" * 30)


def _ensure_schema(payload: dict, question_id: str, question: str) -> dict:
    """5필드가 모두 채워졌는지 최종 확인. 비면 채워 넣는다."""
    out = dict(payload or {})
    out["question_id"] = out.get("question_id") or question_id
    out["question"] = out.get("question") or question
    if not out.get("retrieved_context"):
        out["retrieved_context"] = "근거 문서 없음 — 제공 자료에서 관련 문서를 찾지 못했습니다."
    if not out.get("think_trace"):
        out["think_trace"] = "처리 과정 기록이 남지 않았습니다."
    if not out.get("answer"):
        out["answer"] = "죄송합니다. 답변을 생성하지 못했습니다."
    return {k: out[k] for k in REQUIRED_FIELDS}


@app.get("/answer")
def answer(question_id: str = Query(..., description="평가 문항 ID"),
           question: str = Query(..., description="자연어 질의")) -> JSONResponse:
    t0 = time.time()
    try:
        payload = answer_question(question_id, question)
        result = _ensure_schema(payload, question_id, question)
        log.info("[%s] 처리 완료 %.0fms (근거 %d자)", question_id,
                 (time.time() - t0) * 1000, len(result["retrieved_context"]))
        return JSONResponse(content=result)

    except Exception as e:                      # noqa: BLE001
        # 여기까지 온 예외는 버그다. 그래도 스키마는 지킨다.
        detail = traceback.format_exc(limit=3)
        log.exception("[%s] 처리 실패", question_id)
        return JSONResponse(content=_ensure_schema({
            "retrieved_context": "근거 문서 없음 — 처리 중 오류가 발생해 "
                                 "검색 결과를 확정하지 못했습니다.",
            "think_trace": (f"처리 중 예외가 발생했습니다: {type(e).__name__}: {e}\n"
                            f"답변을 생성하지 않고 한계를 고지합니다.\n"
                            f"(내부 추적: {detail.splitlines()[-1] if detail else 'N/A'})"),
            "answer": "죄송합니다. 이 질의를 처리하는 중 오류가 발생해 "
                      "정확한 답변을 드리지 못했습니다. "
                      "질문을 조금 더 구체적으로 작성해 다시 문의해 주세요.",
        }, question_id, question))


_UI_FILE = Path(__file__).parent / "web" / "chat.html"


@app.get("/ui", response_class=HTMLResponse)
def ui() -> HTMLResponse:
    """사람이 직접 써 보는 화면. 평가와는 무관한 부가 경로다.

    ⚠️ /answer 는 건드리지 않는다 — 평가 규격은 변경 불가다.
       이 화면도 결국 같은 GET /answer 를 부르므로, 여기서 보이는 것이
       채점자가 받는 것과 정확히 같다. 별도 경로로 우회하지 말 것.
    """
    try:
        return HTMLResponse(_UI_FILE.read_text(encoding="utf-8"))
    except OSError as e:
        log.error("UI 파일을 읽지 못했습니다: %s", e)
        return HTMLResponse(
            "<h1>UI 파일을 찾을 수 없습니다</h1>"
            f"<p>{_UI_FILE} 이 이미지에 포함됐는지 확인하십시오.</p>",
            status_code=500)


@app.get("/health")
def health() -> dict:
    info = health_info()
    info["llm_usage"] = USAGE.as_dict()
    info["index_ready"] = not get_store().is_empty
    return info


def _law_status() -> dict:
    """법령 접지 계층의 현재 상태.

    ━━ 왜 노출하는가 ━━
    법령은 **내부 검증 전용**이라 답변 본문에도 retrieved_context에도
    나타나지 않는다. 그래서 배포한 뒤 "반영이 됐는지"를 눈으로 확인할
    방법이 없었다. 실제로 그 질문을 받았고, 추측으로 답할 수밖에 없었다.
    수집본이 붙었는지·앵커가 몇 개인지를 여기서 바로 보게 한다.
    """
    try:
        from app.law.anchors import ANCHORS
        from app.law.store import get_store as get_law_store

        store = get_law_store()
        return {
            "articles": len(store),
            "laws": store.law_names,
            "anchored_traps": sorted(ANCHORS),
            "anchor_refs": sum(len(v) for v in ANCHORS.values()),
            "active": (not store.is_empty) and bool(ANCHORS),
        }
    except Exception as e:                                   # noqa: BLE001
        log.warning("법령 상태 조회 실패: %s", e)
        return {"active": False, "error": str(e)}


@app.get("/")
def root() -> dict:
    return {
        "service": "연금 Agent",
        "endpoints": ["/answer?question_id=&question=", "/health", "/ui"],
        "llm_mock": bool(getattr(get_client(), "is_mock", False)),
        "corpus_kind": get_store().corpus_kind,
        "law": _law_status(),
        "note": "설정은 .env 참고. CLOVA_API_KEY 를 채우면 실연동으로 전환됩니다.",
    }
