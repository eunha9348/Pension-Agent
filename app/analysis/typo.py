"""오타 교정 — 결정론적 폴백.

━━ 왜 필요한가 ━━
L1이 만드는 search_terms가 오타·구어체를 문서 용어로 옮겨 주지만, 그건
**LLM이 성공했을 때 얘기**다. L1 호출이 타임아웃·429로 실패하면 규칙 기반
추출(rule_based_spec)로 떨어지는데, 거기에는 오타 대응이 전혀 없었다.
L1 실패는 실제로 여러 번 발생했으므로, 그때 오타가 섞인 질의는
BM25에서 토큰이 하나도 안 걸려 검색이 통째로 실패한다.

   "세엑공제 얼마에요"  →  BM25: '세엑공제' 토큰 없음 → 근거 0건

━━ 잘못 고치는 것이 안 고치는 것보다 나쁘다 ━━
이 도메인은 한 글자 차이가 답을 뒤집는다.

   "연금수령연차"  ↔  "연금실제수령연차"     (편집거리 2)
     수령한도 결정        퇴직소득세 감면율 결정

이걸 오타로 보고 "교정"하면 답이 통째로 틀린다(trap_rules B1, critical).
그래서 세 겹으로 막는다:

  1. 이미 아는 용어는 건드리지 않는다 (교정 대상이 아니다)
  2. 길이 대비 보수적인 편집거리 상한 — 짧은 말일수록 더 엄격하게
  3. 구분해야 할 용어쌍(vocab.DISTINCT_PAIRS)을 넘나드는 교정은 폐기
  4. 후보가 둘 이상으로 애매하면 교정하지 않는다

━━ 원문을 바꾸지 않는다 ━━
교정 결과는 **검색어를 보태는 데만** 쓴다. 원 질의는 그대로 검색되므로,
교정이 빗나가도 원문으로 찾은 근거는 남는다.
"""

from __future__ import annotations

from app.analysis.vocab import DISTINCT_PAIRS, DOMAIN_TERMS, SYNONYM_GROUPS
from app.retrieval.tokenize import raw_tokens, stem

# 이 길이 미만은 교정하지 않는다. 짧은 말은 편집거리 1만 허용해도
# 전혀 다른 단어로 튀기 쉽다("연금"↔"연말", "세금"↔"새김").
MIN_LEN = 3

# 길이별 편집거리 상한. 길수록 오타 여지가 크므로 완화하되,
# 6자 이하는 1자만 허용해 "연금수령연차"(6자)가 두 글자 떨어진
# "연금실제수령연차"로 넘어가지 못하게 막는다.
def _max_distance(length: int) -> int:
    if length < MIN_LEN:
        return 0
    return 1 if length <= 6 else 2


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """레벤슈타인 거리. cap을 넘으면 조기 종료한다(전수 비교라 성능이 중요)."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 삭제
                           cur[j - 1] + 1,        # 삽입
                           prev[j - 1] + (ca != cb)))   # 치환
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _correctable_terms() -> set[str]:
    """교정 목표가 될 수 있는 도메인 용어."""
    terms: set[str] = set()
    for g in SYNONYM_GROUPS:
        terms |= g
    terms |= DOMAIN_TERMS
    return {t for t in terms if len(t) >= MIN_LEN}


_TARGETS = _correctable_terms()


def _crosses_distinct_boundary(src: str, dst: str) -> bool:
    """교정이 '구분해야 할 용어쌍'을 넘나드는가.

    편집거리 상한만으로도 대부분 막히지만, 상한을 나중에 완화했을 때
    조용히 뚫리지 않도록 명시적으로 한 번 더 막는다.
    """
    for _name, side_a, side_b in DISTINCT_PAIRS:
        in_a = any(t in src for t in side_a) or src in side_a
        in_b = any(t in src for t in side_b) or src in side_b
        dst_a = any(t in dst for t in side_a) or dst in side_a
        dst_b = any(t in dst for t in side_b) or dst in side_b
        if (in_a and dst_b and not dst_a) or (in_b and dst_a and not dst_b):
            return True
    return False


def correct_token(token: str) -> str:
    """토큰 하나를 도메인 용어로 교정. 교정하지 않으면 빈 문자열."""
    if len(token) < MIN_LEN or token in _TARGETS:
        return ""          # 이미 아는 말은 건드리지 않는다

    cap = _max_distance(len(token))
    if cap <= 0:
        return ""

    best: list[str] = []
    best_d = cap + 1
    for term in _TARGETS:
        d = edit_distance(token, term, cap)
        if d > cap:
            continue
        if d < best_d:
            best_d, best = d, [term]
        elif d == best_d:
            best.append(term)

    # 후보가 둘 이상이면 애매하다 — 찍지 않는다
    if len(best) != 1:
        return ""
    if _crosses_distinct_boundary(token, best[0]):
        return ""
    return best[0]


def correct_query(question: str) -> list[tuple[str, str]]:
    """질의에서 오타로 보이는 토큰을 찾아 [(원문, 교정)]으로 반환.

    원문을 바꾸지 않는다 — 호출 측이 검색어를 **보태는 데만** 쓴다.

    ⚠️ 조사를 먼저 뗀다. "연금저축과"를 그대로 보면 "연금저축"과 편집거리 1이라
       멀쩡한 용어가 오타로 잡히고, 쓸데없는 검색어가 예산을 차지한다.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for tok in raw_tokens(question):
        base = stem(tok)
        if base in seen:
            continue
        seen.add(base)
        fixed = correct_token(base)
        if fixed:
            out.append((tok, fixed))
    return out


def corrected_terms(question: str) -> list[str]:
    """검색어로 보탤 교정 결과만 추린다."""
    return [fixed for _raw, fixed in correct_query(question)]
