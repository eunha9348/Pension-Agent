"""청크 임베딩 생성 — 인덱스와 분리된 별도 단계.

    python -m app.ingest.build_embeddings              # 증분 (권장)
    python -m app.ingest.build_embeddings --limit 200  # 일부만 (시험용)
    python -m app.ingest.build_embeddings --rebuild    # 전부 다시

━━ 왜 build_index와 분리했나 ━━
CLOVA 임베딩 API는 텍스트 1건당 1회 호출이다. 8,195청크면 호출도 8,195회고,
시간과 크레딧이 모두 든다. 문서를 하나 고칠 때마다 이걸 다시 돌리면 안 되므로
인제스트와 떼어 놓고, 청크 본문 해시가 같으면 기존 벡터를 재사용한다.

━━ 중단해도 안전하다 ━━
일정 간격으로 중간 저장하므로, Ctrl+C로 끊거나 네트워크가 끊겨도
다시 실행하면 남은 것부터 이어서 만든다. 처음부터 다시 하지 않는다.
"""

from __future__ import annotations

import argparse
import sys
import time

from app.config import get_settings
from app.ingest.store import DEFAULT_INDEX_DIR, get_store
from app.ingest.vector_store import VectorStore, text_hash
from app.retrieval.embedding import EmbeddingError, RateLimitError, embed_one

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="청크 임베딩 생성 (CLOVA Studio)")
    ap.add_argument("--index", default=str(DEFAULT_INDEX_DIR))
    ap.add_argument("--limit", type=int, default=0, help="이번 실행에서 만들 최대 개수")
    ap.add_argument("--rebuild", action="store_true", help="기존 벡터를 무시하고 전부 다시")
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

    if args.limit:
        todo = todo[:args.limit]
        print(f" (--limit 적용 — 이번 실행은 {len(todo)}건만)")
    if not todo:
        removed = vs.prune({c.chunk_id for c in chunks})
        if removed:
            vs.save(args.index)
            print(f" 사라진 청크의 벡터 {removed}건 정리 후 저장했습니다.")
        print("\n✅ 이미 최신입니다. 새로 만들 것이 없습니다.")
        return 0

    print()
    t0 = time.time()
    done = failed = 0
    rate_limited = False
    try:
        for i, c in enumerate(todo, 1):
            try:
                vec = embed_one(c.text[:MAX_CHARS])
                vs.add(c.chunk_id, vec, text_hash(c.text))
                done += 1
            except RateLimitError as e:
                # embed_one이 이미 몇십 초씩 여러 번 쉬며 재시도한 뒤에도
                # 안 풀린 것이다 — 계정 전체 한도에 걸렸을 가능성이 높다.
                # 여기서 계속 밀어붙이면 남은 수천 건도 똑같이 실패하며
                # 시간만 태운다. 실패로 세지 않고 바로 멈춘다.
                rate_limited = True
                print(f"\n⏸  {c.chunk_id}: {e}")
                print("   호출 속도 제한이 계속 걸립니다. 잠시 후(수 분) 다시 "
                      "실행하면 여기부터 이어서 만듭니다.")
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
                time.sleep(PACING_SEC)     # 다음 호출까지 예방적으로 쉰다

            if i % CHECKPOINT_EVERY == 0:
                vs.save(args.index)
                rate = i / max(time.time() - t0, 1e-6)
                left = (len(todo) - i) / max(rate, 1e-6)
                print(f"  · {i}/{len(todo)}  ({rate:.1f}건/초, 남은 시간 약 {left/60:.1f}분)")
    except KeyboardInterrupt:
        print("\n⏸  중단 요청 — 여기까지 저장합니다. 다시 실행하면 이어서 만듭니다.")

    vs.prune({c.chunk_id for c in chunks})
    out = vs.save(args.index)

    print()
    print(f" 생성 {done}건 · 실패 {failed}건 · 총 보유 {len(vs)}건 (차원 {vs.dim})")
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
