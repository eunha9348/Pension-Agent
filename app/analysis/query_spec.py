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
# 출력 스키마 — Function calling 을 쓰지 않는다
# ════════════════════════════════════════════════════════════════
#
# ━━ 왜 tools 를 버렸는가 ━━
# HCX-005 는 tools 페이로드를 간헐적으로 거부한다
# (HTTP 400 · code 40009 "Unsupported function"). 진단 3회에서 확정된 사실:
#
#   · tools 가 있는 요청  → 실행마다 다르게 400 (평가 42건 중 약 14%)
#   · tools 가 없는 요청  → 3회 전부 200                    ← 대조군 C1
#
# 즉 오류는 스키마의 특정 요소가 아니라 **tools 라는 기능 자체**에 붙어 있다.
# 전에는 스키마를 줄여 가며 우회하는 사다리(full→reduced→minimal)를 뒀는데,
# 그건 병을 고치는 게 아니라 병을 안고 도는 장치였다. 코드만 복잡해지고
# 400 은 계속 났다.
#
# 대신 **평범한 텍스트 호출로 JSON 을 받는다.** 파서는 이미 있다
# (clova._loads_lenient — ```json 펜스와 앞뒤 설명을 걷어낸다).
# 스키마 강제를 잃지만, 애초에 그 강제는 필요 없었다 —
# sanitize_spec() 과 supervise_plan() 이 결과를 어차피 전수 검증한다.

QUERY_SPEC_KEYS = ("intent", "asked_for", "user_conditions",
                   "extra_conditions", "entities", "search_terms", "plan")

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
   한 글자 차이로 결과가 달라지는 용어는 질문에 있는 쪽을 유지하십시오.

출력 형식 — 아래 JSON 객체 **하나만** 출력하십시오. 설명·머리말을 붙이지 마십시오.

{
  "intent": "질의 의도 (예: 세액공제, 연금수령한도, 과세방식, 상품_비교)",
  "asked_for": [
    {"id": "짧은 식별자", "description": "요구한 내용",
     "type": "fact | calculation | comparison",
     "calc_function": "type이 calculation일 때만, 아래 목록 중 하나"}
  ],
  "user_conditions": {"account_type": "", "age": 0, "pension_year": 0,
    "actual_receipt_year": 0, "service_years": 0,
    "pension_saving_manwon": 0, "irp_manwon": 0, "severance_manwon": 0,
    "account_value_manwon": 0, "total_income_manwon": 0,
    "private_pension_annual_manwon": 0, "fund_class": ""},
  "extra_conditions": {"사용자가 밝힌 그 밖의 사정": "값"},
  "entities": {"product_name": "", "product_code": "",
               "fund_class": "", "plan_type": ""},
  "search_terms": ["문서 용어로 옮긴 검색어"],
  "plan": ["실행 계획 단계"]
}

확인되지 않은 항목은 키를 아예 빼십시오(0이나 빈 문자열로 채우지 마십시오).
calc_function 에 쓸 수 있는 값: {calc_functions}

━━ extra_conditions — 위 목록에 없는 사정도 빠짐없이 담으십시오 ━━
user_conditions는 계산함수가 쓰는 고정 항목이라 목록이 정해져 있습니다.
그런데 사람은 그 목록에 맞춰 말하지 않습니다. 목록에 없는 사정이 나오면
**버리지 말고** extra_conditions에 자유로운 키로 담으십시오. 예:

  "24살이고 부동산은 없고 현금 3,500만원이 있어요. 주택청약은 400만원이고요."
    → user_conditions: {"age": 24}
      extra_conditions: {"보유현금_만원": 3500, "부동산": "없음",
                         "주택청약_만원": 400}

이 정보는 계산에 직접 쓰이지는 않더라도, 무엇을 더 물어야 하는지와
어떤 안내가 이 사람에게 맞는지를 정하는 근거가 됩니다.
**질문에 없는 것을 지어내지는 마십시오.** 사용자가 말한 것만 담습니다."""


def parse_spec_json(raw: str) -> Optional[dict]:
    """L1 응답 텍스트에서 spec JSON 을 회수한다.

    모델은 ```json 펜스나 앞뒤 설명을 붙이곤 한다. 그 처리는 이미
    clova._loads_lenient 에 있으므로 재구현하지 않고 그대로 쓴다.
    """
    from app.llm.clova import _loads_lenient

    parsed = _loads_lenient(raw)
    if not isinstance(parsed, dict):
        return None
    # 우리 스키마의 키가 하나도 없으면 엉뚱한 JSON 이다(예: 모델이 답변을 지어냄)
    if not any(k in parsed for k in QUERY_SPEC_KEYS):
        return None
    return parsed


def l1_system_prompt() -> str:
    """계산함수 목록을 주입한 L1 시스템 프롬프트.

    예전에는 이 목록을 tools 스키마의 enum 으로 강제했다. tools 를 버렸으므로
    프롬프트로 안내하되, 검증은 그대로다 — supervise_plan() 이
    CALC_REGISTRY 화이트리스트로 미등록 함수를 결정론적으로 제거한다.
    """
    return L1_SYSTEM_PROMPT.replace("{calc_functions}",
                                    ", ".join(sorted(CALC_REGISTRY)))


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
    # 계산 슬롯을 만들 조건이 되는 conditions 키. 비어 있으면 **항상** 만든다
    # (기존 동작). 지정하면 아래 셋 중 하나라도 성립할 때만 계산 슬롯을 만들고,
    # 아니면 사실 슬롯만 남긴다.
    #   ① 이 키 중 하나가 conditions에 있다 (계산에 넣을 사용자 값이 있다)
    #   ② 질의가 그 값 자체를 직접 묻는다 ("얼마", "몇 퍼센트" …)
    #
    # ━━ 왜 필요한가 ━━
    # 주제어 하나로 계산을 걸면, 그 단어가 들어갔을 뿐 계산과 무관한 질의까지
    # 계산 결과를 답변 자리에 받는다. 실측 300건에서 43건이 납입액 없이
    # 세액공제 계산으로 라우팅돼 **전부 같은 한도 카드**를 답으로 받았다
    # ("배우자 명의로 납입해도 공제되나요?", "연말정산 서류가 뭔가요?" 까지).
    # 그 43건 중 PASS는 3건뿐이었고, 그 3건은 실제로 한도를 묻는 질의였다.
    #
    # 사실 슬롯은 그대로 남으므로 검색·함정 유도는 영향받지 않는다.
    calc_needs: tuple[str, ...] = ()


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
    # '환급·돌려받다'는 사용자가 세액공제를 부르는 가장 흔한 말이다.
    # 이게 없어서 "IRP에 900만원 넣으면 얼마나 돌려받나요?"가 주제 미매칭으로
    # 떨어져 계산이 아예 안 돌았고, 숫자를 LLM이 지어냈다(300건 감사 A03·A04).
    # 다만 퇴직소득세 환급·투자손실 환급은 전혀 다른 제도라 배제한다.
    TopicRule("세액공제", ("세액공제", "공제한도", "공제 한도", "소득공제",
                        "환급", "돌려받", "돌려 받"),
              "seaek_gongje", "연금저축·IRP 세액공제",
              "사적연금_납입한도_세액공제_계산", "연금저축·IRP 세액공제 한도",
              exclude=("퇴직소득세", "손실"),
              calc_needs=("pension_saving_manwon", "irp_manwon",
                          "combined_contribution_manwon")),
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


# 계산 결과 값 자체를 묻는 표현. 이게 있으면 사용자 값이 없어도 계산한다
# ("세액공제 한도가 **얼마**인가요?" 는 상수라도 그 상수가 곧 답이다).
_VALUE_ASK = ("얼마", "몇 퍼센트", "몇%", "몇 %", "얼마나 되", "계산")


def _wants_calc(rule: TopicRule, question: str, conditions: dict) -> bool:
    """이 주제에서 계산 슬롯을 만들어야 하는가.

    calc_needs가 비어 있으면 기존대로 항상 만든다 — 규칙별로 근거를 확인한
    것만 좁힌다. 근거 없이 전부에 걸면 그게 또 다른 오작동이 된다.
    """
    if not rule.calc_needs:
        return True
    if any(k in conditions for k in rule.calc_needs):
        return True
    return any(a in question for a in _VALUE_ASK)


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
        if r.calc_function and _wants_calc(r, question, conditions):
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
    out["extra_conditions"] = sanitize_extra_conditions(
        out.get("extra_conditions"), out["user_conditions"])
    out["entities"] = {k: v for k, v in (out.get("entities") or {}).items() if v}
    out["plan"] = [str(p) for p in (out.get("plan") or [])][:6]
    out["search_terms"] = sanitize_search_terms(
        out.get("search_terms"), question)
    return out


# 자유 조건은 보조 정보다 — 무한정 받으면 프롬프트를 잠식한다
MAX_EXTRA_CONDITIONS = 12
_EXTRA_KEY_MAX = 40
_EXTRA_VAL_MAX = 120


def sanitize_extra_conditions(extra, user_conditions: dict) -> dict:
    """21종 밖 조건을 안전한 형태로 정리한다.

    ━━ 왜 받는가 ━━
    user_conditions는 계산함수가 쓰는 고정 스키마라 목록이 정해져 있다.
    그런데 사람은 그 목록에 맞춰 말하지 않는다 — "부동산은 없고 현금
    3,500만원, 주택청약 400만원"은 어느 키에도 안 들어간다. 예전에는
    그대로 버려져서, 사용자가 분명히 말한 사정이 답변에 반영되지 않았다.

    ━━ 무엇을 하지 않는가 ━━
    이 값은 **계산함수 인자로 쓰이지 않는다.** 정규 스키마를 우회해
    계산에 들어가면 단위·타입 검증을 통째로 건너뛰게 된다.
    쓰임은 두 가지뿐이다: 답변 생성 시 맥락, 되물을 항목 선정.
    """
    if not isinstance(extra, dict):
        return {}
    known = set(user_conditions or {})
    out: dict[str, str] = {}
    for k, v in extra.items():
        key = str(k).strip()[:_EXTRA_KEY_MAX]
        # 정규 스키마와 겹치는 키는 버린다 — 검증을 거친 쪽이 우선이다
        if not key or key in known:
            continue
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        val = str(v).strip()[:_EXTRA_VAL_MAX]
        if not val:
            continue
        out[key] = val
        if len(out) >= MAX_EXTRA_CONDITIONS:
            break
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


def _cap_keeping_calc(llm_slots: list[dict],
                      rule_slots: list[dict]) -> list[dict]:
    """LLM 슬롯을 우선하되 **규칙이 찾은 계산 슬롯의 자리는 보장한다.**

    ━━ 왜 그냥 자르면 안 되는가 ━━
    규칙 슬롯은 LLM 슬롯 **뒤에** 붙는다. 그래서 L1이 슬롯을 3개만 내놔도
    규칙이 찾아낸 계산 슬롯이 통째로 잘려나갔다. 계산이 안 돌면
    calc_results가 비고, calc_results가 비면 verify_calc_presence는 대조할
    대상이 없어 **무조건 통과**한다. 결국 "함수가 만든 숫자가 사용자에게
    도달하는지"를 보장하는 장치 전체가 조용히 무력화된다
    (평가 E-14, 원천징수세율 3.3%가 답변에서 사라짐).

    역설적이게도 L1이 429로 죽으면 규칙 스펙이 그대로 쓰여 계산이 돌았다.
    LLM이 성공할 때만 숫자가 사라지는 상태였다.

    사실 슬롯 하나를 덜 보여주는 것과 계산을 통째로 건너뛰는 것 중에는
    전자가 낫다. 자리 다툼이 나면 계산 슬롯이 이긴다.

    ⚠️ 이 함수는 **정상 병합 경로 전용**이다. 오분류(misclassified) 경로에는
       쓰지 말 것. 거기서는 "LLM이 고른 계산함수 자체가 이 질의에 맞지 않다"고
       이미 판정한 상태라, 계산 슬롯이라는 이유로 되살리면 판정을 뒤집는다
       (명퇴 질의에 신규 납입 세액공제를 끌어오는 L-01 오답이 그것이다).

    표시 순서는 건드리지 않는다 — 무엇을 남길지만 고르고 원래 순서로 되돌린다.
    """
    merged = llm_slots + rule_slots
    if len(merged) <= MAX_SLOTS:
        return merged
    # 규칙이 찾은 계산 슬롯이 먼저 자리를 잡고, 남는 자리를 원래 순서로 채운다
    kept = [s for s in rule_slots if s.get("calc_function")][:MAX_SLOTS]
    keep = {id(s) for s in kept}
    for s in merged:
        if len(keep) >= MAX_SLOTS:
            break
        keep.add(id(s))
    return [s for s in merged if id(s) in keep]


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
    # ⚠️ id뿐 아니라 calc_function으로도 걸러야 한다. L1이 자기 표현으로 쓴
    # 슬롯("총급여 5천만 원 근로자의 … 환급액", id=s1)과 규칙 슬롯
    # (id=seaek_gongje_calc)이 **같은 계산함수**를 가리키면, id가 달라 이
    # 필터를 통과해 계산 결과가 [조건별 결론]에 두 번 실렸다(300건 감사에서
    # 발견). 규칙 슬롯은 이미 계획 감사·화이트리스트를 통과한 함수만 골라
    # 두므로, LLM이 같은 함수를 이미 물었다면 규칙 쪽은 중복이다.
    llm_calc_fns = {s.get("calc_function") for s in llm_slots if s.get("calc_function")}
    missing = [s for s in rule_slots
              if s.get("id") not in seen
              and not (s.get("calc_function") and s.get("calc_function") in llm_calc_fns)]

    if missing:
        # 오분류 경로는 규칙 슬롯을 앞세우는 것으로 이미 우선순위가 정해져
        # 있다 — 그대로 자른다. 정상 경로에서만 계산 슬롯 자리를 보장한다.
        out["asked_for"] = ((missing + llm_slots)[:MAX_SLOTS] if misclassified
                            else _cap_keeping_calc(llm_slots, missing))
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

        # ── 평범한 텍스트 호출로 JSON 을 받는다 ────────────
        # tools 를 쓰지 않으므로 40009 오류 자체가 발생하지 않는다.
        try:
            raw = c.call(l1_system_prompt(), user_msg,
                         purpose="l1_query_spec", temperature=0.0)
        except Exception as e:      # noqa: BLE001 — 실패를 조용히 넘기지 않는다
            if trace_log:
                trace_log("질의분석_LLM_실패",
                          f"L1 호출 실패({e}) → 규칙 기반 추출로 진행")
            return fallback

        args = parse_spec_json(raw)
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
