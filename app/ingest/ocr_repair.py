"""OCR 판독 실패 구간 복원 — 교차 문서 대조 + 실패 시 격리.

━━ 무엇이 문제인가 (2026-09-01 실물 확인) ━━
제공 코퍼스의 OCR 텍스트에는 인식하지 못한 글자가 남아 있다. 처음 확인된
형태는 '?'였다.

    - 위 세액공제 한도는 2023년 1월 1일 이후 납입금액부터 ?????????
    연금수령시 과세 연금소득세 5.5%~ 3.3%

이 상태로 인덱싱하면 '?????????'가 그대로 청크에 실려 retrieved_context와
인용문을 타고 **평가자 화면에 나간다.** 실제로 나갔다.

━━ '?'만이 아니다 (2026-09-02 추가 확인) ━━
같은 실패가 **다른 문서(doc55)에서는 다른 글자로** 나타났다 — OCR이
인식하지 못한 자리마다 '?'가 아니라 하나의 특정 한글 음절(예: '퇴')을
반복해서 채워 넣었다:

    퇴퇴 퇴 퇴퇴퇴퇴 : IRP 퇴퇴퇴퇴 퇴퇴 퇴퇴(퇴퇴퇴퇴 or IRP …)

숫자·영문("IRP", "1)")은 멀쩡한데 한글만 통째로 한 음절로 무너진 형태다.
문서 도메인에서 흔한 음절("퇴"—퇴직)로 잘못 수렴하는 것은 이미지 기반
OCR(seq2seq 디코더)이 확신을 잃었을 때 흔한 실패 양상이다(반복 붕괴).
근본 원인은 '?'와 같다 — 판독기가 글자를 못 읽고 대체문자를 채운 것이며,
그 대체문자가 무엇인지만 문서마다 다르다. 그래서 판정을 **특정 문자('?')**
에서 **아무 문자든 3회 이상 반복**으로 일반화했다. 복원·격리 메커니즘
(교차 문서 앵커 대조 → 후보 갈리면 포기 → 실패 시 격리)은 그대로다 —
근본 원인이 같으므로 대응도 같아야 한다.

━━ 왜 '파서 개선'이 아니라 '교차 대조'인가 ━━
이 '?'는 우리가 만든 것이 아니다. 우리 디코더가 실패했을 때 넣는 대체문자는
'�'(�)이지 '?'가 아니다(`zip_parser._decode` · `loader._decode`).
즉 제공된 OCR 텍스트에 이미 '?'로 들어와 있다. 판독기를 바꿔서 될 일이
아니고, 원본 페이지 이미지를 다시 OCR하는 것 외에는 '더 잘 뽑을' 방법이 없다.

그런데 이 코퍼스에는 재OCR보다 확실한 복원 재료가 있다 — **중복**이다.
투자설명서들은 같은 [연금저축계좌 과세 주요 사항] 표를 거의 그대로 싣는다.
한 문서에서 깨진 구절이 다른 문서에서는 멀쩡한 경우가 많다. 그래서 깨진
자리의 **앞뒤 문맥을 앵커로 삼아 코퍼스 전체에서 같은 구절을 찾아**, 그
자리에 실제로 있던 글자를 가져온다.

지어내는 것이 아니라 **코퍼스 안에 실재하는 텍스트로 메우는 것**이므로
근거가 있다. 재OCR과 달리 결과가 결정론적이고 재현 가능하다.

━━ 오탐이 미탐보다 나쁘다 ━━
복원이 틀리면 없던 문구가 근거 문서에 생긴다 — 날조와 구별되지 않는다.
그래서 CLAUDE.md의 "결정론 규칙은 확실할 때만"을 그대로 따른다:

  · 앵커가 짧으면(8자 미만) 복원하지 않는다
  · 후보가 둘 이상으로 갈리면 복원하지 않는다 (하나로 수렴할 때만)
  · 복원 길이에 상한을 둔다 (손상 규모에 비례)

━━ 복원에 실패하면 (대비책) ━━
남은 '?' 런은 `(판독불가)`로 치환한다. 이유:

  1. '?????????'는 평가자에게 시스템 결함으로 보인다.
     '(판독불가)'는 원문이 판독되지 않았다는 **사실의 정직한 표시**다.
  2. LLM이 '?'를 의미 있는 기호로 오해하고 해석하려 드는 것을 막는다.
  3. 치환을 **인제스트 한 곳**에서 하므로 검색·인용·프롬프트·
     retrieved_context가 전부 같은 텍스트를 본다. 인용 경계에서 따로
     처리하면 '검증이 본 텍스트'와 '사용자가 보는 텍스트'가 갈린다 —
     CLAUDE.md가 반복해서 경고하는 결함 계열이다.

━━ 2단계 문턱 (loader.repair_doubled_glyphs와 같은 구조) ━━
① 오염 판정은 **같은 글자**('?' 또는 한글 한 음절)가 3회 이상 연속으로
   한다. 투자설명서에서 물음표든 한글 음절이든 세 번 연달아 나오는 정상
   표기는 없다("정말입니까??"처럼 2회까지는 정상 표현일 수 있으므로
   판정에 넣지 않는다).
② 복원·치환 대상은 ①로 오염이 확인된 텍스트 안에서만 2회 이상 연속으로
   넓힌다. 두 글자짜리 손상('??', '퇴퇴')을 놓치지 않으면서, 정상 텍스트의
   물음표·반복 표현에는 손대지 않는다.

⚠️ **숫자·영문·구두점은 판정에 넣지 않는다.** "계좌번호 111222333444",
   "표 구분: -----------" 같은 정상 표기가 오탐으로 잡힌 사례가
   `loader.py`의 겹침 복원기에 실측돼 있다(같은 함정이라 여기도 피한다).
   판정 문자 집합은 '?'와 한글(가-힣)로만 한정한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 앵커로 쓸 공백 제외 문자 수. 12자는 법령 계층의 MIN_QUOTE_CHARS와 같은
# 감각이다 — 이 정도면 코퍼스 안에서 우연히 겹치지 않는다.
ANCHOR_LEN = 12
# 앵커가 이보다 짧으면(페이지 시작/끝에 붙은 손상) 복원을 포기한다.
MIN_ANCHOR = 8
# 복원 문자열 길이 상한 (절대 상한). 손상 규모에 비례한 상한과 함께 쓴다.
MAX_GAP = 40

UNREADABLE_MARK = "(판독불가)"

# 판독 실패 대체문자 후보 — '?' 아니면 한글 한 음절. 숫자·영문·구두점은
# 뺀다(위 docstring의 오탐 사례 참조). 서로 다른 종류(예: '?'와 '퇴')가
# 섞여 반복되는 것은 잡지 않는다 — 같은 글자의 반복만 오염 신호로 본다.
_GARBLED_CHAR = r'(?:\?|[가-힣])'
# ① 오염 판정용 — **같은 글자**가 3회 이상 연속 (\1로 동일 문자를 강제)
# ② 복원·치환용 — ①로 확인된 텍스트 안에서만 2회 이상
# 사이에 공백이 끼는 것("? ? ?", "퇴 퇴 퇴")까지 한 덩어리로 본다. OCR은
# 인식 실패 글자마다 대체문자를 찍으면서 원문의 자간을 함께 남기는 일이
# 있어서, 공백을 허용하지 않으면 같은 손상을 놓친다.
_GARBLED_STRONG = re.compile(rf'({_GARBLED_CHAR})(?:\s*\1){{2,}}')
_GARBLED_WEAK = re.compile(rf'({_GARBLED_CHAR})(?:\s*\1)+')

# 문서 경계 표식. 본문에 나올 수 없는 문자라 앵커가 문서를 넘어가지 못한다.
_SEP = "\x00"


def looks_garbled(text: str) -> bool:
    """OCR이 글자를 놓쳐 '?'로 남긴 텍스트인가 (고신뢰 판정)."""
    return bool(text) and _GARBLED_STRONG.search(text) is not None


def _space_free(text: str) -> tuple[str, list[int]]:
    """공백을 걷어낸 문자열과, 각 문자의 원문 인덱스.

    ━━ 왜 공백을 지우고 대조하는가 ━━
    OCR 텍스트는 같은 문구라도 문서마다 띄어쓰기가 제멋대로다. 실물에서
    확인된 예: "세 액공제", "다음 각호의", "연 금저축". 원문 그대로
    대조하면 같은 구절인데도 못 찾는다. 공백을 무시하고 맞춰야 한다.

    반환된 인덱스로 원문의 정확한 위치에 되돌려 쓸 수 있다.
    """
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


@dataclass
class RepairReport:
    """무엇을 고쳤고 무엇을 못 고쳤는지. 조용히 넘어가지 않기 위한 것."""

    pages_scanned: int = 0
    pages_garbled: int = 0
    runs_found: int = 0
    runs_repaired: int = 0
    runs_ambiguous: int = 0     # 후보가 갈려 복원을 포기
    runs_no_match: int = 0      # 코퍼스 어디에도 같은 구절이 없음
    runs_weak_anchor: int = 0   # 앞뒤 문맥이 모자람
    runs_masked: int = 0        # 복원 실패 → (판독불가)로 격리
    samples: list[str] = field(default_factory=list)

    @property
    def repair_rate(self) -> float:
        return self.runs_repaired / self.runs_found if self.runs_found else 0.0

    def summary(self) -> str:
        if not self.runs_found:
            return "OCR 판독 실패 구간 없음"
        return (f"OCR 판독 실패 {self.runs_found}건 "
                f"(페이지 {self.pages_garbled}/{self.pages_scanned}) → "
                f"복원 {self.runs_repaired}건 ({self.repair_rate:.0%}) · "
                f"격리 {self.runs_masked}건 "
                f"[후보충돌 {self.runs_ambiguous} · 대조실패 {self.runs_no_match} · "
                f"앵커부족 {self.runs_weak_anchor}]")


def _candidates(haystack: str, left: str, right: str, max_gap: int) -> set[str]:
    """코퍼스 전체에서 `left ... right` 사이에 실제로 있던 글자를 모은다.

    오염된(같은 글자 반복) 구간을 품은 후보는 제외한다 — 그래야 손상된
    자기 자신이 후보로 잡히지 않는다. 문서 경계(_SEP)를 넘는 것도 제외한다.

    ⚠️ "후보에 한글이 있으면 제외"가 아니다 — 그러면 정상적인 한글 복원
       후보까지 전부 걸러져 복원이 아예 안 된다. `_GARBLED_WEAK`(같은 글자
       2회 이상 반복)로 **자기 자신이 오염 패턴을 품고 있는지**만 본다.
    """
    found: set[str] = set()
    start = 0
    while True:
        i = haystack.find(left, start)
        if i < 0:
            break
        start = i + 1
        seg = i + len(left)
        window = haystack[seg:seg + max_gap + len(right)]
        j = window.find(right)
        if j < 0:
            continue
        cand = window[:j]
        if _SEP in cand or _GARBLED_WEAK.search(cand):
            continue
        found.add(cand)
        if len(found) > 1:
            # 이미 갈렸다. 더 봐도 결론은 '복원하지 않는다'로 같다.
            break
    return found


def _repair_text(text: str, haystack: str, report: RepairReport) -> str:
    """페이지 1건을 제자리 복원. haystack은 코퍼스 전체의 공백 제거본.

    ━━ 앵커 오염 방지 (2026-09-04 실측) ━━
    깨진 페이지는 평균 26.7건(5,279÷198)이 한 페이지에 몰려 있다. 앞뒤
    ANCHOR_LEN자를 고정폭으로 그냥 잘라내면, 그 안에 **다른** 손상 구간이
    함께 잘려 들어오는 경우가 매우 흔하다. 그러면 앵커 문자열 자체에
    '?'나 반복 음절이 섞이는데, 그 오염은 이 페이지에만 있는 것이라
    haystack 어디에도 글자 그대로 존재할 수 없다 — `find()`가 항상
    실패한다. 합성 재현(밀도 27건/페이지)에서 인접 손상 4건 중 3건이
    이렇게 앵커를 오염시켰다. **대조실패(no_match)의 압도적 다수가
    "코퍼스에 그 구절이 없어서"가 아니라 "검색어 자체가 깨져 있어서"였다.**

    그래서 앵커는 **이웃 손상 구간을 넘지 않는 만큼만** 가져온다. 이웃과의
    간격이 MIN_ANCHOR보다 좁으면(공간이 정말 없으면) 정직하게 앵커부족으로
    분류한다 — 억지로 이웃 손상까지 끌어와 채우지 않는다.
    """
    sf, idx = _space_free(text)
    spans = [(m.start(), m.end()) for m in _GARBLED_WEAK.finditer(sf)]
    edits: list[tuple[int, int, str]] = []

    for i, (a, b) in enumerate(spans):
        report.runs_found += 1

        left_bound = max(0, a - ANCHOR_LEN)
        if i > 0:
            _prev_start, prev_end = spans[i - 1]
            if prev_end > left_bound:
                left_bound = prev_end
        left = sf[left_bound:a]

        right_bound = min(len(sf), b + ANCHOR_LEN)
        if i + 1 < len(spans):
            next_start, _next_end = spans[i + 1]
            if next_start < right_bound:
                right_bound = next_start
        right = sf[b:right_bound]

        if len(left) < MIN_ANCHOR or len(right) < MIN_ANCHOR:
            report.runs_weak_anchor += 1
            continue

        # 손상 규모에 비례한 상한. '?' 하나가 글자 하나인 것이 보통이므로
        # 넉넉히 잡아도 2배 + 여유면 충분하다. 무제한으로 두면 엉뚱하게
        # 긴 구간을 끌어와 붙인다.
        gap = min(MAX_GAP, (b - a) * 2 + 6)
        cands = _candidates(haystack, left, right, gap)

        if len(cands) == 1:
            repl = cands.pop()
            edits.append((idx[a], idx[b - 1] + 1, repl))
            if len(report.samples) < 12:
                # ⚠️ '?' 하드코딩 금지 — 오염 문자는 '?'만이 아니라 한글
                #    음절일 수도 있다(doc55 '퇴' 사례). 실제 오염 구간
                #    그대로(sf[a:b])를 보여줘야 무엇이 무엇으로 바뀌었는지
                #    정확히 확인할 수 있다.
                report.samples.append(f"{left[-8:]} [{sf[a:b]} → "
                                      f"{repl or '(삭제)'}] {right[:8]}")
        elif cands:
            report.runs_ambiguous += 1
        else:
            report.runs_no_match += 1

    # 뒤에서부터 써야 앞쪽 인덱스가 밀리지 않는다.
    for s, e, repl in reversed(edits):
        text = text[:s] + repl + text[e:]
        report.runs_repaired += 1
    return text


def mask_unreadable(text: str) -> tuple[str, int]:
    """복원하지 못한 판독 실패 런('?' 또는 반복된 한글 음절)을 (판독불가)로 격리한다.

    오염이 확인된 텍스트(같은 글자가 3회 이상 연속인 구간이 어딘가 있는)
    에서만 동작하므로, 정상 문서의 물음표·짧은 반복 표현은 건드리지 않는다.
    """
    if not looks_garbled(text):
        return text, 0
    count = 0

    def _sub(_m: re.Match) -> str:
        nonlocal count
        count += 1
        return UNREADABLE_MARK

    return _GARBLED_WEAK.sub(_sub, text), count


def repair_documents(docs) -> RepairReport:
    """ParsedDocument 목록을 **제자리에서** 고친다.

    ⚠️ haystack은 복원 **전** 원문으로 만든다. 고쳐가며 만들면 복원 결과가
       다음 복원의 근거가 돼(연쇄) 한 번의 오복원이 코퍼스 전체로 번진다.
       한 번의 대조로 끝내면 모든 복원의 근거가 항상 '제공된 원문'이다.
    """
    report = RepairReport()
    pages = [pg for d in docs for pg in d.pages]
    report.pages_scanned = len(pages)

    garbled = [pg for pg in pages if looks_garbled(pg.text)]
    report.pages_garbled = len(garbled)
    if not garbled:
        return report          # 정상 코퍼스에서는 haystack조차 만들지 않는다

    haystack = _SEP.join(_space_free(pg.text)[0] for pg in pages)

    for pg in garbled:
        pg.text = _repair_text(pg.text, haystack, report)
        pg.text, masked = mask_unreadable(pg.text)
        report.runs_masked += masked

    return report
