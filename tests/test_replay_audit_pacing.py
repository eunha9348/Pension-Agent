"""replay_audit_300.py의 429 페이싱 — 실측(2026-09-05)으로 추가.

298건을 텀 없이 쏘다가 앞쪽 몇십 건만 정상이고 나머지는 전부 429로
결정론적 폴백(degradation_total 420건)만 돌았다. tests/eval_set.py가
42문항에서 이미 겪은 것과 같은 사고이자 같은 해법이므로, 여기서도
같은 불변식을 고정한다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "replay_audit_300", Path(__file__).resolve().parent.parent / "scripts" / "replay_audit_300.py")
replay = importlib.util.module_from_spec(_SPEC)
sys.modules["replay_audit_300"] = replay
_SPEC.loader.exec_module(replay)


def _reset():
    replay._pace_state["sec"] = replay.PACING_SEC


def test_429가_아니면_페이싱이_늘지_않는다():
    _reset()
    replay._note_response("정상 처리, 근거 3건")
    assert replay._pace_state["sec"] == replay.PACING_SEC


def test_429_흔적이_있으면_페이싱이_늘어난다():
    _reset()
    before = replay._pace_state["sec"]
    replay._note_response("L1 호출 실패(HTTP 429: rate limited) → 규칙 기반 추출로 진행")
    assert replay._pace_state["sec"] > before


def test_페이싱은_상한을_넘지_않는다():
    _reset()
    for _ in range(30):
        replay._note_response("HTTP 429")
    assert replay._pace_state["sec"] == replay.PACING_MAX


def test_ask는_응답의_429_흔적을_페이싱에_반영한다(monkeypatch):
    """배선 테스트 — ask()가 _note_response를 실제로 호출하는지 확인한다.
    (부품만 따로 테스트하면 ask() 안에서 호출을 빼먹어도 위 테스트들은
    계속 통과한다 — 배선을 지나가는 테스트를 최소 1건 둔다.)
    """
    _reset()

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({
                "question_id": "X1", "question": "q", "answer": "a",
                "retrieved_context": "", "think_trace": "HTTP 429 발생",
            }).encode("utf-8")

    monkeypatch.setattr(replay.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(replay, "_wait_pace", lambda: None)  # 테스트 속도

    before = replay._pace_state["sec"]
    replay.ask("http://무관", "X1", "q", 5.0)
    assert replay._pace_state["sec"] > before
