"""겹쳐 그려진 PDF 텍스트 복원 — 배수(fold) 일반화 (2026-09-01).

━━ 실측 결함 ━━
실물 코퍼스의 doc20·doc55 근거 문서가 "는는는" "연연연" "퇴퇴퇴" 처럼
같은 글자가 **3번씩** 반복된 채로 사용자 화면에 노출됐다.

복원 로직(`repair_doubled_glyphs`)은 있었지만 **2배 겹침 전용**이었다.
3배 런은 감지조차 되지 않는다 — "퇴퇴퇴직직직"에서 쌍을 찾으면
(퇴퇴)(직직) 두 쌍뿐이라 '4쌍 연속' 문턱에 닿지 못하기 때문이다.
그래서 복원기가 있는데도 아무 일도 하지 않고 그대로 통과시켰다.

━━ 함께 고친 것: 판정 문자 집합 ━━
판정에 숫자·영문·구두점이 들어 있어 정상 문서를 오염으로 오판했다.
실측된 오탐 넷을 아래 테스트가 고정한다. 결정론 계층의 오탐은 LLM
감사가 되돌리지 못하므로(단조성) 미탐보다 엄격히 나쁘다 —
그래서 판정은 한글 반복만 보고, 복원은 확인된 구간에서만 숫자·구두점을
함께 걷어낸다.
"""

from __future__ import annotations

from app.ingest.loader import (detect_fold, looks_doubled,
                               repair_doubled_glyphs)

# ── 3배 겹침 — 이번에 새로 잡게 된 것 ────────────────────────

def test_3배_겹침을_감지하고_복원한다():
    """★ 실측 사고 재현 — doc20·doc55에서 본 그 패턴."""
    broken = "퇴퇴퇴직직직연연연금금금은은은 이렇게 나온다"
    assert detect_fold(broken) == 3
    assert repair_doubled_glyphs(broken) == "퇴직연금은 이렇게 나온다"


def test_3배_구간의_숫자와_괄호도_함께_걷어낸다():
    """오염이 한글로 확인되면, 그 안의 수치·구두점도 복원 대상이다."""
    broken = "연연연금금금수수수령령령한한한도도도 111222000%%% 적적적용용용"
    assert detect_fold(broken) == 3
    assert repair_doubled_glyphs(broken) == "연금수령한도 120% 적용"


# ── 2배 겹침 — 기존 동작이 유지되는가 ────────────────────────

def test_2배_겹침은_그대로_복원된다():
    broken = "퇴퇴직직연연금금제제도도는는 다음과 같다"
    assert detect_fold(broken) == 2
    assert repair_doubled_glyphs(broken) == "퇴직연금제도는 다음과 같다"


def test_문서_원문_예시가_그대로_복원된다():
    """loader 주석에 적힌 실제 깨짐 사례."""
    broken = "평가가액액 × 112200 ((1111 -- 연연금금수수령령연연차차))"
    assert repair_doubled_glyphs(broken) == "평가액 × 120 (11 -- 연금수령연차)"


def test_배수를_섞어_적용하지_않는다():
    """★ 3배 텍스트에 2배 복원을 걸면 글자가 남고, 반대면 사라진다.

    배수를 먼저 판정하고 그 배수로만 걷어내야 한다.
    """
    assert repair_doubled_glyphs("가가가나나나다다다라라라") == "가나다라"
    assert repair_doubled_glyphs("가가나나다다라라") == "가나다라"


# ── 오탐 방지 (실측된 것들) ──────────────────────────────────

def test_반복되는_숫자를_뭉개지_않는다():
    """★ 판정에 숫자가 들어 있어 "111222333444"가 "1234"가 되던 결함."""
    text = "계좌번호 111222333444 입니다"
    assert detect_fold(text) == 0
    assert repair_doubled_glyphs(text) == text

    text2 = "코드 11223344 확인"
    assert repair_doubled_glyphs(text2) == text2


def test_구분선을_짧게_만들지_않는다():
    """★ 표 구분선이 오염으로 잡혀 길이가 줄던 결함."""
    for text in ("표 구분: ----------- 합계", "------------", "............"):
        assert detect_fold(text) == 0, text
        assert repair_doubled_glyphs(text) == text


def test_반복되는_영문을_뭉개지_않는다():
    """신용등급 표기 등."""
    text = "등급 AAABBBCCCDDD 표기"
    assert repair_doubled_glyphs(text) == text


def test_정상_문장은_손대지_않는다():
    for text in ("연금수령연차는 11년차부터 한도가 없다",
                 "수수료는 1100원이고 가입자가 부담한다",
                 "가입자가 신청한 날이 속하는 과세기간"):
        assert looks_doubled(text) is False, text
        assert repair_doubled_glyphs(text) == text


def test_mock_코퍼스_전수에서_오탐이_없다():
    """★ 문턱을 바꿀 때마다 다시 돌려야 하는 검사.

    실물 코퍼스는 저장소에 없으므로(gitignore), 저장소 안에서 오탐을
    잴 수 있는 유일한 자료가 mock이다. 여기서 한 건이라도 바뀌면
    문턱이 너무 낮은 것이다.
    """
    from app.ingest.loader import corpus_files, load_file

    for p in corpus_files("data/corpus_mock"):
        for pg in load_file(p).pages:
            assert repair_doubled_glyphs(pg.text) == pg.text, \
                f"{p.name}: 정상 텍스트가 변형됐다"


# ── 배선 ─────────────────────────────────────────────────────

def test_청킹을_지나가면_복원된다():
    """★ 부품이 아니라 배선. chunker가 복원기를 호출하지 않으면 실패한다."""
    from app.ingest.chunker import chunk_document
    from app.ingest.zip_parser import Page, ParsedDocument

    # 실제 3배 렌더링은 숫자 글리프도 3배가 된다 ("6" → "666")
    doc = ParsedDocument(doc_id="doc20", pages=[Page(
        1, "제제제666조조조 연연연금금금수수수령령령\n"
           "이이이 조조조항항항은은은 다다다음음음과과과 같같같다다다")])
    chunks = chunk_document(doc)
    assert chunks
    joined = "\n".join(c.text for c in chunks)
    assert "제6조 연금수령" in joined
    assert "연연연" not in joined


def test_고립된_단일_반복은_복원하지_않는다():
    """★ 알려진 한계를 명시적으로 고정한다 (설계상 의도).

    복원 대상은 '같은 배수의 반복이 2번 이상 이어지는' 구간이다. 홀로
    떨어진 "가가가" 하나는 손대지 않는다 — 낮추면 정상 표기인 "999",
    "1100"까지 뭉개기 때문이다. 실제 겹침은 글리프마다 일어나 반드시
    연속으로 나타나므로, 이 한계 때문에 놓치는 실물 손상은 없다.
    """
    text = "퇴퇴퇴직직직연연연금금금은은은 그러나 여기 999 는 그대로"
    out = repair_doubled_glyphs(text)
    assert "퇴직연금은" in out
    assert "999" in out
