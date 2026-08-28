"""법령 수집기 — 국가법령정보센터(법제처) OPEN API.

실행 위치
--------
**오프라인(배포 서버)에서만 돌린다.** 평가는 세션 없는 단일 GET이고
시간 예산이 있으므로, 요청 처리 중에 외부를 호출해서는 안 된다.
수집 결과는 data/law/articles.json에 저장되고 기동 시 한 번 적재된다.

    python -m app.law.crawler --oc <법제처OC> --out data/law/articles.json

⚠️ 이 파서는 **실 API 응답으로 검증되지 않았다.**
   개발 샌드박스에서 law.go.kr 이 egress 정책으로 차단돼 있어, 공개된
   응답 구조에 맞춘 픽스처로만 검증했다. 서버에서 처음 돌릴 때는 반드시
   --dry-run 으로 파싱 결과를 눈으로 확인한 뒤 저장할 것.
   구조가 다르면 조용히 빈 결과를 내지 않고 예외를 던지도록 해 두었다.

⚠️ 조문 원문은 어떤 경우에도 요약·가공하지 않는다. 인용 대조의 기준이
   되는 텍스트이므로, 가공하면 대조 자체가 무의미해진다.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from app.law.schema import LawArticle
from app.law.store import LawStore

log = logging.getLogger(__name__)

API_BASE = "https://www.law.go.kr/DRF"
SEARCH_URL = f"{API_BASE}/lawSearch.do"
SERVICE_URL = f"{API_BASE}/lawService.do"

# 연금 도메인에서 실제로 근거가 되는 법령들.
# 이 목록은 함정 규칙 26종이 다루는 주제(세액공제·연금소득·원천징수·
# 중도인출·연금수령한도·퇴직소득)를 덮도록 고른 것이다.
TARGET_LAWS = [
    "소득세법",
    "소득세법 시행령",
    "근로자퇴직급여 보장법",
    "근로자퇴직급여 보장법 시행령",
    "조세특례제한법",
    "조세특례제한법 시행령",
]

_REQUEST_INTERVAL_SEC = 0.7   # 공공 API에 부담을 주지 않는다


class CrawlError(RuntimeError):
    """수집 실패. 조용한 빈 결과보다 예외가 낫다."""


# ════════════════════════════════════════════════════════════════
# HTTP
# ════════════════════════════════════════════════════════════════

def _get(url: str, params: dict, timeout: float = 20.0) -> bytes:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "pension-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:                                   # noqa: BLE001
        raise CrawlError(f"요청 실패: {full} ({e})") from e


# ════════════════════════════════════════════════════════════════
# 파싱
# ════════════════════════════════════════════════════════════════
# 법제처 XML의 조문 단위는 <조문단위> 아래에 조문번호/조문가지번호/조문내용,
# 그리고 항이 있으면 <항><항내용>이 반복된다. 태그 이름이 한글이라
# ElementTree로 그대로 찾을 수 있다.

def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    # itertext로 중첩 태그 안의 글자까지 원문 순서대로 모은다.
    return "".join(el.itertext())


def _strip_leading(text: str, prefix: str) -> str:
    """앞에 붙은 번호 토큰을 뗀다. 항번호 '3'이 본문 앞에 붙는 것을 없앤다."""
    t = (text or "").strip()
    p = (prefix or "").strip()
    if p and t.startswith(p):
        t = t[len(p):].strip()
    return t


def _clause_text(c: ET.Element) -> str:
    """항 하나의 원문 — 그 아래 호·목까지 포함한다.

    ⚠️ 여기서 항내용만 뽑으면 **각 호가 통째로 사라진다.**
       실제로 그랬다(2026-08-28 실수집본):
         "③다음 각 호에 따른 소득의 금액은 종합소득과세표준을 계산할 때
           합산하지 아니한다."  ← 정작 '각 호'가 없다
       법령은 실질 내용을 호에 두는 경우가 많아, 1,500만원 분리과세 기준
       같은 핵심 수치가 통째로 수집되지 않았다.

       itertext는 각 텍스트 노드를 한 번씩만 훑으므로 호가 항내용 안에
       중첩돼 있든 형제로 놓여 있든 중복 없이 모두 담긴다.
    """
    return _strip_leading(_text(c), _text(c.find("항번호")))


def _article_label(no: str, branch: str) -> str:
    """조문번호(59) + 가지번호(3) → '제59조의3'."""
    no = (no or "").strip()
    branch = (branch or "").strip()
    if not no:
        return ""
    if branch and branch != "0":
        return f"제{no}조의{branch}"
    return f"제{no}조"


def parse_law_xml(xml_bytes: bytes, law_name: str, source_url: str,
                  fetched_at: str | None = None) -> list[LawArticle]:
    """법제처 조문 XML → LawArticle 목록. 항이 있으면 항 단위로 쪼갠다."""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise CrawlError(f"XML 파싱 실패 ({law_name}): {e}") from e

    # 시행일자는 기본정보에 있다. 구법 판별의 근거가 되므로 반드시 잡는다.
    eff = ""
    for tag in ("시행일자", "施行日字"):
        if (el := root.find(f".//{tag}")) is not None and _text(el).strip():
            eff = _text(el).strip()
            break
    if re.fullmatch(r'\d{8}', eff):
        eff = f"{eff[:4]}-{eff[4:6]}-{eff[6:]}"

    # 법령명은 응답에 있는 것을 우선한다(요청명과 표기가 다를 수 있다).
    if (el := root.find(".//법령명_한글")) is not None and _text(el).strip():
        law_name = _text(el).strip()

    units = root.findall(".//조문단위")
    if not units:
        raise CrawlError(
            f"조문단위를 찾지 못했습니다 ({law_name}). 응답 구조가 예상과 다릅니다 "
            f"— --dry-run 으로 원본을 확인하십시오.")

    out: list[LawArticle] = []
    for u in units:
        label = _article_label(_text(u.find("조문번호")),
                               _text(u.find("조문가지번호")))
        if not label:
            continue

        # 조문 제목만 있고 내용이 없는 '편/장/절' 표제는 건너뛴다.
        if _text(u.find("조문여부")).strip() == "전문":
            continue

        clauses = u.findall(".//항")
        if clauses:
            for c in clauses:
                cno = _text(c.find("항번호")).strip()
                body = _clause_text(c)
                if not body:
                    continue
                out.append(LawArticle(
                    law_name=law_name, article_no=label,
                    clause_no=f"제{cno}항" if cno else "",
                    text=body, effective_date=eff,
                    source_url=source_url, fetched_at=fetched_at))
        else:
            body = _text(u.find("조문내용")).strip()
            # 항이 없는 조문은 호가 조문내용의 형제로 놓이기도 한다.
            # 이미 담긴 것은 다시 붙이지 않는다(중첩된 경우 중복 방지).
            for h in u.findall("호"):
                if (t := _text(h).strip()) and t not in body:
                    body = f"{body}\n{t}".strip()
            if not body:
                continue
            out.append(LawArticle(
                law_name=law_name, article_no=label, clause_no="",
                text=body, effective_date=eff,
                source_url=source_url, fetched_at=fetched_at))

    if not out:
        raise CrawlError(f"조문을 하나도 추출하지 못했습니다 ({law_name}).")
    return out


# ════════════════════════════════════════════════════════════════
# 수집
# ════════════════════════════════════════════════════════════════

def fetch_law(oc: str, law_name: str, timeout: float = 20.0) -> list[LawArticle]:
    """법령명으로 조문 전체를 받아온다."""
    params = {"OC": oc, "target": "law", "type": "XML", "LM": law_name}
    url = f"{SERVICE_URL}?{urllib.parse.urlencode(params)}"
    log.info("수집: %s", law_name)
    raw = _get(SERVICE_URL, params, timeout=timeout)
    return parse_law_xml(raw, law_name, url)


def crawl(oc: str, laws: list[str] | None = None,
          timeout: float = 20.0) -> tuple[list[LawArticle], list[str]]:
    """대상 법령을 순회 수집. 실패한 법령은 기록하고 나머지를 계속한다."""
    laws = laws or TARGET_LAWS
    arts: list[LawArticle] = []
    errors: list[str] = []
    for i, name in enumerate(laws):
        if i:
            time.sleep(_REQUEST_INTERVAL_SEC)
        try:
            got = fetch_law(oc, name, timeout=timeout)
            arts.extend(got)
            log.info("  → %s 조문 %d건", name, len(got))
        except CrawlError as e:
            errors.append(f"{name}: {e}")
            log.error("  ✗ %s 실패 — %s", name, e)
    return arts, errors


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="법제처 OPEN API에서 연금 관련 법령 조문을 수집한다.")
    ap.add_argument("--oc", required=True,
                    help="법제처 OPEN API OC 값 (신청한 이메일의 @ 앞부분)")
    ap.add_argument("--out", default=str(Path("data/law/articles.json")))
    ap.add_argument("--laws", nargs="*", default=None,
                    help=f"수집할 법령명 (기본: {', '.join(TARGET_LAWS)})")
    ap.add_argument("--dry-run", action="store_true",
                    help="저장하지 않고 파싱 결과 표본만 출력 — 첫 실행 시 필수")
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    arts, errors = crawl(a.oc, a.laws, timeout=a.timeout)

    print()
    print(f"조문 {len(arts)}건 수집, 법령 {len({x.law_name for x in arts})}종")
    for name in sorted({x.law_name for x in arts}):
        n = sum(1 for x in arts if x.law_name == name)
        eff = next((x.effective_date for x in arts if x.law_name == name), "?")
        print(f"  · {name:24s} {n:4d}건  시행일 {eff}")
    if errors:
        print("\n실패:")
        for e in errors:
            print(f"  ✗ {e}")

    if a.dry_run:
        print("\n── 표본 3건 (원문 그대로) ──")
        for x in arts[:3]:
            print(f"\n[{x.ref}]  시행 {x.effective_date}")
            print(x.text[:300])
        print("\n--dry-run 이므로 저장하지 않았습니다.")
        return 0 if arts else 1

    if not arts:
        print("\n수집된 조문이 없어 저장하지 않습니다.", file=sys.stderr)
        return 1

    p = LawStore.save(arts, a.out)
    print(f"\n저장: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
