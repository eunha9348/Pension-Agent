"""법령 수집기 파서 테스트.

⚠️ 한계 — 이 테스트는 **공개된 응답 구조에 맞춘 픽스처**로만 검증한다.
   개발 환경에서 law.go.kr 이 egress 정책으로 차단돼 실 API 응답을
   받아보지 못했다. 서버 첫 실행은 반드시 --dry-run 으로 확인할 것.

   그래서 여기서 가장 중요한 테스트는 "정상 파싱"이 아니라
   **"구조가 다르면 조용히 빈 결과를 내지 않는다"** 쪽이다.
   빈 결과를 정상으로 넘기면, 법령 없이 도는 상태를 수집 성공으로 착각한다.
"""

from __future__ import annotations

import pytest

from app.law.crawler import CrawlError, parse_law_xml

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<법령>
  <기본정보>
    <법령명_한글>시험법</법령명_한글>
    <시행일자>20240101</시행일자>
  </기본정보>
  <조문>
    <조문단위>
      <조문번호>10</조문번호>
      <조문가지번호></조문가지번호>
      <조문내용>제10조(중도인출) 가입자는 다음 각 호의 사유로 인출할 수 있다.</조문내용>
      <항>
        <항번호>1</항번호>
        <항내용>가입자가 적립금을 중도인출하는 경우에는 대통령령으로 정하는 사유에 해당하여야 한다.</항내용>
      </항>
      <항>
        <항번호>2</항번호>
        <항내용>제1항의 사유는 대통령령으로 정한다.</항내용>
      </항>
    </조문단위>
    <조문단위>
      <조문번호>59</조문번호>
      <조문가지번호>3</조문가지번호>
      <조문내용>제59조의3(연금계좌세액공제) 연금계좌에 납입한 금액에 대하여는 공제한다.</조문내용>
    </조문단위>
  </조문>
</법령>
"""


def _parse():
    return parse_law_xml(_XML.encode("utf-8"), "요청명",
                         "https://example.invalid/x", "2026-08-27T00:00:00+00:00")


def test_항이_있으면_항_단위로_쪼갠다():
    arts = _parse()
    refs = [a.ref for a in arts]
    assert "시험법 제10조 제1항" in refs
    assert "시험법 제10조 제2항" in refs


def test_항이_없으면_조_단위로_담는다():
    arts = _parse()
    assert "시험법 제59조의3" in [a.ref for a in arts]


def test_가지번호가_조문표기에_반영된다():
    """'제59조의3'을 '제59조'로 뭉개면 다른 조문과 섞인다."""
    arts = _parse()
    a = next(a for a in arts if a.article_no == "제59조의3")
    assert a.clause_no == ""


def test_시행일자를_정규형으로_읽는다():
    """구법 판별의 근거이므로 반드시 잡혀야 한다."""
    arts = _parse()
    assert all(a.effective_date == "2024-01-01" for a in arts)


def test_응답의_법령명이_요청명보다_우선한다():
    arts = _parse()
    assert all(a.law_name == "시험법" for a in arts)


def test_원문이_가공되지_않는다():
    """인용 대조의 기준이므로 원문 그대로여야 한다."""
    arts = _parse()
    a = next(a for a in arts if a.ref == "시험법 제10조 제1항")
    assert a.text == ("가입자가 적립금을 중도인출하는 경우에는 "
                      "대통령령으로 정하는 사유에 해당하여야 한다.")


# ── 조용한 실패 금지 ★ ────────────────────────────────────────

def test_조문단위가_없으면_예외를_던진다():
    """빈 결과를 성공으로 넘기면 법령 없이 도는 상태를 눈치채지 못한다."""
    bad = b'<?xml version="1.0"?><law><item>x</item></law>'
    with pytest.raises(CrawlError, match="조문단위를 찾지 못했"):
        parse_law_xml(bad, "시험법", "u")


def test_XML이_깨지면_예외를_던진다():
    with pytest.raises(CrawlError, match="XML 파싱 실패"):
        parse_law_xml(b"<not-xml", "시험법", "u")


def test_내용이_전부_비면_예외를_던진다():
    empty = ('<법령><조문><조문단위><조문번호>1</조문번호>'
             '<조문내용></조문내용></조문단위></조문></법령>')
    with pytest.raises(CrawlError, match="하나도 추출하지 못했"):
        parse_law_xml(empty.encode("utf-8"), "시험법", "u")


# ── 호·목 누락 ★ 실수집본에서 드러난 결함 ──────────────────────
# 항내용만 뽑으면 그 아래 '각 호'가 통째로 사라진다. 법령은 실질 내용을
# 호에 두는 경우가 많아, 1,500만원 분리과세 기준 같은 핵심 수치가
# 수집본에 아예 없었다(--grep 0건으로 확인).

_XML_호 = """<?xml version="1.0" encoding="UTF-8"?>
<법령>
  <기본정보>
    <법령명_한글>시험법</법령명_한글>
    <시행일자>20260101</시행일자>
  </기본정보>
  <조문>
    <조문단위>
      <조문번호>14</조문번호>
      <조문내용>제14조(과세표준의 계산)</조문내용>
      <항>
        <항번호>3</항번호>
        <항내용>③다음 각 호에 따른 소득의 금액은 종합소득과세표준을 계산할 때 합산하지 아니한다.</항내용>
        <호>
          <호번호>9</호번호>
          <호내용>9. 연금소득의 합계액이 연 1천500만원 이하인 경우 그 연금소득</호내용>
        </호>
      </항>
    </조문단위>
  </조문>
</법령>
"""


def _parse_호():
    return parse_law_xml(_XML_호.encode("utf-8"), "시험법", "u", "t")


def test_항_아래의_호가_원문에_담긴다():
    arts = _parse_호()
    a = next(a for a in arts if a.ref == "시험법 제14조 제3항")
    assert "1천500만원" in a.text, (
        "각 호가 누락됐다 — 법령의 실질 내용 상당수가 호에 있다")
    assert "합산하지 아니한다" in a.text, "항 본문도 함께 있어야 한다"


def test_항번호가_본문_앞에_붙지_않는다():
    """_text는 항번호까지 훑으므로 떼어내지 않으면 '3③다음...'이 된다."""
    arts = _parse_호()
    a = next(a for a in arts if a.ref == "시험법 제14조 제3항")
    assert not a.text.startswith("3③"), a.text[:20]
    assert a.text.startswith("③"), a.text[:20]


def test_호가_담겨도_인용검증이_통과한다():
    """수집 → 대조까지 이어지는지. 호에 있는 수치를 인용할 수 있어야 한다."""
    from app.law.citation_guard import verify_citation
    from app.law.store import LawStore

    store = LawStore(_parse_호())
    c = verify_citation(store, "시험법 제14조 제3항",
                        "연금소득의 합계액이 연 1천500만원 이하인 경우")
    assert c.ok, c.reason


def test_파싱_결과가_인용검증에_그대로_쓰인다():
    """수집 → 저장 → 대조가 한 줄로 이어지는지."""
    from app.law.citation_guard import verify_citation
    from app.law.store import LawStore

    store = LawStore(_parse())
    c = verify_citation(store, "시험법 제10조 제1항",
                        "대통령령으로 정하는 사유에 해당하여야 한다")
    assert c.ok
