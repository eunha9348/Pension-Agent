"""OCR 판독 실패 대체문자 일반화 — '?'만이 아니다 (2026-09-02).

━━ 실물 확인 ━━
doc55에서 OCR 판독 실패가 '?'가 아니라 **하나의 한글 음절('퇴')을
반복해서 채우는 형태**로 나타났다:

    퇴퇴 퇴 퇴퇴퇴퇴 : IRP 퇴퇴퇴퇴 퇴퇴 퇴퇴(퇴퇴퇴퇴 or IRP …)

숫자·영문("IRP", "1)")은 멀쩡한데 한글만 통째로 한 음절로 무너졌다.
'?' 전용이던 `ocr_repair.py`의 판정을 **같은 글자가 3회 이상 반복**으로
일반화했다('?' 또는 한글 한 음절). 복원·격리 메커니즘(교차 문서 앵커
대조 → 후보 갈리면 포기 → 실패 시 격리)은 그대로다.

━━ 왜 안전하다고 판단했는가 ━━
· mock 코퍼스 14페이지 전수 + 298건 실측 질의 전수에서 오탐 0건
  (딱 한 건 예외 — 사용자가 강조로 "??????"를 쓴 질의. 그런데 이
  탐지기는 **코퍼스 문서에만** 적용되고 사용자 질의에는 적용되지 않는다
  — build_index.py·store.py·check_corpus.py 세 곳뿐이다)
· "와와와"·"하하하"·"저저저는요" 같은 구어체 반복은 이론상 걸릴 수
  있지만, 이 코퍼스는 **투자설명서(공식 금융 문서)**다 — 감탄사·의성어가
  나올 도메인이 아니다. 설령 걸리더라도 최악의 경우는 숫자·사실이 아닌
  감탄사 하나가 (판독불가)로 격리되는 것뿐이다(낮은 심각도).
· 숫자·영문·구두점은 판정에서 제외했다(loader.py의 겹침 복원기가 이미
  겪은 오탐 계열 — "111222333444", "-----------").
"""

from __future__ import annotations

from app.ingest.loader import corpus_files, load_file
from app.ingest.ocr_repair import (UNREADABLE_MARK, looks_garbled,
                                   mask_unreadable, repair_documents)
from app.ingest.zip_parser import Page, ParsedDocument

# 실제로 화면에 나갔던 그 형태를 근사한 구절 (2026-09-02 실물 확인)
_DOC55_LIKE = ("퇴퇴 퇴 퇴퇴퇴퇴 : IRP 퇴퇴퇴퇴 퇴퇴 퇴퇴(퇴퇴퇴퇴 or IRP o) "
               "퇴퇴 퇴퇴퇴 퇴퇴퇴퇴 퇴퇴 가가 or 퇴퇴퇴퇴 퇴퇴퇴퇴퇴퇴 퇴 퇴) "
               "퇴. 퇴퇴퇴퇴퇴퇴 퇴퇴 퇴퇴 1) 퇴퇴")


def _doc(doc_id: str, *texts: str) -> ParsedDocument:
    return ParsedDocument(doc_id=doc_id,
                          pages=[Page(i, t) for i, t in enumerate(texts, 1)])


# ── 탐지 — '?' 아닌 한글 반복도 잡는가 ───────────────────────

def test_실물_doc55_패턴을_오염으로_판정한다():
    assert looks_garbled(_DOC55_LIKE) is True


def test_숫자와_영문은_판정에서_제외한다():
    """★ 기존 loader.py 겹침 복원기의 실측 오탐을 여기서도 피해야 한다."""
    for text in ("계좌번호 111222333444 입니다",
                 "표 구분: ----------- 합계",
                 "코드 AAABBBCCCDDD 확인",
                 "............"):
        assert looks_garbled(text) is False, text


def test_2회까지는_정상_표현으로_본다():
    """'?' 전용 시절과 같은 문턱(3회 이상)을 한글에도 동일하게 적용한다."""
    for text in ("정말입니까??", "네네", "가만가만 하세요", "빨리빨리 진행합시다"):
        assert looks_garbled(text) is False, text


def test_기존_물음표_판정은_그대로다():
    """★ 회귀 방지 — 일반화하면서 기존 '?' 동작을 깨면 안 된다."""
    broken = "위 세액공제 한도는 2023년 1월 1일 이후 납입금액부터 ?????????"
    assert looks_garbled(broken) is True
    out, n = mask_unreadable(broken)
    assert n == 1
    assert UNREADABLE_MARK in out
    assert "?" not in out


# ── 격리 (대비책) ────────────────────────────────────────────

def test_doc55_패턴은_복원_실패시_판독불가로_격리된다():
    out, n = mask_unreadable(_DOC55_LIKE)
    assert n >= 1
    assert "퇴" * 2 not in out, "복원되지 않은 반복 구간이 남았다"
    assert UNREADABLE_MARK in out
    # 정상 토큰(IRP·숫자)은 그대로 보존돼야 한다
    assert "IRP" in out
    assert "or" in out


def test_격리해도_영문_숫자는_보존된다():
    out, _n = mask_unreadable(_DOC55_LIKE)
    assert "1)" in out


# ── 교차 문서 복원 ───────────────────────────────────────────

def test_다른_문서에_같은_구절이_있으면_한글_반복도_복원된다():
    broken = _doc("doc55",
                  "이 계좌는 만 55세 이후 퇴퇴퇴퇴 가능하며 별도 신청이 필요합니다")
    intact = _doc("R2_test",
                  "이 계좌는 만 55세 이후 연금수령 가능하며 별도 신청이 필요합니다")
    report = repair_documents([broken, intact])

    fixed = broken.pages[0].text
    assert "퇴퇴퇴퇴" not in fixed
    assert "연금수령" in fixed
    assert report.runs_repaired == 1


def test_자기_자신의_오염이_복원_후보로_잡히지_않는다():
    """★ _candidates()가 '?'만이 아니라 반복된 한글도 자기 오염으로 걸러야 한다.

    두 문서 다 같은 자리가 오염돼 있으면(둘 다 '퇴퇴퇴퇴') 복원할 근거가
    없다 — 후보 집합이 비어야 하고, 오염된 자기 자신을 후보로 삼으면 안 된다.
    """
    broken1 = _doc("doc55", "이 계좌는 만 55세 이후 퇴퇴퇴퇴 가능합니다 그 다음 문장입니다")
    broken2 = _doc("doc20", "이 계좌는 만 55세 이후 퇴퇴퇴퇴 가능합니다 그 다음 문장입니다")
    report = repair_documents([broken1, broken2])

    assert report.runs_repaired == 0
    assert "퇴퇴퇴퇴" not in broken1.pages[0].text  # 격리는 됐어야 한다
    assert UNREADABLE_MARK in broken1.pages[0].text


# ── mock 코퍼스 전수 — 오탐 0건 재확인 ────────────────────────

def test_mock_코퍼스_전수에서_일반화된_탐지기도_오탐이_없다():
    """★ '?' 전용에서 '모든 한글 반복'으로 넓힌 뒤 다시 재는 회귀 테스트.

    문턱을 넓힐 때마다 다시 실측해야 한다는 CLAUDE.md 원칙을 따른다.
    """
    for p in corpus_files("data/corpus_mock"):
        for pg in load_file(p).pages:
            assert not looks_garbled(pg.text), (
                f"{p.name}: 정상 텍스트가 오염으로 오판됐다 — {pg.text[:100]}")


# ── 배선 — 실제로 인제스트를 지나가는가 ──────────────────────

def test_build_index를_통과하면_한글_반복_오염도_청크에_남지_않는다(tmp_path):
    from app.ingest.build_index import ingest

    (tmp_path / "a.txt").write_text(
        "이 계좌는 만 55세 이후 퇴퇴퇴퇴 가능하며 별도 신청이 필요합니다",
        encoding="utf-8")
    (tmp_path / "b.txt").write_text(
        "이 계좌는 만 55세 이후 연금수령 가능하며 별도 신청이 필요합니다",
        encoding="utf-8")

    store = ingest(tmp_path, "real")
    assert store.chunks
    for c in store.chunks.values():
        assert not looks_garbled(c.text), f"청크에 오염이 남았다: {c.text[:80]}"
