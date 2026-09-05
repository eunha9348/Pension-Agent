"""상품 팩트가 **공개 진입점을 통해** 실제로 답변 생성까지 도달하는가.

━━ 왜 별도 파일인가 (2026-09-05 실측으로 드러난 사각지대) ━━
CLAUDE.md: "배선을 검사하는 테스트는 배선을 지나가야 한다. 부품을 직접
이어 붙여 흉내낸 테스트는 호출자가 결과를 안 넘겨도 통과한다."

`test_product_facts.py`는 링크 셋 중 둘만 본다:
  ① 색인 시점 추출 → doc_meta        (test_색인_메타데이터에_팩트가_실린다)
  ② _product_facts → 프롬프트 블록    (test_L5_프롬프트에_팩트_블록이_실린다 등)

그런데 ②의 테스트들은 `spec["_product_facts"]`를 **손으로 만들어 넣는다.**
즉 가운데 링크 — `pipeline.py`가 `collect_facts()`를 실제로 불러
`query_spec["_product_facts"]`에 넣는 부분 — 은 어느 테스트도 지나가지
않는다. 그 코드를 통째로 지워도 기존 상품 팩트 테스트 50여 건이 전부
통과하고, 위험등급·수익률·총보수가 답변에서 조용히 사라진다.
법령 계층이 죽은 채 배포됐는데 관련 테스트 16건이 전부 통과했던 것과
같은 사고 유형이다.

이 파일은 `answer_question()`(= GET /answer가 부르는 그 함수)으로 들어가,
생성기에 실제로 전달된 페이로드에 팩트 블록이 있는지 본다. 중간을 손으로
이어 붙이지 않는다.
"""

from __future__ import annotations

import pytest

from app.ingest.store import ChunkRecord, DocumentStore, get_store, set_store
from app.retrieval.bm25 import build_index as build_bm25

# 실물 투자설명서에서 확인된 형태(위험등급 자기분류 + 총보수표 머리말)를 축약했다.
_DOC_TEXT = (
    "미래에셋퇴직연금케이인덱스25증권자투자신탁 투자설명서\n"
    "【투자위험등급】\n"
    "4등급(보통 위험)\n"
    "이 투자신탁은 위험도를 종합적으로 고려하여 4등급으로 분류하였습니다.\n"
    "【수수료 및 총보수】\n"
    "클래스 운용보수 판매보수 총보수\n"
    "C-P2 0.180 0.150 0.395\n"
    "연금저축 계좌에서 가입할 수 있는 상품입니다.\n"
)

_QUESTION = "이 퇴직연금 펀드 위험등급이랑 총보수 알려주세요"


def _store_with_facts() -> DocumentStore:
    """실제 인제스트와 같은 경로로 doc_meta를 만든다.

    ⚠️ product_facts를 손으로 써넣지 않는다 — 그러면 색인 시점 추출이
       망가져도 이 테스트가 통과해 버린다. build_doc_metadata()를 그대로
       불러 실제 추출기를 지나가게 한다.
    """
    from app.ingest.chunker import chunk_document
    from app.ingest.metadata import build_doc_metadata
    from app.ingest.zip_parser import Page, ParsedDocument

    doc = ParsedDocument(doc_id="fundA", source_path="fundA.pdf",
                         pages=[Page(1, _DOC_TEXT)])
    chunks = chunk_document(doc)
    meta = build_doc_metadata(doc, chunks)

    store = DocumentStore(corpus_kind="real")
    store.docs["fundA"] = meta
    for c in chunks:
        store.chunks[c.chunk_id] = ChunkRecord.from_dict(c.as_dict())
    store.bm25 = build_bm25((c.chunk_id, c.text) for c in store.chunks.values())
    return store


@pytest.fixture
def _facts_store():
    original = get_store()
    store = _store_with_facts()
    set_store(store)
    yield store
    set_store(original)


class _SpyClient:
    """생성기에 실제로 전달된 페이로드를 붙잡아 둔다."""

    is_mock = False

    def __init__(self):
        self.payloads: dict[str, str] = {}

    def call(self, system, user, purpose="?", **kw):
        self.payloads[purpose] = user
        if "감사자" in system:
            return '{"verdict":"APPROVE","findings":[]}'
        return "위험등급은 4등급(보통 위험)이고 총보수는 원문 표를 확인해 주세요."

    def call_with_functions(self, s, u, t, purpose="?", **kw):
        return {"name": None, "arguments": None, "raw": ""}


def test_색인_추출이_먼저_동작하는지_확인한다(_facts_store):
    """전제 확인 — 이 단계가 깨져 있으면 아래 배선 테스트의 실패가
    '배선 문제'인지 '추출 문제'인지 구별할 수 없다."""
    facts = _facts_store.doc_meta("fundA").get("product_facts")
    assert facts, "build_doc_metadata가 product_facts를 넣지 못했다"
    assert facts.get("risk_grade"), f"위험등급이 안 뽑혔다: {facts}"


def test_파이프라인이_팩트를_수집해_생성기까지_보낸다(_facts_store):
    """★ 이 파일의 핵심 — 가운데 링크(pipeline → collect_facts → query_spec).

    pipeline.py의 상품 팩트 수집 블록을 지우면 여기서만 실패한다.
    """
    from app.pipeline import answer_question

    c = _SpyClient()
    result = answer_question("PF-1", _QUESTION, client=c)

    generated = [p for k, p in c.payloads.items()
                 if k in ("l5_supervisor", "l4sub_advisory")]
    assert generated, f"생성기가 호출되지 않았다: {list(c.payloads)}"
    assert any("[상품 팩트" in p for p in generated), (
        "생성 페이로드에 상품 팩트 블록이 없다 — pipeline이 collect_facts를 "
        "부르지 않았거나 query_spec에 싣지 않았다")
    assert any("4등급" in p for p in generated), "위험등급 값이 전달되지 않았다"
    # 사용자에게 나가는 기록에도 수집 사실이 남아야 한다
    assert "L4_상품팩트" in result["think_trace"]


def test_색인에만_있고_근거에_없는_문서의_팩트는_수집되지_않는다(_facts_store):
    """`collect_facts`는 evidence의 doc_id로만 조회한다.

    색인에 팩트가 있어도 검색이 고르지 않은 문서의 수치를 답변에 쓰면
    근거 없는 인용이다. 여기서는 링크 ②의 **입력 집합**만 본다 —
    검색 결과가 아닌 문서가 섞여 들어오면 실패한다.
    """
    from app.analysis.product_facts import collect_facts

    facts = collect_facts([], _facts_store.doc_meta)
    assert facts == [], "근거가 비었는데 팩트가 수집됐다"

    facts = collect_facts(["fundA"], _facts_store.doc_meta)
    assert [f["doc_id"] for f in facts] == ["fundA"]


def test_답변이_반영한_팩트의_근거문서는_retrieved_context에_실린다(_facts_store):
    """★ 팩트가 답변을 형성했으면 그 문서는 반드시 인용돼야 한다.

    팩트 블록은 슬롯 매핑을 거치지 않고 생성 프롬프트에 실린다. 인용이
    `_used_evidence`(슬롯 매핑 결과)로만 만들어지던 시절에는, 슬롯이
    하나도 안 붙은 질의에서 **답변에는 4등급이 있는데 retrieved_context는
    "근거 문서 없음"** 인 상태가 만들어졌다(2026-09-05 확인).
    평가 API 스펙이 retrieved_context를 "답변 생성에 참고한 검색 문서"로
    정의하므로 그건 스펙 위반이다.
    """
    from app.pipeline import answer_question

    c = _SpyClient()
    # 슬롯 매핑이 성립하지 않도록 상품 팩트와 무관한 제도 질의를 쓴다.
    r = answer_question("PF-2", "국민연금 수령 개시 연령이 어떻게 되나요", client=c)

    generated = [p for k, p in c.payloads.items()
                 if k in ("l5_supervisor", "l4sub_advisory")]
    fed_facts = any("[상품 팩트" in p for p in generated)
    if not fed_facts:
        pytest.skip("이 질의에서는 팩트 블록이 생성기로 가지 않았다")

    # 스파이 생성기가 "4등급(보통 위험)"을 답변에 넣으므로, 그 근거 문서인
    # fundA가 retrieved_context에 있어야 한다.
    assert "4등급" in r["answer"], "전제 확인 실패 — 답변에 팩트 값이 없다"
    assert "fundA" in r["retrieved_context"], (
        "답변이 팩트를 반영했는데 그 근거 문서가 retrieved_context에 없다 — "
        f"실제: {r['retrieved_context'][:120]!r}")
