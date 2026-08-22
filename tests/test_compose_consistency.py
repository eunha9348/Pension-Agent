"""docker-compose 빌드 인자 일관성.

━━ 실사고 ━━
compose의 서비스 6종이 전부 같은 이미지 태그(pension-agent:latest)를 만든다.
그런데 WITH_EMBEDDING 인자가 agent·embed 에만 있었다. 그래서

    WITH_EMBEDDING=true docker compose build agent embed eval

를 실행하면 **eval 이 마지막에 WITH_EMBEDDING=false 로 빌드되어 앞의 두 개를
덮어썼다.** torch 없는 이미지가 만들어졌고, 임베딩은 예외를 삼키고
BM25 단독으로 축퇴했다 — 죽지 않으니 알아채기까지 오래 걸렸다.

이 종류의 사고는 눈으로 리뷰해서 막기 어렵다. 인자가 하나 빠진 것을
YAML 200줄 안에서 알아보기는 힘들기 때문이다. 그래서 검사로 고정한다.
"""

from __future__ import annotations

import pathlib

import pytest

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"


def _load() -> dict:
    yaml = pytest.importorskip("yaml", reason="PyYAML 없이는 검사할 수 없다")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _building_services(cfg: dict) -> dict[str, dict]:
    return {name: svc for name, svc in (cfg.get("services") or {}).items()
            if isinstance(svc, dict) and "build" in svc}


def test_같은_태그를_만드는_서비스는_빌드인자가_같다():
    """다르면 마지막 빌드가 앞의 것을 조용히 덮어쓴다."""
    cfg = _load()
    building = _building_services(cfg)
    assert building, "build 블록이 있는 서비스가 하나도 없다"

    by_image: dict[str, dict[str, dict]] = {}
    for name, svc in building.items():
        by_image.setdefault(svc.get("image", name), {})[name] = (
            (svc.get("build") or {}).get("args") or {})

    for image, services in by_image.items():
        if len(services) < 2:
            continue
        distinct = {frozenset(args.items()) for args in services.values()}
        assert len(distinct) == 1, (
            f"'{image}' 를 만드는 서비스들의 build args 가 다르다 → "
            f"마지막 빌드가 앞의 것을 덮어쓴다.\n"
            + "\n".join(f"  {n}: {a}" for n, a in services.items()))


def test_모든_빌드_서비스가_WITH_EMBEDDING을_받는다():
    """하나라도 빠지면 torch 없는 이미지가 만들어질 수 있다."""
    cfg = _load()
    missing = [name for name, svc in _building_services(cfg).items()
               if "WITH_EMBEDDING" not in ((svc.get("build") or {}).get("args") or {})]
    assert not missing, f"WITH_EMBEDDING 인자가 없는 서비스: {missing}"


def test_임베딩을_쓰는_서비스는_모델_캐시를_마운트한다():
    """캐시가 없으면 컨테이너를 지울 때마다 수백 MB를 다시 받는다."""
    cfg = _load()
    need = {"agent", "embed", "eval"}
    for name in need:
        svc = (cfg.get("services") or {}).get(name)
        if not svc:
            continue
        mounts = " ".join(svc.get("volumes") or [])
        assert "/app/data/models" in mounts, f"{name} 에 모델 캐시 볼륨이 없다"
