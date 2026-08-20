"""L1 · 질의 분석 (extract_query_spec).

HyperCLOVA X Function calling으로 요구사항 슬롯·계산함수·실행계획을 뽑는다.
**LLM이 실패하거나 mock이면 규칙 기반 추출기가 같은 스키마를 채운다.**
즉 이 계층은 LLM 없이도 동작하며, LLM은 정확도를 올리는 역할만 한다.

━━ 왜 결정론적 폴백이 필수인가 ━━
① 지금 CLOVA 실연동이 안 된 상태에서도 나머지 계층을 전부 굴려야 한다.
② 실연동 후에도 LLM 호출은 실패할 수 있다. 그때 파이프라인이 통째로
   멈추면 평가에서 0점이다.
③ planned_calls는 어차피 CALC_REGISTRY 화이트리스트로 검증되므로,
   LLM이 골라주든 규칙이 골라주든 검증 관문은 같다.

━━ 출력 스키마 ━━
{
  "query": 원문,
  "intent": "세액공제" | "상품_비교" | ...,
  "entities": {product_name, fund_class, plan_type, ...},
  "asked_for": [{"id","description","type","required","calc_function"}],
  "user_conditions": {...},          # conditions.py 정규 스키마
  "planned_calls": [{"function","args"}],
  "plan": ["...", ...]               # think_trace 서두에 배치
}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.analysis.calc_params import remap_function
from app.analysis.conditions import derive_conditions
from app.analysis.typo import corrected_terms
from app.core.coverage_pipeline import CALC_REGISTRY

# ════════════════════════════════════════════════════════════════
# Function calling 스키마
# ════════════════════════════════════════════════════════════════

QUERY_SPEC_TOOL = [{
    "type": "function",
    "function": {
        "name": "extract_query_spec",
        "description": (
            "연금 질의를 분석해 ① 질문이 요구한 답변 구성요소(asked_for), "
            "② 사용자가 밝힌 조건(user_conditions), ③ 호출할 결정론적 계산함수"
            "(planned_calls), ④ 실행 계획(plan)을 추출한다. "
            "숫자를 직접 계산하지 말 것 — 계산은 등록된 함수만 수행한다."),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "질의 의도 (예: 세액공제, 연금수령한도, 과세방식, 상품_비교)",
                },
                "asked_for": {
                    "type": "array",
                    "description": "질문이 명시적으로 요구한 답변 구성요소",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "type": {"type": "string",
                                     "enum": ["fact", "calculation", "comparison"]},
                            "required": {"type": "boolean"},
                            # ⚠️ 여기에 enum(등록 함수 15종)을 넣지 말 것.
                            #    HCX-005가 값이 많은 enum을 담은 tools를 통째로
                            #    거부한다(HTTP 400 · 40009 Unsupported function).
                            #    그러면 L1 호출이 매번 실패해 규칙 폴백으로만
                            #    돌아간다 — 실제로 평가 42건 전부가 그랬다.
                            #    스키마로 강제하지 않아도 안전한 이유:
                            #    supervise_plan()이 CALC_REGISTRY 화이트리스트로
                            #    미등록 함수를 결정론적으로 제거하고,
                            #    remap_function()이 유사 명칭을 정규화한다.
                            #    즉 검증 관문은 그대로 남아 있고, 스키마 enum은
                            #    중복이었다. 함수명은 description으로 안내한다.
                            "calc_function": {
                                "type": "string",
                                "description":
                                    "type이 calculation일 때만. 다음 중 하나를 "
                                    "정확히 적을 것: " + ", ".join(sorted(CALC_REGISTRY)),
                            },
                        },
                        "required": ["id", "description", "type"],
                    },
                },
                "user_conditions": {
                    "type": "object",
                    "description": "질의에서 확인된 사용자 조건. "
                                   "금액은 만원 단위 숫자로, 원 단위면 키에 _won 접미사.",
                    "properties": {
                        "account_type": {"type": "string"},
                        "age": {"type": "integer"},
                        "pension_year": {"type": "integer"},
                        "actual_receipt_year": {"type": "integer"},
                        "service_years": {"type": "integer"},
                        "pension_saving_manwon": {"type": "number"},
                        "irp_manwon": {"type": "number"},
                        "severance_manwon": {"type": "number"},
                        "account_value_manwon": {"type": "number"},
                        "total_income_manwon": {"type": "number"},
                        "private_pension_annual_manwon": {"type": "number"},
                        "fund_class": {"type": "string"},
                    },
                },
                "entities": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "product_code": {"type": "string"},
                        "fund_class": {"type": "string"},
                        "plan_type": {"type": "string"},
                    },
                },
                "search_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "문서 검색에 쓸 **문서 어휘**. 사용자의 구어체·오타·줄임말을 "
                        "제공 문서에서 실제로 쓰는 정식 용어로 바꿔 적는다. "
                        "예: '아이알피 세엑공제 얼마' → ['IRP', '개인형퇴직연금', '세액공제']. "
                        "⚠️ 비슷해 보여도 다른 제도는 절대 합치지 말 것 — "
                        "'연금수령연차'와 '연금실제수령연차'는 다른 개념이므로 "
                        "질문에 있는 쪽을 그대로 둔다."),
                },
                "plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "실행 계획 단계 (예: '자격 확인 → 한도 계산 → 조건별 비교')",
                },
            },
            "required": ["intent", "asked_for"],
        },
    },
}]


# ── 축소 스키마 (1단 폴백) ──────────────────────────────────
# CLOVA가 tools를 거부하면(스키마 요건이 문서화돼 있지 않고 조용히 바뀐다)
# 예전에는 곧장 규칙 폴백으로 떨어졌다. 규칙 폴백은 search_terms 재작성도,
# 의도 분류도 못 하므로 검색 품질이 눈에 띄게 떨어진다.
# 그래서 그 사이에 한 단계를 둔다 — **검증된 요소만으로 만든 최소 스키마**.
# (진단 사다리에서 스칼라·문자열배열·중첩객체·객체배열은 전부 통과했고,
#  값이 많은 enum만 거부됐다. 여기에는 enum을 아예 쓰지 않는다.)
MINIMAL_QUERY_SPEC_TOOL = [{
    "type": "function",
    "function": {
        "name": "extract_query_spec",
        "description": "연금 질의의 의도와 요구사항을 구조화한다.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "질의 의도"},
                "asked_for": {
                    "type": "array",
                    "description": "질문이 요구한 답변 구성요소",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "type": {"type": "string"},
                        },
                    },
                },
                "search_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "문서에서 쓰는 정식 용어로 옮긴 검색어",
                },
            },
            "required": ["intent"],
        },
    },
}]

L1_SYSTEM_PROMPT = """당신은 연금 상담 질의를 분석하는 분석기입니다.
답변을 작성하지 마십시오. 질문이 요구한 것이 무엇인지만 구조화하십시오.

규칙:
1. 숫자를 직접 계산하지 마십시오. 계산이 필요하면 등록된 계산함수를 지정하십시오.
2. 질문에 없는 조건을 지어내지 마십시오. 확인되지 않은 조건은 비워 두십시오.
3. 연금수령연차와 연금실제수령연차는 다른 개념입니다. 질문이 어느 쪽을 말하는지
   불분명하면 임의로 정하지 말고 비워 두십시오.
4. 금액 단위에 주의하십시오. 계산함수는 만원 단위를 씁니다.
5. search_terms에는 **문서에서 쓰는 용어**를 적으십시오.
   사용자는 구어체로 묻고 오타도 냅니다("세엑공제", "아이알피", "연저축",
   "깨면 손해", "얼마나 떼요"). 문서는 법령체입니다("세액공제",
   "개인형퇴직연금", "중도해지", "원천징수세율").
   질문의 표현을 문서 쪽 표현으로 옮겨 적어야 검색이 됩니다.
   다만 **다른 제도를 같은 것으로 합치지 마십시오** — 특히
   연금수령연차/연금실제수령연차, 중도인출/부득이한사유 인출처럼
   한 글자 차이로 결과가 달라지는 용어는 질문에 있는 쪽을 유지하십시오."""


# ════════════════════════════════════════════════════════════════
# 규칙 기반 추출 (LLM 폴백 겸 검증 기준)
# ════════════════════════════════════════════════════════════════

@dataclass
class TopicRule:
    """질의 주제 → 요구사항 슬롯 매핑."""
    intent: str
    keywords: tuple[str, ...]
    slot_id: str
    description: str
    calc_function: Optional[str] = None
    fact_description: Optional[str] = None   # 함께 필요한 사실 슬롯
    # 이 단어가 있으면 이 규칙을 적용하지 않는다.
    # "한도" 같은 일반어를 키워드로 쓰면 다른 주제(세액공제 한도)까지 끌어오므로,
    # 넓은 키워드에는 반드시 배제어를 함께 둔다.
    exclude: tuple[str, ...] = ()


# 순서가 중요하다 — 앞의 규칙이 더 구체적인 주제다.
TOPIC_RULES: list[TopicRule] = [
    TopicRule("퇴직소득세_감면", ("감면", "이연퇴직소득", "실제수령연차"),
              "toejik_gamnyeon", "연금실제수령연차에 따른 이연퇴직소득세 감면율",
              "퇴직소득세_감면율_계산", "이연퇴직소득세 감면 기준"),
    TopicRule("연금수령한도", ("수령한도", "인출한도", "얼마까지 인출", "얼마나 인출",
                            "얼마까지 뽑", "한도"),
              "suryeong_hando", "연금수령한도",
              "연금수령한도_계산", "연금수령한도 산정 방식",
              exclude=("세액공제", "공제한도", "납입한도", "공제 한도")),
    TopicRule("연금수령연차", ("연차", "기산", "2013"),
              "suryeong_yeoncha", "연금수령연차 기산",
              "연금수령연차_계산", "연금수령연차 기산 규칙"),
    TopicRule("세액공제", ("세액공제", "공제한도", "공제 한도", "소득공제"),
              "seaek_gongje", "연금저축·IRP 세액공제",
              "사적연금_납입한도_세액공제_계산", "연금저축·IRP 세액공제 한도"),
    TopicRule("과세방식", ("분리과세", "종합과세", "1500", "1,500", "천오백"),
              "gwase_bangsik", "1,500만원 초과 시 과세방식 선택",
              "과세방식_비교_계산", "연금소득 과세방식 선택 기준"),
    TopicRule("원천징수", ("원천징수", "몇 퍼센트", "몇 %", "세율", "세금 얼마",
                        "얼마나 떼", "떼나요"),
              "wonchen", "연금소득 원천징수세율",
              "사적연금_원천징수_계산", "연령별 연금소득 원천징수세율"),
    TopicRule("퇴직소득세", ("퇴직소득세", "퇴직금 세금", "퇴직급여 세금",
                          "근속연수공제", "환산급여", "퇴직소득 과세"),
              "toejik_se", "퇴직소득세 산출",
              "퇴직소득세_계산", "퇴직소득세 계산 구조"),
    TopicRule("상품_비교", ("총보수", "보수가 낮", "수수료 비교", "어떤 클래스",
                         "비교해", "저렴한"),
              "chongbosu", "가입 가능한 클래스의 총보수 비교",
              "총보수_비교", "판매 클래스별 총보수"),
    TopicRule("가입자격", ("가입자격", "가입 자격", "가입할 수 있", "가입 가능"),
              "gaipjagyeok", "판매 클래스 가입자격",
              "판매클래스_적합성_판정", "종류별 가입자격"),
    TopicRule("중도인출", ("중도인출", "중도 인출", "중도해지", "깨면", "빼면", "꺼내"),
              "jungdo", "중도인출 사유와 세제", None,
              "중도인출 사유와 적용 세율"),
    TopicRule("국민연금", ("국민연금", "노령연금", "본인부담", "출산크레딧"),
              "gukmin", "국민연금", None, "국민연금 제도"),
    TopicRule("명예퇴직", ("명예퇴직", "명퇴"),
              "myeongtoe", "명예퇴직급여 처리", None, "명예퇴직급여와 IRP 이전"),
]

# 주제를 못 잡았을 때 쓰는 일반 사실 슬롯
_FALLBACK_SLOT = ("ilban", "질의 주제에 대한 제공 자료 근거")


def _match_topics(question: str) -> list[TopicRule]:
    hits = [r for r in TOPIC_RULES
            if any(k in question for k in r.keywords)
            and not any(x in question for x in r.exclude)]
    # 같은 계산함수를 두 번 넣지 않는다
    seen: set[str] = set()
    out: list[TopicRule] = []
    for r in hits:
        key = r.calc_function or r.slot_id
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out[:3]      # 슬롯이 너무 많으면 답변이 산만해진다


def rule_based_spec(question: str) -> dict:
    """LLM 없이 질의 스펙을 만든다."""
    conditions = derive_conditions(question)
    topics = _match_topics(question)

    asked_for: list[dict] = []
    planned: list[dict] = []
    plan: list[str] = []

    for r in topics:
        if r.fact_description:
            asked_for.append({
                "id": f"{r.slot_id}_fact",
                "description": r.fact_description,
                "type": "fact", "required": True,
            })
        if r.calc_function:
            asked_for.append({
                "id": f"{r.slot_id}_calc",
                "description": r.description,
                "type": "calculation", "required": True,
                "calc_function": r.calc_function,
            })
            planned.append({"function": r.calc_function, "args": {}})
            plan.append(f"{r.description} — 근거 확인 후 '{r.calc_function}' 실행")
        else:
            plan.append(f"{r.description} — 제공 자료 근거로 설명")

    if not asked_for:
        asked_for.append({
            "id": _FALLBACK_SLOT[0], "description": _FALLBACK_SLOT[1],
            "type": "fact", "required": True,
        })
        plan.append("주제를 특정하지 못해 일반 근거 검색으로 진행")

    entities = {}
    if conditions.get("fund_class"):
        entities["fund_class"] = conditions["fund_class"]
    if conditions.get("account_type"):
        entities["plan_type"] = conditions["account_type"]

    intent = topics[0].intent if topics else "일반"
    return {
        "query": question,
        "intent": intent,
        "entities": entities,
        "asked_for": asked_for,
        "user_conditions": conditions,
        "planned_calls": planned,
        "plan": plan,
        # L1이 실패해도 오타 대응이 사라지지 않도록, 규칙 경로에서도
        # 검색어를 만든다. LLM 없이 편집거리로만 교정하므로 커버리지는
        # 좁지만, 없는 것보다는 낫다(app/analysis/typo.py 참고).
        "search_terms": corrected_terms(question),
        "source": "rule",
    }


# ════════════════════════════════════════════════════════════════
# 화이트리스트 검증
# ════════════════════════════════════════════════════════════════

def sanitize_spec(spec: dict, question: str) -> dict:
    """LLM 산출물을 안전한 형태로 정리한다.

    · 미등록 계산함수 제거 (DEPRECATED는 현행 함수로 교정)
    · asked_for 필수 필드 보정
    · user_conditions를 정규 스키마로 재해석 (단위 변환 포함)

    ⚠️ 여기서 거른 뒤에도 1.5 계층(supervise_plan)이 한 번 더 감사한다.
       중복이 아니라 이중 방어다 — 여기는 형식, 저기는 정합성을 본다.
    """
    out = dict(spec or {})
    out["query"] = question

    cleaned: list[dict] = []
    for item in out.get("asked_for") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        stype = item.get("type", "fact")
        if stype not in ("fact", "calculation", "comparison"):
            stype = "fact"
        entry = {
            "id": str(item["id"]),
            "description": str(item.get("description") or item["id"]),
            "type": stype,
            "required": bool(item.get("required", True)),
        }
        if stype == "calculation":
            fn = remap_function(str(item.get("calc_function") or ""))
            if fn not in CALC_REGISTRY:
                # 미등록 함수 → 계산을 포기하고 사실 슬롯으로 강등
                entry["type"] = "fact"
                entry["description"] += " (등록된 계산함수 없음)"
            else:
                entry["calc_function"] = fn
        cleaned.append(entry)
    out["asked_for"] = cleaned

    planned = []
    for call in out.get("planned_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = remap_function(str(call.get("function") or ""))
        if fn in CALC_REGISTRY:
            planned.append({"function": fn, "args": call.get("args") or {}})
    out["planned_calls"] = planned

    out["user_conditions"] = derive_conditions(question, out.get("user_conditions"))
    out["entities"] = {k: v for k, v in (out.get("entities") or {}).items() if v}
    out["plan"] = [str(p) for p in (out.get("plan") or [])][:6]
    out["search_terms"] = sanitize_search_terms(
        out.get("search_terms"), question)
    return out


# 검색어는 보조 신호다 — 너무 많으면 원 질의를 묻어 버린다
MAX_SEARCH_TERMS = 8


def sanitize_search_terms(terms, question: str) -> list[str]:
    """L1이 만든 검색어를 검증한다. 문제가 있으면 통째로 버린다.

    ━━ 왜 부분 수리가 아니라 전량 폐기인가 ━━
    재작성이 구분해야 할 용어를 뒤바꿨다면(연금수령연차 ↔ 연금실제수령연차),
    그 재작성은 질의를 잘못 이해했다는 뜻이다. 문제가 된 항목만 빼고 나머지를
    쓰면, 같은 오해가 남은 항목에도 스며 있을 수 있다. 원 질의만으로 검색해도
    기존 동의어 확장이 돌아가므로, 의심스러우면 버리는 쪽이 안전하다.
    """
    from app.analysis.vocab import conflates_distinct_terms

    cleaned: list[str] = []
    for t in (terms or []):
        s_ = str(t).strip()
        if 2 <= len(s_) <= 40 and s_ not in cleaned:
            cleaned.append(s_)
    cleaned = cleaned[:MAX_SEARCH_TERMS]

    if conflates_distinct_terms(question, cleaned):
        return []          # 사유는 호출 측에서 trace에 남긴다
    return cleaned


# ════════════════════════════════════════════════════════════════
# LLM 산출물과 규칙 산출물의 조정
# ════════════════════════════════════════════════════════════════

# 이 말이 있으면 **이미 발생한 퇴직소득** 이야기다.
# 관련 제도는 이연퇴직소득세 감면이지, 신규 납입 세액공제가 아니다.
_RETIREMENT_INCOME_SIGNALS = ("명예퇴직", "명퇴", "희망퇴직", "퇴직금",
                              "퇴직급여", "퇴직소득", "이연퇴직")

# 신규 납입 세액공제를 실제로 묻는 말
_TAX_CREDIT_SIGNALS = ("세액공제", "공제한도", "공제 한도", "소득공제",
                       "납입한도", "납입 한도", "연말정산")

MAX_SLOTS = 3      # 슬롯이 많으면 답변이 산만해진다


def reconcile_spec(spec: dict, fallback: dict, question: str) -> dict:
    """LLM 분석과 규칙 분석을 합친다. 도메인 판단은 규칙이 이긴다.

    ━━ 왜 필요한가 (Q-001 실패) ━━
    "명퇴수당을 연금계좌에 넣으면 **세금감면**이…"라는 질의에서 L1이 의도를
    '세액공제'로 잡았다. 명퇴수당은 이미 발생한 퇴직소득이므로 맞는 제도는
    이연퇴직소득세 감면인데, '세금감면'이라는 표현에 이끌려 전혀 다른
    제도로 분류한 것이다.

    더 나쁜 건 구조였다. LLM이 슬롯을 주면 규칙 슬롯을 **통째로 버렸다.**
    그래서 화면에 표시된 실행 계획(퇴직소득세 감면율 계산)과 실제로 실행된
    슬롯(세액공제)이 서로 달랐다. 사용자도 우리도 눈치채기 어려운 형태다.

    CLAUDE.md의 "판단은 코드, 문장은 LLM" 원칙대로, 어떤 제도를 다루는지는
    코드가 정하고 LLM은 그 위에서 문장을 만든다.
    """
    out = dict(spec)
    q = question or ""

    has_retirement = any(k in q for k in _RETIREMENT_INCOME_SIGNALS)
    has_tax_credit = any(k in q for k in _TAX_CREDIT_SIGNALS)

    # ── 1. 제도 오분류 교정 ──────────────────────────────────
    # 퇴직소득 신호는 있는데 세액공제를 명시적으로 묻지 않았다면,
    # LLM이 '세액공제'라고 해도 그 판단은 채택하지 않는다.
    misclassified = (has_retirement and not has_tax_credit
                     and out.get("intent") == "세액공제")
    if misclassified:
        out["intent"] = fallback.get("intent") or "퇴직소득세_감면"
        out["source"] = "llm+rule(제도교정)"

    # ── 2. 규칙이 찾은 슬롯을 잃지 않는다 ────────────────────
    # LLM 슬롯으로 갈아치우지 않고 합친다. 오분류가 확인된 경우에는
    # 규칙 슬롯을 앞에 둬 계산 함수가 먼저 잡히게 한다.
    llm_slots = list(out.get("asked_for") or [])
    rule_slots = list(fallback.get("asked_for") or [])
    seen = {s.get("id") for s in llm_slots}
    missing = [s for s in rule_slots if s.get("id") not in seen]

    if missing:
        merged = (missing + llm_slots) if misclassified else (llm_slots + missing)
        out["asked_for"] = merged[:MAX_SLOTS]
        if not misclassified:
            out["source"] = out.get("source", "llm") + "+rule(슬롯보강)"

    # ── 2-B. 검색어는 두 경로를 합친다 ───────────────────────
    # L1이 만든 검색어(넓은 커버리지)와 규칙 기반 오타 교정(좁지만 확실)은
    # 서로 대체재가 아니라 보완재다. L1 재작성이 폐기됐거나 L1이 아예
    # 실패했더라도 오타 교정은 남아야 한다.
    merged_terms = list(out.get("search_terms") or [])
    for t in (fallback.get("search_terms") or []):
        if t not in merged_terms:
            merged_terms.append(t)
    out["search_terms"] = merged_terms[:MAX_SEARCH_TERMS]

    # ── 3. 계획과 실제 실행을 일치시킨다 ─────────────────────
    # 표시된 계획과 실행 슬롯이 다르면 트레이스를 신뢰할 수 없다.
    have_fn = {s.get("calc_function") for s in out["asked_for"]
               if s.get("calc_function")}
    planned = [c for c in (out.get("planned_calls") or [])
               if c.get("function") in have_fn]
    for s in out["asked_for"]:
        fn = s.get("calc_function")
        if fn and fn not in {c.get("function") for c in planned}:
            planned.append({"function": fn, "args": {}})
    out["planned_calls"] = planned

    return out


# ════════════════════════════════════════════════════════════════
# 진입점
# ════════════════════════════════════════════════════════════════

def make_extract_query_spec(client=None,
                            grounding_hint: str = "",
                            trace_log: Optional[Callable[..., Any]] = None):
    """(question) -> query_spec 시그니처의 함수를 만든다.

    grounding_hint : L0의 as_analysis_hint() 결과.
                     ⚠️ 문서 원문이 아니라 영역·용어 목록만 넣는다.
                        원문을 주면 L1이 이를 근거로 착각해 답변을 만들어낸다.
    """
    from app.llm.clova import get_client
    c = client or get_client()

    def extract_query_spec(question: str) -> dict:
        fallback = rule_based_spec(question)

        user_msg = question
        if grounding_hint:
            user_msg = f"[문서 접지 정보 — 참고용, 근거 아님]\n{grounding_hint}\n\n[질의]\n{question}"

        try:
            out = c.call_with_functions(L1_SYSTEM_PROMPT, user_msg,
                                        QUERY_SPEC_TOOL, purpose="l1_query_spec")
        except Exception as e:      # 호출 실패를 조용히 넘기지 않는다
            # 스키마가 거부된 경우(400)만 축소 스키마로 한 번 더 시도한다.
            # 타임아웃·5xx는 스키마 문제가 아니므로 여기서 또 부르면
            # 지연만 늘린다 — 평가는 단일 GET이라 지연이 곧 점수다.
            retried = False
            if "HTTP 400" in str(e) or "40009" in str(e):
                if trace_log:
                    trace_log("질의분석_스키마_거부",
                              f"전체 스키마 거부({e}) → 축소 스키마로 재시도")
                try:
                    out = c.call_with_functions(
                        L1_SYSTEM_PROMPT, user_msg, MINIMAL_QUERY_SPEC_TOOL,
                        purpose="l1_query_spec_minimal")
                    retried = True
                except Exception as e2:     # noqa: BLE001
                    if trace_log:
                        trace_log("질의분석_LLM_실패",
                                  f"축소 스키마도 실패({e2}) → 규칙 기반 추출로 진행")
                    return fallback
            if not retried:
                if trace_log:
                    trace_log("질의분석_LLM_실패",
                              f"L1 호출 실패({e}) → 규칙 기반 추출로 진행")
                return fallback

        args = (out or {}).get("arguments")
        if not args:
            if trace_log:
                reason = ("mock 클라이언트" if getattr(c, "is_mock", False)
                          else "함수 호출 응답 없음")
                trace_log("질의분석_규칙기반",
                          f"L1이 구조화 결과를 주지 못함({reason}) → 규칙 기반 추출 사용")
            return fallback

        spec = sanitize_spec(args, question)
        spec["source"] = "llm"
        # LLM이 슬롯을 하나도 못 뽑으면 규칙 결과로 보강한다
        if not spec.get("asked_for"):
            spec["asked_for"] = fallback["asked_for"]
            spec["planned_calls"] = fallback["planned_calls"]
            spec["source"] = "llm+rule"
        if not spec.get("plan"):
            spec["plan"] = fallback["plan"]

        # 검색어 재작성이 구분해야 할 용어를 뒤바꿨다면 버렸다는 사실을 남긴다.
        # 조용히 버리면 "왜 검색이 그대로지"를 추적할 수 없다.
        if trace_log and (args.get("search_terms") and not spec.get("search_terms")):
            from app.analysis.vocab import conflates_distinct_terms
            reason = conflates_distinct_terms(
                question, [str(t) for t in (args.get("search_terms") or [])])
            if reason:
                trace_log("검색어_재작성_폐기",
                          f"L1이 만든 검색어가 구분해야 할 용어를 뒤바꿈({reason}) "
                          f"→ 재작성을 버리고 원 질의로만 검색")

        # ⚠️ 도메인 판단은 규칙이 이긴다. LLM이 제도를 잘못 짚어도
        #    (Q-001: 명퇴수당 → '세액공제') 여기서 되돌린다.
        before = spec.get("intent")
        spec = reconcile_spec(spec, fallback, question)
        if trace_log and spec.get("intent") != before:
            trace_log("질의분석_제도교정",
                      f"L1이 의도를 '{before}'로 잡았으나 질의에 퇴직소득 신호가 "
                      f"있어 '{spec.get('intent')}'로 교정 "
                      f"(퇴직소득과 신규 납입 세액공제는 다른 제도)")
        return spec

    return extract_query_spec
