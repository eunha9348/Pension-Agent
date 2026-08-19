"""L3 검색 품질 회귀 — 실배포 실패 2건에서 도출.

이 파일의 테스트는 전부 **실제로 발생한 사고**를 고정한 것이다.
느슨하게 고치고 싶어지면 아래 사고 기록을 먼저 읽을 것.

Q-001 (명퇴 교사)
  근거 8건 중 6건이 글자까지 동일한 세액공제 보일러플레이트였다.
  근거 예산을 사실 하나가 독점해, 정작 필요한 명예퇴직급여 문서가
  근거에 들어오지 못했다. → 중복 제거(A-2) · 함정 유도 검색(A-1)

Q-002 (솔로몬 국공채)
  근거 2건이 전부 '집합투자기구의 연혁'이었다. 펀드명에 "중장기국공채"가
  수십 번 반복되니 연혁 청크의 어휘 밀도가 본문보다 높았기 때문이다.
  근거에 답이 없으니 LLM이 만기 구간을 지어냈다. → 저정보 강등(A-4)
"""

from __future__ import annotations

from app.core.trap_rules import TRAPS, build_trap_context
from app.ingest.store import ChunkRecord
from app.retrieval.rerank import is_low_information, jaccard, rerank

# ── 실제 코퍼스에서 가져온 연혁 청크 (Q-002에서 근거로 잘못 뽑힌 것) ──
CHRONO_TEXT = """2. 집합투자기구의 연혁
2009.05.11 최초설정
2010.02.16 이익배분관련소득세법시행령개정사항 반영, 증권거래세 면제 조항 삭제
2010.07.16 비교지수변경(Customized KIS 중장기 채권지수 (1Y~7Y, 듀레이션: 3.0±0.7))
2011.04.19 집합투자업자 주소변경, 법시행령 개정사항 반영
2012.10.19 클래스 추가 (종류 C-I)
2014.04.07 클래스 명칭 변경(종류 직판F->종류 F) 및 가입자격 변경
2014.10.15 종류S 신설
2024.08.30 종류A-e 신설"""

SUBSTANTIVE_TEXT = """[연금저축계좌 과세 주요 사항]
납입요건 가입기간 5년 이상, 연 1,800만 원 한도(퇴직연금, 타 연금저축 납입액 포함)
수령요건 55세 이후 10년간 연간 연금수령한도 내에서 연금수령
세액공제 연간 연금저축계좌 납입액 600만 원 이내 세액공제 13.2%(지방소득세 포함)"""


def _chunk(cid: str, doc: str, text: str) -> ChunkRecord:
    return ChunkRecord(chunk_id=cid, doc_id=doc, text=text)


# ════════════════════════════════════════════════════════════════
# A-4 · 저정보(연혁·목차) 청크 강등
# ════════════════════════════════════════════════════════════════

def test_연혁_청크를_저정보로_판정한다():
    assert is_low_information(CHRONO_TEXT) is True


def test_본문_청크는_저정보가_아니다():
    assert is_low_information(SUBSTANTIVE_TEXT) is False


def test_연혁은_제거가_아니라_강등이다():
    """'이 클래스 언제 신설됐나요' 같은 질의에서는 연혁이 정답이다.
    후보가 그것뿐이면 여전히 근거로 나와야 한다."""
    cands = [(_chunk("c1", "d1", CHRONO_TEXT), 1.0)]
    out, _ = rerank(cands, top_k=8)
    assert len(out) == 1


def test_본문이_연혁을_이긴다():
    """BM25 점수가 연혁 쪽이 높아도 최종 순위는 뒤집혀야 한다.
    Q-002 실패의 핵심 — 연혁이 본문을 밀어냈다."""
    cands = [
        (_chunk("c1", "d1", CHRONO_TEXT), 1.0),        # 어휘 밀도로 1위였음
        (_chunk("c2", "d2", SUBSTANTIVE_TEXT), 0.6),
    ]
    out, report = rerank(cands, top_k=8)
    assert out[0][0].chunk_id == "c2"
    assert report.low_info_demoted == 1


# ════════════════════════════════════════════════════════════════
# A-2 · 근중복 제거
# ════════════════════════════════════════════════════════════════

def test_동일_보일러플레이트는_한_건만_남는다():
    """Q-001 사고 — 158개 투자설명서의 동일 조항이 근거 8칸을 다 먹었다."""
    cands = [(_chunk(f"c{i}", f"d{i}", SUBSTANTIVE_TEXT), 1.0 - i * 0.01)
             for i in range(6)]
    out, report = rerank(cands, top_k=8)
    assert len(out) == 1
    assert report.duplicates_removed == 5


def test_펀드명만_다른_문서도_중복으로_본다():
    """실제 코퍼스는 조항이 같고 펀드명만 다른 경우가 대부분이다."""
    a = SUBSTANTIVE_TEXT + "\n미래에셋스마트롱숏70증권자투자신탁1호(주식)"
    b = SUBSTANTIVE_TEXT + "\n미래에셋그린뉴딜인덱스증권자투자신탁(주식)"
    out, _ = rerank([(_chunk("c1", "d1", a), 1.0),
                     (_chunk("c2", "d2", b), 0.9)], top_k=8)
    assert len(out) == 1


def test_서로_다른_사실은_남긴다():
    """중복 제거가 과하면 근거가 빈약해진다 — 오탐 회귀."""
    out, _ = rerank([(_chunk("c1", "d1", SUBSTANTIVE_TEXT), 1.0),
                     (_chunk("c2", "d2", CHRONO_TEXT), 0.9)], top_k=8)
    assert len(out) == 2


def test_jaccard_경계():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert 0 < jaccard({"a", "b"}, {"b", "c"}) < 1


# ════════════════════════════════════════════════════════════════
# A-3 · 문서 다양성
# ════════════════════════════════════════════════════════════════

def test_한_문서가_근거를_독점하지_못한다():
    cands = [(_chunk(f"c{i}", "same_doc", f"{SUBSTANTIVE_TEXT} 항목{i} " * (i + 1)), 1.0 - i * 0.01)
             for i in range(5)]
    out, _ = rerank(cands, top_k=8, max_per_doc=2)
    assert len({rec.doc_id for rec, _ in out}) == 1
    assert len(out) <= 2


def test_후보가_부족하면_다양성_제한을_푼다():
    """근거를 못 채우느니 같은 문서를 한 번 더 쓰는 편이 낫다."""
    cands = [(_chunk(f"c{i}", "same_doc", f"고유내용{i} " * 30), 1.0 - i * 0.01)
             for i in range(4)]
    out, report = rerank(cands, top_k=4, max_per_doc=2)
    assert len(out) == 4
    assert report.relaxed is True


def test_pinned_청크는_다양성_제한을_면제받는다():
    """함정이 지목한 근거는 문서 편중을 이유로 빠지면 안 된다."""
    cands = [(_chunk(f"c{i}", "same_doc", f"고유내용{i} " * 30), 0.5)
             for i in range(4)]
    out, _ = rerank(cands, top_k=8, max_per_doc=1,
                    pinned={"c0", "c1", "c2", "c3"})
    assert len(out) == 4


# ════════════════════════════════════════════════════════════════
# A-1 · 함정 규칙 → 검색 유도
# ════════════════════════════════════════════════════════════════

def test_함정_규칙에서_근거_문서_ID를_뽑는다():
    by_id = {r.id: r for r in TRAPS}
    assert by_id["E1"].source_doc_ids() == {"doc55"}
    assert by_id["B1"].source_doc_ids() == {"doc40"}
    # 여러 문서를 지목하는 경우
    assert by_id["C6"].source_doc_ids() == {"doc39", "doc40", "R2_KR516702010M"}


def test_문서를_특정하지_못하는_source는_걸러진다():
    """'각 투자설명서' 같은 서술로는 검색을 유도할 수 없다."""
    by_id = {r.id: r for r in TRAPS}
    assert by_id["D2"].source_doc_ids() == set()


def test_명퇴_질의는_명예퇴직_문서를_검색에_지목한다():
    """Q-001 회귀 — 이 질의에서 doc55가 지목되지 않으면 같은 실패가 재현된다."""
    ctx = build_trap_context(
        "명퇴하는 교사예요. 명퇴수당을 연금계좌에 넣으면 세금감면이 "
        "어마어마하다던데, 절세법만 알려주세요.")
    steer = ctx["retrieval_steer"]
    steered_docs = {d for item in steer for d in item["docs"]}
    assert "doc55" in steered_docs, "명예퇴직급여 근거 문서가 검색에 전달되지 않는다"


def test_유도_항목은_질의로_쓸_fact를_함께_넘긴다():
    """사용자 질의는 구어체라 그대로 쓰면 법령체 문서가 안 걸린다.
    fact 문장을 질의로 써야 그 격차를 넘는다."""
    ctx = build_trap_context("명퇴수당을 연금계좌에 넣으면 어떻게 되나요")
    assert ctx["retrieval_steer"], "유도 대상이 비어 있다"
    for item in ctx["retrieval_steer"]:
        assert item["fact"].strip()
        assert item["docs"]


def test_critical_함정이_먼저_슬롯을_받는다():
    ctx = build_trap_context(
        "명퇴수당을 연금계좌에 넣으면 세금감면이 얼마나 되나요")
    sev = [i["severity"] for i in ctx["retrieval_steer"]]
    assert "critical" in sev


def test_함정이_없으면_유도도_없다():
    """일반 질의에 불필요한 문서를 끌어오면 안 된다 — 오탐 회귀."""
    ctx = build_trap_context("안녕하세요")
    assert ctx["retrieval_steer"] == []
