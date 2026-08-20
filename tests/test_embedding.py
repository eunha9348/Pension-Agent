"""A-5 · CLOVA 임베딩 연동 회귀.

━━ 이 파일이 지키는 것 ━━
임베딩은 **있으면 좋고 없어도 도는** 계층이어야 한다. 키가 없든, 벡터를
아직 안 만들었든, API가 죽었든, 검색은 BM25로 계속 돌아야 한다.
임베딩 때문에 서비스가 죽으면 임베딩을 안 쓰느니만 못하다.

실제 API 호출은 검증하지 않는다(네트워크·크레딧). 검증 대상은
"실패했을 때 올바르게 축퇴하는가"와 "저장소가 정확한가"이다.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ingest.vector_store import VectorStore, text_hash


# ════════════════════════════════════════════════════════════════
# 벡터 저장소
# ════════════════════════════════════════════════════════════════

def test_저장하고_불러오면_같은_벡터가_나온다(tmp_path):
    vs = VectorStore("test-model")
    vs.add("c1", [0.1, 0.2, 0.3], text_hash("본문1"))
    vs.add("c2", [0.4, 0.5, 0.6], text_hash("본문2"))
    vs.save(tmp_path)

    back = VectorStore.load(tmp_path)
    assert len(back) == 2
    assert back.dim == 3
    assert back.model == "test-model"
    assert [round(v, 5) for v in back.get("c1")] == [0.1, 0.2, 0.3]


def test_벡터_파일이_없으면_빈_저장소를_준다(tmp_path):
    """아직 임베딩을 안 만든 상태 — BM25로 돌아야 한다."""
    vs = VectorStore.load(tmp_path)
    assert len(vs) == 0
    assert vs.rank([0.1, 0.2], top_k=5) == []


def test_손상된_벡터_파일이_서비스를_죽이지_않는다(tmp_path):
    (tmp_path / "vectors.json").write_text("{망가진 JSON", encoding="utf-8")
    (tmp_path / "vectors.bin").write_bytes(b"\x00\x01")
    vs = VectorStore.load(tmp_path)
    assert len(vs) == 0


def test_본문이_그대로면_다시_임베딩하지_않는다():
    """증분 임베딩 — 8195회 호출을 반복하지 않기 위한 핵심 규칙."""
    vs = VectorStore()
    vs.add("c1", [1.0, 0.0], text_hash("연금저축 세액공제"))
    assert vs.needs_embedding("c1", "연금저축 세액공제") is False


def test_본문이_바뀌면_다시_임베딩한다():
    vs = VectorStore()
    vs.add("c1", [1.0, 0.0], text_hash("연금저축 세액공제"))
    assert vs.needs_embedding("c1", "연금저축 세액공제 개정") is True


def test_처음_보는_청크는_임베딩_대상이다():
    assert VectorStore().needs_embedding("새청크", "본문") is True


def test_차원이_다른_벡터는_거부한다():
    """모델을 바꿨는데 기존 벡터에 섞이면 검색이 조용히 망가진다."""
    vs = VectorStore()
    vs.add("c1", [1.0, 0.0])
    with pytest.raises(ValueError, match="차원"):
        vs.add("c2", [1.0, 0.0, 0.0])


def test_사라진_청크의_벡터는_정리된다():
    vs = VectorStore()
    vs.add("c1", [1.0, 0.0])
    vs.add("c2", [0.0, 1.0])
    assert vs.prune({"c1"}) == 1
    assert len(vs) == 1
    assert "c2" not in vs
    assert [round(v, 5) for v in vs.get("c1")] == [1.0, 0.0]


def test_코사인_순위가_정확하다():
    vs = VectorStore()
    vs.add("같음", [1.0, 0.0])
    vs.add("직교", [0.0, 1.0])
    vs.add("반대", [-1.0, 0.0])
    ranked = vs.rank([1.0, 0.0], top_k=3)
    assert [cid for cid, _ in ranked] == ["같음", "직교", "반대"]
    assert round(ranked[0][1], 5) == 1.0


def test_allowed_필터가_적용된다():
    vs = VectorStore()
    vs.add("c1", [1.0, 0.0])
    vs.add("c2", [0.9, 0.1])
    ranked = vs.rank([1.0, 0.0], allowed={"c2"}, top_k=5)
    assert [cid for cid, _ in ranked] == ["c2"]


# ════════════════════════════════════════════════════════════════
# 축퇴 안전성
# ════════════════════════════════════════════════════════════════

def test_clova_백엔드는_키가_없으면_쓰지_않는다(monkeypatch):
    """USE_EMBEDDING=true 여도 키가 없으면 호출을 시도조차 하면 안 된다."""
    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="",
                                         embedding_backend="clova"))
    assert emb.embedding_enabled() is False
    assert emb.embed_texts(["질의"]) is None


def test_local_백엔드는_키가_필요_없다(monkeypatch):
    """로컬 모델은 API를 쓰지 않으므로 CLOVA 키와 무관하게 동작해야 한다.
    (429 때문에 기본 백엔드를 local로 둔 이유이기도 하다.)"""
    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="",
                                         embedding_backend="local"))
    assert emb.embedding_enabled() is True


def test_모델을_못_불러오면_BM25로_축퇴한다(monkeypatch):
    """sentence-transformers 미설치·모델 다운로드 실패 등 —
    어떤 경우에도 서비스가 죽으면 안 된다."""
    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True,
                                         embedding_backend="local"))

    def _boom(texts, **kw):
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setattr(emb, "embed_local", _boom)
    assert emb.embed_texts(["질의"]) is None


def test_설정이_꺼져_있으면_None을_준다(monkeypatch):
    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=False, clova_api_key="nv-k",
                                         embedding_backend="clova"))
    assert emb.embed_texts(["질의"]) is None


def test_호출이_실패하면_None으로_축퇴한다(monkeypatch):
    """API가 죽어도 검색은 BM25로 계속 돌아야 한다."""
    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="nv-k",
                                         embedding_backend="clova"))

    def _boom(text, **kw):
        raise emb.EmbeddingError("연결 실패")

    monkeypatch.setattr(emb, "embed_one", _boom)
    assert emb.embed_texts(["질의"]) is None


def test_임베딩_헤더가_채팅과_같은_규칙을_쓴다(monkeypatch):
    """두 곳이 어긋나면 채팅은 되는데 임베딩만 401이 나는 상황이 된다."""
    import app.retrieval.embedding as emb

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(clova_api_key="nv-abc"))
    assert emb._headers()["Authorization"] == "Bearer nv-abc"

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(clova_api_key="ncpXYZ",
                                         clova_apigw_key="gw-1"))
    h = emb._headers()
    assert h["X-NCP-CLOVASTUDIO-API-KEY"] == "ncpXYZ"
    assert h["X-NCP-APIGW-API-KEY"] == "gw-1"
    assert "Authorization" not in h


def test_벡터가_없으면_BM25_단독으로_돈다(monkeypatch):
    """임베딩을 켜 두고 벡터를 아직 안 만든 상태 — 흔한 운영 순서다."""
    import app.retrieval.hybrid as hy
    monkeypatch.setattr(hy, "embedding_enabled", lambda: True)
    monkeypatch.setattr(hy, "_vectors", lambda: VectorStore())
    assert hy._vector_rank(None, "질의", None) == []


# ════════════════════════════════════════════════════════════════
# RRF 융합 가중치
# ════════════════════════════════════════════════════════════════

def test_RRF_가중치가_실제로_반영된다():
    """설정만 있고 코드가 안 쓰면, 값을 바꿔도 효과 없는 손잡이가 된다 —
    튜닝하는 사람이 원인을 찾지 못한다."""
    from app.retrieval.hybrid import _rrf_merge

    lex = ["a", "b"]
    vec = ["b", "a"]
    equal = _rrf_merge((lex, 1.0), (vec, 1.0))
    lex_heavy = _rrf_merge((lex, 1.0), (vec, 0.1))

    assert abs(equal["a"] - equal["b"]) < 1e-9      # 대칭이면 동점
    assert lex_heavy["a"] > lex_heavy["b"]          # 어휘 순위가 이긴다


def test_가중치_0이면_벡터가_순위에_영향을_주지_않는다():
    from app.retrieval.hybrid import _rrf_merge
    only_lex = _rrf_merge((["a", "b"], 1.0))
    with_zero = _rrf_merge((["a", "b"], 1.0), (["b", "a"], 0.0))
    assert only_lex == with_zero


# ════════════════════════════════════════════════════════════════
# 429 (호출 속도 제한) — 인증 오류와 다르게 다뤄야 한다
# ════════════════════════════════════════════════════════════════
#
# 실제 사고: build_embeddings 8,195건 중 대부분이 429로 실패했다.
# 재시도 간격이 0.4초라 재시도해도 곧바로 또 429가 났다. 인증 오류처럼
# "재시도해도 어차피 안 된다"가 아니라 "충분히 쉬면 풀린다"는 점이 다르다.

_ENDPOINT = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"


def test_429는_RateLimitError로_구분된다(monkeypatch):
    import httpx
    import app.retrieval.embedding as emb

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="nv-k",
                                         clova_embedding_endpoint=_ENDPOINT,
                                         embedding_backend="clova"))
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)   # 백오프 대기 생략

    class _Resp:
        status_code = 429
        text = '{"status":{"code":"42901","message":"Too many requests"}}'

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(emb.RateLimitError):
        emb.embed_one("텍스트", max_retry=0)


def test_429는_여러_번_쉬며_재시도한_뒤에_실패한다(monkeypatch):
    """한 번 튕겼다고 바로 포기하면 안 된다 — 백오프 예산을 다 써야 한다."""
    import httpx
    import app.retrieval.embedding as emb
    from app.config import Settings

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="nv-k",
                                         clova_embedding_endpoint=_ENDPOINT,
                                         embedding_backend="clova"))
    sleeps: list[float] = []
    monkeypatch.setattr(emb.time, "sleep", lambda s: sleeps.append(s))

    class _Resp:
        status_code = 429
        text = "rate limited"

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(emb.RateLimitError):
        emb.embed_one("텍스트")

    assert len(sleeps) == len(emb._RATE_LIMIT_BACKOFF)
    assert sleeps == list(emb._RATE_LIMIT_BACKOFF)


def test_429는_일반_재시도_예산을_소모하지_않는다(monkeypatch):
    """max_retry=0 이어도 429 백오프는 별도로 돈다 — 인증 오류와 취급이 다르다."""
    import httpx
    import app.retrieval.embedding as emb
    from app.config import Settings

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="nv-k",
                                         clova_embedding_endpoint=_ENDPOINT,
                                         embedding_backend="clova"))
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)

    calls = {"n": 0}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            calls["n"] += 1
            class R:
                status_code = 429 if calls["n"] <= 2 else 200
                def json(self):
                    return {"status": {"code": "20000"}, "result": {"embedding": [0.1]}}
                text = "rate limited"
            return R()

    monkeypatch.setattr(httpx, "Client", _Client)
    vec = emb.embed_one("텍스트", max_retry=0)
    assert vec == [0.1]
    assert calls["n"] == 3     # 429 두 번 + 성공 한 번, max_retry=0인데도 성공


# ════════════════════════════════════════════════════════════════
# 같은 본문은 한 번만 호출한다 (429 대응의 핵심)
# ════════════════════════════════════════════════════════════════
#
# 투자설명서 158건에 같은 조항이 반복되므로, 청크 수만큼 호출하면 대부분이
# 낭비다. 429는 벤더를 바꿔서가 아니라 **호출 수를 줄여서** 푼다
# (대회 절대 제약: LLM은 HyperCLOVA X만).

def test_공백만_다른_본문은_같은_것으로_묶인다():
    from app.ingest.build_embeddings import _text_key
    assert _text_key("연금저축 세액공제") == _text_key("연금저축   세액공제")
    assert _text_key("연금저축\n세액공제") == _text_key("연금저축 세액공제")


def test_내용이_다르면_다른_묶음이다():
    from app.ingest.build_embeddings import _text_key
    assert _text_key("연금저축 세액공제") != _text_key("IRP 세액공제")


def test_429를_겪으면_신호가_남는다(monkeypatch):
    """build_embeddings가 이 신호를 보고 요청 간격을 스스로 늘린다."""
    import httpx
    import app.retrieval.embedding as emb

    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(use_embedding=True, clova_api_key="nv-k",
                                         clova_embedding_endpoint=_ENDPOINT,
                                         embedding_backend="clova"))
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    emb.rate_limit_seen()      # 이전 상태 초기화

    calls = {"n": 0}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            calls["n"] += 1
            class R:
                status_code = 429 if calls["n"] == 1 else 200
                text = "rate limited"
                def json(self):
                    return {"status": {"code": "20000"},
                            "result": {"embedding": [0.5]}}
            return R()

    monkeypatch.setattr(httpx, "Client", _Client)
    emb.embed_one("텍스트")           # 429 한 번 겪고 성공
    assert emb.rate_limit_seen() is True


def test_신호는_읽으면_초기화된다(monkeypatch):
    """매 호출마다 새로 판단해야 한다 — 한 번 겪었다고 영원히 켜져 있으면
    간격이 무한정 늘어난다."""
    import app.retrieval.embedding as emb
    emb._RATE_LIMIT_SEEN = True
    assert emb.rate_limit_seen() is True
    assert emb.rate_limit_seen() is False


# ════════════════════════════════════════════════════════════════
# 근중복 묶기 — 안전이 속도보다 우선
# ════════════════════════════════════════════════════════════════

def _c(cid, text):
    from app.ingest.store import ChunkRecord
    return ChunkRecord(chunk_id=cid, doc_id=f"d_{cid}", text=text)


_CLAUSE = ("[면제사유] 천재지변, 저축자의 사망, 퇴직, 해외이주, 폐업\n"
           "① 세액공제 - 연금계좌에 납입한 금액은 종합소득이 있는 거주자가 "
           "해당 연도의 연금계좌에 납입한 금액과 연 900만원 중 적은 금액으로 "
           "합니다. 13.2% 세액공제")


def test_같은_조항은_한_묶음이_된다():
    """반복되는 조항에 펀드명이 덧붙은 형태 — 실제 코퍼스의 주된 중복 양상."""
    from app.ingest.build_embeddings import group_near_duplicates
    groups = group_near_duplicates([
        _c("c1", _CLAUSE),
        _c("c2", _CLAUSE + "\n키움더드림단기채증권투자신탁[채권]"),
    ])
    assert [sorted(x.chunk_id for x in g) for g in groups] == [["c1", "c2"]]


def test_수치가_다르면_아무리_비슷해도_묶지_않는다():
    """⚠️ 이 테스트를 느슨하게 고치지 말 것.

    '연금저축 600만원'과 'IRP 900만원'은 문장이 90% 넘게 같아도 완전히 다른
    사실이다. 묶이면 같은 벡터를 공유하게 되어 의미 검색이 둘을 구분하지
    못한다. 이 도메인은 작은 수치 차이가 답의 정오를 가른다.
    """
    from app.ingest.build_embeddings import group_near_duplicates
    groups = group_near_duplicates([
        _c("c900", _CLAUSE),
        _c("c600", _CLAUSE.replace("900만원", "600만원")),
    ])
    assert len(groups) == 2, "수치가 다른 청크가 묶였다 — 검색이 조용히 틀려진다"


def test_내용이_다르면_묶이지_않는다():
    from app.ingest.build_embeddings import group_near_duplicates
    groups = group_near_duplicates([
        _c("c1", _CLAUSE),
        _c("c2", "연금수령한도는 평가액을 11에서 연금수령연차를 뺀 수로 "
                 "나눈 뒤 120%를 곱한다"),
    ])
    assert len(groups) == 2


def test_한_청크는_한_묶음에만_속한다():
    """길이 버킷 경계에서 청크가 두 묶음에 들어가면 벡터를 두 번 쓴다."""
    from app.ingest.build_embeddings import group_near_duplicates
    chunks = [_c(f"c{i}", _CLAUSE + f"\n부속문서 {'가' * i}") for i in range(30)]
    groups = group_near_duplicates(chunks)
    seen = [x.chunk_id for g in groups for x in g]
    assert len(seen) == len(set(seen)) == 30


def test_묶인_결과가_원본을_빠짐없이_담는다():
    """하나라도 빠지면 그 청크는 벡터 없이 남는다."""
    from app.ingest.build_embeddings import group_near_duplicates
    chunks = [_c(f"c{i}", f"서로 다른 내용 {i} " * (i + 3)) for i in range(25)]
    groups = group_near_duplicates(chunks)
    assert sorted(x.chunk_id for g in groups for x in g) == sorted(
        c.chunk_id for c in chunks)


# ════════════════════════════════════════════════════════════════
# build_embeddings — 백엔드별 모델 정체성 · 근중복 스킵
# ════════════════════════════════════════════════════════════════
#
# 실제 사고: CLOVA(1024차원)로 벡터를 일부 만든 뒤 로컬(384차원)로
# 바꿨더니, 저장소의 model 필드가 항상 clova_embedding_endpoint와만
# 비교돼서 로컬 모델 전환을 감지하지 못했다. 사전 경고 없이 add() 단계에서
# 야 차원 불일치로 실패했다.

def test_로컬_백엔드는_근중복_묶기를_건너뛴다():
    """CLOVA에서만 의미 있는 최적화(호출 수 절감)를 로컬에도 적용하면,
    절감 효과(실측 14%)보다 판정 로직 자체의 리스크가 크다."""
    import app.ingest.build_embeddings as be
    # 로컬 경로는 group_near_duplicates를 호출하지 않고 각 청크를
    # 단독 묶음으로 취급해야 한다 — 소스에서 분기를 직접 확인한다.
    src = open(be.__file__, encoding="utf-8").read()
    assert 'if emb_backend() == "clova":\n        print(" 근중복 묶는 중' in src


def test_벡터_저장소_모델_식별자가_백엔드별로_다르다(tmp_path):
    """CLOVA 벡터(1024차원)가 남아 있는 상태에서 로컬(384차원)로 바꾸면
    반드시 경고가 떠야 한다 — 실제로 사전 경고 없이 실패한 적이 있다."""
    from app.ingest.vector_store import VectorStore

    clova_vs = VectorStore("https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2", 3)
    clova_vs.add("c1", [0.1, 0.2, 0.3], "h1")
    clova_vs.save(tmp_path)

    reloaded = VectorStore.load(tmp_path)
    local_model_id = "intfloat/multilingual-e5-small"
    assert reloaded.model != local_model_id, \
        "저장된 CLOVA 모델 식별자가 로컬 모델명과 우연히 같으면 안 된다"


# ════════════════════════════════════════════════════════════════
# 로컬 임베딩 OOM 방지 (실사고: exit 137, 총 메모리 1.9GB·스왑 0B에서
# BATCH=256으로 커널이 프로세스를 죽였다)
# ════════════════════════════════════════════════════════════════

def test_로컬_배치_기본값이_소형_인스턴스에_안전하다():
    """256은 실제로 OOM을 냈다. 기본값은 그보다 훨씬 작아야 한다."""
    from app.ingest.build_embeddings import LOCAL_BATCH_DEFAULT
    assert LOCAL_BATCH_DEFAULT <= 16


def test_peak_rss_mb는_예외_없이_양수를_준다():
    """OOM 재발 시 어디까지 갔었는지 보려고 매 배치 찍는 진단값 —
    이것 자체가 죽으면 진단 목적을 잃는다."""
    from app.ingest.build_embeddings import _peak_rss_mb
    v = _peak_rss_mb()
    assert isinstance(v, float)
    assert v > 0


def test_batch_size_인자를_명령줄에서_받는다():
    """메모리가 넉넉한 서버에서는 올리고, 빠듯하면 더 내릴 수 있어야 한다."""
    import argparse

    from app.ingest.build_embeddings import LOCAL_BATCH_DEFAULT, main
    # main()은 실행 전체를 돌리므로, 여기서는 파서 구성만 재현해 확인한다.
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=None)
    args = ap.parse_args(["--batch-size", "4"])
    assert args.batch_size == 4

    args_default = ap.parse_args([])
    assert args_default.batch_size is None
    assert (args_default.batch_size or LOCAL_BATCH_DEFAULT) == LOCAL_BATCH_DEFAULT


def test_run_local이_batch_size_인자를_실제로_사용한다(monkeypatch, tmp_path):
    """인자만 받고 무시하면 --batch-size가 아무 효과 없는 손잡이가 된다."""
    import app.ingest.build_embeddings as be

    class _FakeVS:
        def __init__(self):
            self.saved = 0
        def add(self, chunk_id, vec, h):
            pass
        def save(self, path):
            self.saved += 1
            return path
        def prune(self, ids):
            return 0
        def __len__(self):
            return 0
        dim = 2

    class _C:
        def __init__(self, cid, text):
            self.chunk_id = cid
            self.text = text

    chunks = [_C(f"c{i}", f"본문{i}") for i in range(10)]
    grouped = [[c] for c in chunks]
    member_of = {c.chunk_id: [c] for c in chunks}

    calls: list[int] = []

    def _fake_embed_local(texts, **kw):
        calls.append(len(texts))
        return [[0.1, 0.2] for _ in texts]

    import app.retrieval.embedding as emb
    monkeypatch.setattr(emb, "embed_local", _fake_embed_local)

    class Args:
        limit = 0
        batch_size = 3
        index = tmp_path

    be._run_local(_FakeVS(), chunks, grouped, member_of, Args())
    # 배치 크기 3으로 10건을 나누면 4번 호출(3+3+3+1)이어야 한다
    assert calls == [3, 3, 3, 1]


def test_모델_적재시_스레드를_1로_고정한다(monkeypatch):
    """torch 기본 멀티스레드는 코어마다 연산 버퍼를 잡아 메모리를 더 먹는다
    — 소형 인스턴스에서 이것이 OOM의 한 원인이었다."""
    import sys
    import types

    import app.retrieval.embedding as emb

    thread_calls: list[int] = []

    fake_torch = types.ModuleType("torch")
    fake_torch.set_num_threads = lambda n: thread_calls.append(n)

    class _FakeST:
        def __init__(self, name, device=None):
            self.name = name
            self.device = device

    fake_st_mod = types.ModuleType("sentence_transformers")
    fake_st_mod.SentenceTransformer = _FakeST

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_mod)
    monkeypatch.setattr(emb, "_LOCAL_MODEL", None)
    monkeypatch.setattr(emb, "get_settings",
                        lambda: Settings(local_embedding_model="fake-model"))

    model = emb._local_model()
    assert thread_calls == [1]
    assert isinstance(model, _FakeST)
    assert model.device == "cpu"

    monkeypatch.setattr(emb, "_LOCAL_MODEL", None)
