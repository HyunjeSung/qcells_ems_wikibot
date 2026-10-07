"""위키봇 답변 파이프라인 실행 추적(작업대 화면용).

답변 하나가 만들어지는 동안 거치는 단계(검색어 분석 → Rovo 검색 → 인물/Jira 보강 → 답변 생성 →
모델 비교 판정 → 저장)를 이벤트로 남긴다. 이벤트는 두 군데로 간다:
  1. emit 콜백 — /api/chat/stream이 NDJSON 한 줄씩 브라우저로 흘려보내 작업대가 실시간 갱신된다.
  2. self.events — 끝나면 runs 테이블에 통째로 저장돼 "기록" 화면/과거 대화에서 그대로 재생된다.

프론트는 같은 key의 step 이벤트를 하나의 행으로 합친다(running → done/skip/error 순으로 덮어씀).
"""

import time
from contextlib import contextmanager


class _StepInfo:
    """with tracer.step(...) as st: 블록 안에서 결과를 채워 넣는 그릇."""

    def __init__(self):
        self.detail = ""
        self.items = None
        self.status = None  # None이면 정상 종료 시 "done"


class Tracer:
    def __init__(self, emit=None):
        self.events = []
        self._emit = emit
        self._t0 = time.monotonic()

    def elapsed_ms(self):
        return int((time.monotonic() - self._t0) * 1000)

    def push(self, event):
        event = dict(event)
        event["t"] = self.elapsed_ms()
        self.events.append(event)
        if self._emit:
            try:
                self._emit(event)
            except Exception:
                # 브라우저가 연결을 끊어도 파이프라인 자체는 끝까지 돌아 기록을 남겨야 한다.
                pass

    @contextmanager
    def step(self, key, label):
        started = time.monotonic()
        self.push({"type": "step", "key": key, "label": label, "status": "running"})
        info = _StepInfo()
        try:
            yield info
        except Exception as e:
            self.push({
                "type": "step", "key": key, "label": label, "status": "error",
                "detail": str(e)[:300], "ms": int((time.monotonic() - started) * 1000),
            })
            raise
        self.push({
            "type": "step", "key": key, "label": label, "status": info.status or "done",
            "detail": info.detail, "items": info.items, "ms": int((time.monotonic() - started) * 1000),
        })

    def note(self, key, label, detail="", status="done", items=None, ms=None):
        """이미 끝난(또는 건너뛴) 단계를 한 번에 기록한다."""
        self.push({
            "type": "step", "key": key, "label": label, "status": status,
            "detail": detail, "items": items, "ms": ms,
        })
