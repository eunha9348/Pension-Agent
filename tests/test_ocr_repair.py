"""OCR 판독 실패('?') 복원 — 실물 확인 결함 (2026-09-01).

━━ 실측 결함 ━━
제공 코퍼스의 OCR 텍스트에 인식 실패 구간이 '?'로 남아 있고, 그것이
청크 → retrieved_context → 인용문을 타고 화면에 그대로 나갔다:

    - 위 세액공제 한도는 2023년 1월 1일 이후 납입금액부터 ?????????

mock 코퍼스에는 '?' 런이 0건이라 이 결함은 **실물에서만 보인다**
(마크다운 노출 결함과 같은 계열 — mock만 보고는 발견할 수 없다).

━━ 무엇을 검증하는가 ━━
'?'는 우리가 만든 것이 아니므로 판독기를 바꿔서 고칠 수 없다. 대신
투자설명서들이 같은 표를 중복해서 싣는다는 성질을 이용해, 깨진 자리의
앞뒤 문맥을 앵커로 다른 문서에서 같은 구절을 찾아 메운다.

아래 테스트는 이 복원이 **틀린 글자를 만들어 내지 않는다**는 쪽에
무게를 둔다. 복원이 틀리면 없던 문구가 근거 문서에 생겨 날조와
구별되지 않기 때문이다 (CLAUDE.md "결정론 규칙은 확실할 때만").

※ 아래 fixture의 '멀쩡한 쌍둥이' 문장은 합성이다. 실제로 그 자리에
  어떤 글자가 있었는지는 서버의 실물 코퍼스를 대조해야 알 수 있다.
  테스트가 검증하는 것은 **복원 메커니즘**이지 특정 복원 결과가 아니다.
"""

from __future__ import annotations

from app.ingest.ocr_repair import (UNREADABLE_MARK, RepairReport, looks_garbled,
                                   mask_unreadable, repair_documents)
from app.ingest.zip_parser import Page, ParsedDocument


def _doc(doc_id: str, *texts: str) -> ParsedDocument:
    return ParsedDocument(doc_id=doc_id,
                          pages=[Page(i, t) for i, t in enumerate(texts, 1)])


# 실제로 화면에 나갔던 그 구절 (2026-09-01 실물 캡처)
_BROKEN = ("- 위 표의 세액공제 한도와는 별도로 당해연도 추가 세액공제 대상 "
           "금액으로 인정됨\n- 위 세액공제 한도는 2023년 1월 1일 이후 "
           "납입금액부터 ?????????\n연금수령시 과세 연금소득세 5.5%~ 3.3%")
# 다른 투자설명서에 실린 같은 구절 (합성 — 위 docstring 참조)
_INTACT = ("- 위 표의 세액공제 한도와는 별도로 당해연도 추가 세액공제 대상 "
           "금액으로 인정됨\n- 위 세액공제 한도는 2023년 1월 1일 이후 "
           "납입금액부터 적용됩니다\n연금수령시 과세 연금소득세 5.5%~ 3.3%")


# ── 탐지 ─────────────────────────────────────────────────────

def test_실물_캡처를_오염으로_판정한다():
    assert looks_garbled(_BROKEN) is True


def test_정상_문서는_오염으로_보지_않는다():
    assert looks_garbled(_INTACT) is False
    assert looks_garbled("연금수령한도는 어떻게 계산합니까?") is False
    # 물음표 두 개까지는 손대지 않는다 — 수사적 표기일 수 있다
    assert looks_garbled("정말입니까??") is False


def test_공백이_끼어도_한_덩어리로_본다():
    """OCR이 인식 실패 글자마다 '?'를 찍으며 자간을 남기는 경우."""
    assert looks_garbled("납입금액부터 ? ? ? 연금수령시") is True


# ── 교차 문서 복원 ───────────────────────────────────────────

def test_다른_문서의_같은_구절로_복원한다():
    """★ 실측 사고 재현 — 깨진 자리를 코퍼스 안의 실재 텍스트로 메운다."""
    docs = [_doc("R2_KR5129420025", _BROKEN), _doc("R2_KR5111450067", _INTACT)]
    report = repair_documents(docs)

    fixed = docs[0].pages[0].text
    assert "?" not in fixed
    assert "납입금액부터 적용됩니다" in fixed
    assert report.runs_repaired == 1
    assert report.runs_masked == 0
    # 나머지 문장은 손상되지 않아야 한다
    assert "연금소득세 5.5%~ 3.3%" in fixed


def test_띄어쓰기가_달라도_복원한다():
    """★ OCR 텍스트는 같은 문구라도 문서마다 띄어쓰기가 제멋대로다.

    실물에서 확인된 예: "세 액공제", "다음 각호의", "연 금저축".
    원문 그대로 대조하면 같은 구절인데도 못 찾는다.
    """
    broken = _doc("A", "연금저축계좌 납입액 600만원 이내 ?????? 13.2%입니다")
    intact = _doc("B", "연금저축계좌 납 입액 600만 원 이내 세액공제 13.2%입니다")
    report = repair_documents([broken, intact])

    fixed = broken.pages[0].text
    assert "?" not in fixed
    assert "세액공제" in fixed
    assert report.runs_repaired == 1


def test_복원해도_다른_문서는_건드리지_않는다():
    docs = [_doc("A", _BROKEN), _doc("B", _INTACT)]
    repair_documents(docs)
    assert docs[1].pages[0].text == _INTACT


# ── 오탐 방지 (복원이 틀리면 날조가 된다) ────────────────────

def test_후보가_갈리면_복원하지_않는다():
    """★ 두 문서가 서로 다른 글자를 말하면 어느 쪽도 근거가 되지 못한다.

    억지로 하나를 고르면 그 순간 '지어낸 근거'가 된다.
    """
    broken = _doc("A", "연금저축계좌 납입액 600만원 이내 ?????? 13.2%입니다")
    alt1 = _doc("B", "연금저축계좌 납입액 600만원 이내 세액공제 13.2%입니다")
    alt2 = _doc("C", "연금저축계좌 납입액 600만원 이내 소득공제 13.2%입니다")
    report = repair_documents([broken, alt1, alt2])

    assert report.runs_repaired == 0
    assert report.runs_ambiguous == 1
    # 복원하지 못했으므로 격리된다 — '?'가 남아서는 안 된다
    assert "?" not in broken.pages[0].text
    assert UNREADABLE_MARK in broken.pages[0].text


def test_앵커가_짧으면_복원하지_않는다():
    """페이지 맨 앞/뒤에 붙은 손상은 문맥이 모자라 특정할 수 없다."""
    broken = _doc("A", "?????? 13.2%")
    intact = _doc("B", "세액공제 13.2%")
    report = repair_documents([broken, intact])

    assert report.runs_repaired == 0
    assert report.runs_weak_anchor == 1


def test_코퍼스_어디에도_없으면_복원하지_않는다():
    broken = _doc("A", "연금저축계좌 납입액 600만원 이내 ?????? 13.2%입니다")
    other = _doc("B", "전혀 다른 내용의 문서입니다. 퇴직연금 수령한도 관련 조항.")
    report = repair_documents([broken, other])

    assert report.runs_repaired == 0
    assert report.runs_no_match == 1
    assert UNREADABLE_MARK in broken.pages[0].text


def test_정상_코퍼스에는_아무것도_하지_않는다():
    """★ 오탐 0 — mock 코퍼스처럼 멀쩡한 자료를 건드리면 안 된다."""
    docs = [_doc("A", _INTACT), _doc("B", "연금수령한도는 어떻게 됩니까?")]
    before = [pg.text for d in docs for pg in d.pages]
    report = repair_documents(docs)

    assert report.runs_found == 0
    assert report.pages_garbled == 0
    assert [pg.text for d in docs for pg in d.pages] == before


# ── 대비책 — 복원 실패 시 격리 ───────────────────────────────

def test_복원_실패분은_판독불가로_격리된다():
    """★ '?????????'가 평가자 화면에 나가서는 안 된다.

    복원하지 못했다는 사실 자체는 숨기지 않는다 — 다만 시스템 결함으로
    보이는 '?' 대신, 원문이 판독되지 않았다는 정직한 표시를 남긴다.
    """
    out, n = mask_unreadable(_BROKEN)
    assert n == 1
    assert "?" not in out
    assert UNREADABLE_MARK in out
    assert "납입금액부터 (판독불가)" in out


def test_격리는_오염된_텍스트에서만_동작한다():
    """정상 문서의 물음표는 남겨 둔다."""
    text = "연금수령한도는 어떻게 계산합니까?"
    out, n = mask_unreadable(text)
    assert n == 0
    assert out == text


def test_복원과_격리를_거치면_어떤_경우에도_물음표_런이_남지_않는다():
    """★ 최종 불변식 — 복원되든 안 되든 '?' 런은 밖으로 나가지 않는다."""
    docs = [
        _doc("A", _BROKEN),                                     # 복원됨
        _doc("B", _INTACT),
        _doc("C", "완전히 고립된 문장 ?????? 뒤쪽 문맥도 충분히 깁니다"),  # 대조실패
        _doc("D", "짧음 ??? 짧음"),                              # 앵커부족
    ]
    repair_documents(docs)
    for d in docs:
        for pg in d.pages:
            assert "??" not in pg.text, f"{d.doc_id}에 '?' 런이 남았다"


# ── 연쇄 오복원 방지 ─────────────────────────────────────────

def test_복원_결과가_다음_복원의_근거가_되지_않는다():
    """★ haystack은 복원 **전** 원문으로 한 번만 만든다.

    고쳐가며 대조하면 한 번의 오복원이 다음 복원의 '근거'가 돼 코퍼스
    전체로 번진다. 모든 복원의 근거는 항상 제공된 원문이어야 한다.
    """
    import inspect

    from app.ingest import ocr_repair

    src = inspect.getsource(ocr_repair.repair_documents)
    # haystack 생성이 복원 루프보다 앞에 있어야 한다
    assert src.index("haystack =") < src.index("for pg in garbled")


# ── 배선 — 인제스트를 실제로 지나가는가 ──────────────────────

def test_build_index를_통과하면_청크에_물음표_런이_없다(tmp_path):
    """★ 부품이 아니라 **배선**을 지나가는 테스트.

    CLAUDE.md: "배선을 검사하는 테스트는 배선을 지나가야 한다."
    복원 모듈만 단위 테스트하면, build_index가 그것을 호출하지 않아도
    전부 통과한다 — 실제로 법령 계층이 통째로 죽은 채 배포된 이력이 있다.
    """
    from app.ingest.build_index import ingest

    (tmp_path / "a.txt").write_text(_BROKEN, encoding="utf-8")
    (tmp_path / "b.txt").write_text(_INTACT, encoding="utf-8")

    store = ingest(tmp_path, "real")

    assert store.chunks, "청크가 만들어지지 않았다"
    for c in store.chunks.values():
        assert "??" not in c.text, f"청크에 '?' 런이 남았다: {c.text[:80]}"
    # 복원까지 됐는지 (격리로 때운 것이 아니라)
    assert any("납입금액부터 적용됩니다" in c.text for c in store.chunks.values())


def test_복원_집계가_인덱스에_새겨진다(tmp_path):
    """★ 감사 결과는 도달해야 한다.

    빌드 로그를 놓치면 "원문이 얼마나 깨져 있었는지"를 알 방법이 없어진다.
    집계를 인덱스에 새겨야 /health에서 사후에 확인할 수 있다.
    """
    from app.ingest.build_index import ingest
    from app.ingest.store import DocumentStore

    (tmp_path / "a.txt").write_text(_BROKEN, encoding="utf-8")
    (tmp_path / "b.txt").write_text(_INTACT, encoding="utf-8")

    store = ingest(tmp_path, "real")
    assert store.ocr_repair["runs_found"] == 1
    assert store.ocr_repair["runs_repaired"] == 1

    # 저장 → 적재를 거쳐도 남아 있어야 한다
    out = tmp_path / "idx"
    store.save(out)
    assert DocumentStore.load(out).ocr_repair["runs_found"] == 1


def test_정상_코퍼스는_집계가_0이다(tmp_path):
    from app.ingest.build_index import ingest

    (tmp_path / "a.txt").write_text(_INTACT, encoding="utf-8")
    store = ingest(tmp_path, "real")
    assert store.ocr_repair["runs_found"] == 0
    assert "없음" in store.ocr_repair["summary"]


def test_이_코드보다_먼저_만들어진_인덱스도_물음표를_내보내지_않는다():
    """★ 적재 시점 안전망.

    인덱스는 호스트 볼륨에 남고 entrypoint는 있으면 재사용한다. 이미지를
    새로 빌드해도 **예전 인덱스에 박힌 '?'는 사라지지 않는다.** 재인덱싱을
    잊으면 그대로 나간다 — 실제로 그런 상태로 운영됐다.
    """
    from app.ingest.store import ChunkRecord

    rec = ChunkRecord.from_dict({
        "chunk_id": "c1", "doc_id": "R2_KR5129420025", "text": _BROKEN})
    assert "?" not in rec.text
    assert UNREADABLE_MARK in rec.text


def test_적재_안전망은_정상_청크를_건드리지_않는다():
    from app.ingest.store import ChunkRecord

    text = "연금수령한도는 어떻게 계산합니까?"
    assert ChunkRecord.from_dict(
        {"chunk_id": "c1", "doc_id": "d", "text": text}).text == text


def test_보고서_요약은_복원율을_말한다():
    r = RepairReport(pages_scanned=10, pages_garbled=3, runs_found=4,
                     runs_repaired=3, runs_masked=1)
    assert "복원 3건 (75%)" in r.summary()
