"""청크 임베딩 생성 — 인덱스와 분리된 별도 단계.

    python -m app.ingest.build_embeddings              # 증분 (권장)
    python -m app.ingest.build_embeddings --pace 1.0   # 429가 잦으면 더 천천히
    python -m app.ingest.build_embeddings --limit 200  # 일부만 (시험용)
    python -m app.ingest.build_embeddings --rebuild    # 전부 다시

━━ 왜 build_index와 분리했나 ━━
CLOVA 임베딩 API는 텍스트 1건당 1회 호출이다. 8,195청크면 호출도 8,195회고,
시간과 크레딧이 모두 든다. 문서를 하나 고칠 때마다 이걸 다시 돌리면 안 되므로
인제스트와 떼어 놓고, 청크 본문 해시가 같으면 기존 벡터를 재사용한다.

━━ 같은 본문은 한 번만 호출한다 ━━
제공 자료는 투자설명서 158건이라 세액공제 조항 같은 문구가 **글자까지 똑같이**
반복된다. 같은 텍스트는 임베딩도 같으므로, 본문을 정규화해 묶고 대표 1건만
호출한 뒤 나머지에는 그 벡터를 복사한다. 호출 수가 그만큼 줄고, 429(속도 제한)에
걸릴 일도 줄어든다.

━━ 429는 벤더를 바꿔서 풀지 않는다 ━━
대회 절대 제약이 "LLM은 HyperCLOVA X만 사용"이고, 위반하면 평가 대상에서
제외된다. 임베딩 사용은 허용받았지만 그건 기법에 대한 허용이지 타사 모델
허용이 아니다. 속도 제한은 호출 수를 줄이고(위) 간격을 늘려서(--pace) 푼다.

━━ 중단해도 안전하다 ━━
일정 간격으로 중간 저장하므로, Ctrl+C로 끊거나 네트워크가 끊겨도
다시 실행하면 남은 것부터 이어서 만든다. 처음부터 다시 하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time

from app.config import get_settings
from app.ingest.store import DEFAULT_INDEX_DIR, get_store
from app.ingest.vector_store import VectorStore, text_hash
from app.retrieval.embedding import (EmbeddingError, RateLimitError, embed_one,
                                     rate_limit_seen)

# 이 간격마다 중간 저장한다. 너무 잦으면 느리고, 너무 뜸하면 중단 시 손해가 크다.
CHECKPOINT_EVERY = 100

# 임베딩 입력 길이 상한(문자). bge-m3는 8,192토큰까지 받지만 청크가 그보다
# 훨씬 짧고, 지나치게 긴 입력은 요금과 지연만 늘린다.
MAX_CHARS = 4000

# 요청 사이 기본 텀(초). CLOVA 임베딩은 초당 호출 제한이 있어서, 쉬지 않고
# 쏘면 거의 다 429(Too Many Requests)로 튕긴다 — 실제로 8,195건 중 대다수가
# 이렇게 실패한 적이 있다. embed_one이 429를 만나면 알아서 몇 초씩 쉬며
# 재시도하지만, 애초에 이 텀만큼 예방적으로 쉬면 429 자체가 크게 줄어든다.
PACING_SEC = 0.3

# 429를 만날 때마다 텀을 이 배수로 늘린다(자동 조절).
# 계정마다 한도가 다르고 공개된 수치도 없어서, 고정값보다 이렇게 스스로
# 맞춰 가는 편이 확실하다. 한 번 늘린 텀은 그 실행 동안 유지한다.
PACING_GROWTH = 1.8
PACING_MAX = 5.0


def _text_key(text: str) -> str:
    """본문 정규화 해시 — 공백 차이만 있는 텍스트를 같은 것으로 본다."""
    return hashlib.sha1(
        re.sub(r'\s+', ' ', (text or "")).strip().encode("utf-8")).hexdigest()


# 근중복 판정 임계값(토큰 집합 Jaccard).
NEAR_DUP_THRESHOLD = 0.90

# 길이 버킷 폭(문자). 근중복은 길이가 비슷하므로 이걸로 비교 대상을 좁힌다.
_LEN_BUCKET = 120

# 한 버킷에서 비교할 최대 개수 — 병적으로 큰 버킷에서 O(n²)이 터지지 않게
_MAX_BUCKET = 400


def group_near_duplicates(chunks: list) -> list[list]:
    """같거나 사실상 같은 본문끼리 묶는다. 묶인 것들은 벡터를 공유한다.

    ━━ 왜 완전 일치만으로는 부족한가 ━━
    반복되는 조항이라도 글자까지 같지는 않다. 같은 세액공제 조항에도 중간에
    펀드명("키움더드림단기채증권투자신탁[채권]")과 페이지 번호가 끼어 있어
    해시가 달라진다. 실제 코퍼스 8,075건에서 완전 일치로는 **한 건도**
    묶이지 않았다.

    ━━ 잘못 묶으면 검색이 조용히 틀려진다 ━━
    이 도메인은 작은 차이가 전부다 — "연금저축 600만원"과 "IRP 900만원"은
    문장이 90% 같아도 완전히 다른 사실이다. 두 청크를 묶으면 같은 벡터를
    쓰게 되어 의미 검색이 둘을 구분하지 못한다. 그래서 유사도만 보지 않고
    **등장하는 수치 집합이 완전히 같을 때만** 묶는다. 수치가 하나라도
    다르면 아무리 문장이 비슷해도 따로 호출한다.

    비교 대상은 길이 버킷으로 좁힌다(근중복은 길이가 비슷하다).
    """
    from app.core.numeric_verifier import extract_numbers
    from app.retrieval.rerank import jaccard
    from app.retrieval.tokenize import content_terms

    terms_of: dict[str, set[str]] = {}
    nums_of: dict[str, frozenset] = {}
    buckets: dict[int, list] = {}
    for c in chunks:
        terms_of[c.chunk_id] = content_terms(c.text)
        nums_of[c.chunk_id] = frozenset(extract_numbers(c.text, include_trivial=True))
        buckets.setdefault(len(c.text) // _LEN_BUCKET, []).append(c)

    def _mergeable(a, b) -> bool:
        # 수치가 다르면 절대 묶지 않는다 (600 ↔ 900 사고 방지)
        if nums_of[a.chunk_id] != nums_of[b.chunk_id]:
            return False
        return jaccard(terms_of[a.chunk_id], terms_of[b.chunk_id]) >= NEAR_DUP_THRESHOLD

    groups: list[list] = []
    for key in sorted(buckets):
        # 경계에 걸친 근중복을 놓치지 않도록 인접 버킷도 함께 본다
        pending = buckets[key] + [c for c in buckets.get(key + 1, [])
                                  if len(c.text) % _LEN_BUCKET < _LEN_BUCKET // 4]
        pending = pending[:_MAX_BUCKET] + []
        claimed: set[str] = set()
        for i, head in enumerate(pending):
            if head.chunk_id in claimed:
                continue
            same = [head]
            claimed.add(head.chunk_id)
            for other in pending[i + 1:]:
                if other.chunk_id in claimed:
                    continue
                if _mergeable(head, other):
                    same.append(other)
                    claimed.add(other.chunk_id)
            groups.append(same)
        # 버킷 상한을 넘겨 잘린 것들은 개별 처리
        for c in buckets[key][_MAX_BUCKET:]:
            if c.chunk_id not in claimed:
                groups.append([c])

    # 인접 버킷에서 중복 편입된 청크 정리 — 한 청크는 한 묶음에만 속해야 한다
    seen: set[str] = set()
    cleaned: list[list] = []
    for g in groups:
        members = [c for c in g if c.chunk_id not in seen]
        if not members:
            continue
        seen.update(c.chunk_id for c in members)
        cleaned.append(members)
    return cleaned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="청크 임베딩 생성 (CLOVA Studio)")
    ap.add_argument("--index", default=str(DEFAULT_INDEX_DIR))
    ap.add_argument("--limit", type=int, default=0, help="이번 실행에서 만들 최대 개수")
    ap.add_argument("--rebuild", action="store_true", help="기존 벡터를 무시하고 전부 다시")
    ap.add_argument("--pace", type=float, default=PACING_SEC,
                    help=f"요청 사이 대기(초). 429가 잦으면 늘리십시오 "
                         f"(기본 {PACING_SEC}, 429를 만나면 자동으로도 늘어남)")
    ap.add_argument("--dry-run", action="store_true",
                    help="API를 호출하지 않고 '몇 회 호출이 필요한지'만 즉시 계산")
    args = ap.parse_args(argv)

    s = get_settings()
    print("═" * 62)
    print(" 청크 임베딩 생성 — CLOVA Studio")
    print("═" * 62)
    print(f" endpoint : {s.clova_embedding_endpoint}")
    print(f" API KEY  : {'설정됨' if s.clova_api_key else '★ 비어 있음 ★'}")
    print(f" USE_EMBEDDING : {s.use_embedding}")
    print()

    if not s.clova_api_key:
        print("❌ CLOVA_API_KEY 가 없습니다. .env 를 먼저 채우십시오.")
        return 1
    if not s.use_embedding:
        print("⚠️  USE_EMBEDDING=false 입니다. 벡터는 만들어 두되, 검색에서는")
        print("   쓰이지 않습니다. 쓰려면 .env 에서 USE_EMBEDDING=true 로 바꾸십시오.")
        print()

    store = get_store()
    chunks = store.all_chunks()
    if not chunks:
        print("❌ 인덱스가 비어 있습니다. 먼저 build_index 를 실행하십시오.")
        return 1

    vs = VectorStore() if args.rebuild else VectorStore.load(args.index)
    if vs.model and vs.model != s.clova_embedding_endpoint:
        print(f"⚠️  기존 벡터는 다른 엔드포인트({vs.model})로 만든 것입니다.")
        print("   모델이 바뀌었다면 --rebuild 로 다시 만드십시오.")
    vs.model = s.clova_embedding_endpoint

    todo = [c for c in chunks if args.rebuild or vs.needs_embedding(c.chunk_id, c.text)]
    print(f" 전체 청크 {len(chunks)}건 · 기존 벡터 {len(vs)}건 · 생성 대상 {len(todo)}건")

    # ── 사실상 같은 본문끼리 묶는다 ──────────────────────────
    # 투자설명서 158건에 같은 조항이 반복되므로, 실제 호출해야 할 고유 본문은
    # 청크 수보다 적다. 대표 1건만 호출하고 나머지는 벡터를 복사한다.
    print(" 근중복 묶는 중...", flush=True)
    grouped = group_near_duplicates(todo)
    member_of: dict[str, list] = {g[0].chunk_id: g for g in grouped}

    saved = len(todo) - len(grouped)
    if saved > 0:
        print(f" 같은 본문 묶음 — 실제 호출 {len(grouped)}회 "
              f"(중복 {saved}건은 벡터 복사, 호출 {saved/max(len(todo),1)*100:.0f}% 절감)")
    else:
        print(" 근중복 없음 — 청크마다 따로 호출해야 합니다")

    reps = [g[0] for g in grouped]
    if args.limit:
        reps = reps[:args.limit]
        print(f" (--limit 적용 — 이번 실행은 {len(reps)}회 호출만)")

    if args.dry_run:
        # 2시간을 쓰기 전에 '쓸 가치가 있는지'부터 알려 준다.
        for pace_try in (0.3, 1.0, 2.0):
            est = len(reps) * (pace_try + 0.35) / 60      # 0.35초 ≈ 호출 자체 지연
            print(f"   --pace {pace_try:<4} → 약 {est:.0f}분 (429 없을 때)")
        print("\n dry-run 이므로 API를 호출하지 않았습니다.")
        return 0

    if not todo:
        removed = vs.prune({c.chunk_id for c in chunks})
        if removed:
            vs.save(args.index)
            print(f" 사라진 청크의 벡터 {removed}건 정리 후 저장했습니다.")
        print("\n✅ 이미 최신입니다. 새로 만들 것이 없습니다.")
        return 0

    print(f" 요청 간격 {args.pace:.1f}초로 시작 "
          f"(429가 나면 자동으로 늘립니다)")
    print()

    t0 = time.time()
    done = copied = failed = 0
    rate_limited = False
    pace = max(args.pace, 0.0)
    try:
        for i, c in enumerate(reps, 1):
            try:
                vec = embed_one(c.text[:MAX_CHARS])
                # 대표에게 받은 벡터를 같은 묶음 전체에 복사한다
                for member in member_of[c.chunk_id]:
                    vs.add(member.chunk_id, vec, text_hash(member.text))
                    if member.chunk_id != c.chunk_id:
                        copied += 1
                done += 1
            except RateLimitError as e:
                # embed_one이 이미 몇십 초씩 여러 번 쉬며 재시도한 뒤에도
                # 안 풀린 것이다 — 계정 전체 한도에 걸렸을 가능성이 높다.
                # 여기서 계속 밀어붙이면 남은 수천 건도 똑같이 실패하며
                # 시간만 태운다. 실패로 세지 않고 바로 멈춘다.
                rate_limited = True
                print(f"\n⏸  {c.chunk_id}: {e}")
                print(f"   요청 간격을 {pace:.1f}초까지 늘려도 계속 걸립니다.")
                print(f"   몇 분 뒤 `--pace {min(pace * 2, 10):.0f}` 로 다시 "
                      f"실행하면 여기부터 이어서 만듭니다.")
                break
            except EmbeddingError as e:
                failed += 1
                print(f"  ❌ {c.chunk_id}: {e}")
                # 인증 오류라면 남은 8천 건도 전부 실패한다 — 즉시 멈춘다.
                if "401" in str(e) or "40104" in str(e) or "403" in str(e):
                    print("\n❌ 인증 오류로 보입니다. 키/엔드포인트를 확인하십시오.")
                    break
                if failed > 20 and failed > done:
                    print("\n❌ 실패가 너무 많아 중단합니다.")
                    break
            finally:
                # 429를 한 번이라도 겪었으면 간격을 늘려 둔다.
                # 계정별 한도를 모르니 스스로 맞춰 가는 편이 확실하다.
                if rate_limit_seen():
                    new_pace = min(pace * PACING_GROWTH, PACING_MAX)
                    if new_pace > pace:
                        print(f"  ⏱  429 감지 — 요청 간격을 "
                              f"{pace:.1f}→{new_pace:.1f}초로 늘립니다")
                        pace = new_pace
                time.sleep(pace)

            if i % CHECKPOINT_EVERY == 0:
                vs.save(args.index)
                rate = i / max(time.time() - t0, 1e-6)
                left = (len(reps) - i) / max(rate, 1e-6)
                print(f"  · {i}/{len(reps)}회  ({rate:.1f}회/초, "
                      f"남은 시간 약 {left/60:.1f}분)")
    except KeyboardInterrupt:
        print("\n⏸  중단 요청 — 여기까지 저장합니다. 다시 실행하면 이어서 만듭니다.")

    vs.prune({c.chunk_id for c in chunks})
    out = vs.save(args.index)

    print()
    print(f" 호출 {done}회 · 벡터 복사 {copied}건 · 실패 {failed}건")
    print(f" 총 보유 {len(vs)}/{len(chunks)}건 (차원 {vs.dim})")
    print(f" 저장 → {out}")
    if rate_limited:
        print(" ⏸  호출 속도 제한으로 중간에 멈췄습니다. 같은 명령을 다시 실행하면")
        print("    이어서 만듭니다(이미 만든 것은 다시 안 만듭니다).")
    elif failed:
        print(" ⚠️  실패분은 다시 실행하면 재시도합니다.")
    if len(vs) >= len(chunks) and not rate_limited:
        print("\n✅ 완료. .env 의 USE_EMBEDDING=true 로 두고 서버를 재시작하십시오.")
    return 0 if (done or not failed) and not rate_limited else 1


if __name__ == "__main__":
    sys.exit(main())
