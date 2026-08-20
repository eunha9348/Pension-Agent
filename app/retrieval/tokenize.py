"""한국어 토크나이저 (형태소 분석기 없이).

━━ 왜 직접 만드는가 ━━
mecab/khaiii 같은 형태소 분석기는 설치 부담이 크고, 컨테이너 재현성이
떨어진다. 반면 이 도메인의 검색·매칭은 **명사 위주**여서 조사/어미만
잘라내도 실용적인 정확도가 나온다.

━━ 두 가지 모드를 구분하는 이유 ━━
· `index_terms()` — 검색(BM25)용. 재현율이 중요하므로 어간 + 문자 bigram을
  함께 낸다. 순위 매기기이므로 약간의 잡음은 허용된다.
· `content_terms()` — 규칙 매칭(슬롯-근거, 답변-슬롯)용. 정밀도가 중요하므로
  bigram을 내지 않고 어간만 낸다.

  ⚠️ 규칙 매칭에 부분 문자열(substring) 비교를 쓰면 안 된다.
     "정해지는"에서 "해지"가 잡히는 종류의 오탐이 실제로 발생한 이력이 있다.
     이 파일의 매칭 함수는 전부 **토큰 경계** 기준으로 비교한다.
"""

from __future__ import annotations

import re

# 어말 조사·어미. 긴 것부터 잘라야 한다(예: "에서는" → "에서" 아닌 전체 제거)
_SUFFIXES = [
    "이라는", "라는", "이라고", "라고", "에서는", "에게는", "한테는",
    "입니다", "습니다", "합니다", "됩니다", "인가요", "인지요", "나요",
    "까요", "네요", "에서", "에게", "한테", "으로", "부터", "까지",
    "보다", "처럼", "마다", "이나", "이란", "라도", "이며", "지만",
    "는데", "인데", "이는", "은", "는", "이", "가", "을", "를", "에",
    "의", "와", "과", "도", "만", "로", "나", "요", "고",
]

_STOPWORDS = {
    "그리고", "그런데", "하지만", "그래서", "때문", "경우", "정도", "관련",
    "무엇", "어떻게", "얼마", "얼마나", "어떤", "무슨", "누구", "언제", "어디",
    "저는", "제가", "우리", "지금", "정말", "너무", "조금", "이거", "그거",
    "알려주세요", "해주세요", "궁금", "질문", "문의", "생각", "이번", "다음",
    "있나", "없나", "되나", "하나", "인가", "이런", "저런", "그런",
}

_TOKEN = re.compile(r'[가-힣]+|[A-Za-z]+|\d+(?:\.\d+)?')


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or "")).strip().lower()


def _strip_suffix(token: str) -> str:
    """어말 조사/어미 제거. 어간이 2자 미만이 되면 자르지 않는다."""
    for suf in _SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 2:
            return token[: -len(suf)]
    return token


def raw_tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def stem(token: str) -> str:
    """어말 조사·어미를 뗀 어간. 오타 교정처럼 토큰 단위로 다룰 때 쓴다.

    "연금저축과" → "연금저축". 이걸 먼저 떼지 않으면 조사가 붙은 정상 용어가
    오타로 보인다(편집거리 1).
    """
    return _strip_suffix(token)


def content_terms(text: str) -> set[str]:
    """규칙 매칭용 내용어 집합 (어간, bigram 없음).

    정밀도 우선 — 여기서 나온 항목끼리는 **완전 일치**로만 비교한다.
    """
    out: set[str] = set()
    for t in raw_tokens(text):
        if t in _STOPWORDS:
            continue
        if re.fullmatch(r'\d+(?:\.\d+)?', t):
            out.add(t)
            continue
        stem = _strip_suffix(t)
        if len(stem) < 2 or stem in _STOPWORDS:
            continue
        out.add(stem)
        if stem != t:
            out.add(t)
    return out


def index_terms(text: str) -> list[str]:
    """검색(BM25) 색인·질의용 텀. 재현율 우선 — 어간 + 문자 bigram.

    한국어는 복합명사가 붙어 다니므로("연금수령한도"), bigram이 없으면
    "수령한도"로 질의했을 때 매칭되지 않는다.
    """
    terms: list[str] = []
    for t in raw_tokens(text):
        if t in _STOPWORDS:
            continue
        stem = _strip_suffix(t)
        if re.fullmatch(r'\d+(?:\.\d+)?', t):
            terms.append(t)
            continue
        if len(stem) >= 2:
            terms.append(stem)
        # 복합명사 부분 매칭용 문자 bigram
        if len(stem) >= 3:
            terms.extend(stem[i:i + 2] for i in range(len(stem) - 1))
    return terms
