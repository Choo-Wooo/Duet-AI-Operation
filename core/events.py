"""이벤트 버스: 어댑터/오케스트레이터가 발행하고 TUI·로거가 구독한다."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 이벤트 종류
#   human        사람 메시지          {text, n, to}
#   turn_start   턴 시작              {n, kind, info}
#   turn_end     턴 종료              {n, summary, directives, ok}
#   text         에이전트 메시지      {text}
#   tool         도구 호출/명령 시작  {name, detail}
#   tool_output  도구 결과           {text, ok}
#   approval     권한 판정 기록       {summary, tier, decision, by, reason}
#   notice       안내                {text, level}
#   status       상태 변경            {…}
#   usage        비용/토큰            {cost_usd, tokens}
#   error        오류                {text}
#   save         세션 저장            {name, last_n}
#   load         세션 불러오기        {name, last_n}
#   phase        합의 단계 전이        {task_id, from, to}


STATUS_LOG_INTERVAL = 30.0  # 초


@dataclass
class Event:
    kind: str
    role: str | None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self, log_dir: Path | None = None):
        self._subs: list[Callable[[Event], None]] = []
        self._log_file = None
        self._status_sig: int | None = None
        self._status_ts = 0.0
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_dir / (time.strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8")

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subs.append(cb)

    def emit(self, kind: str, role: str | None = None, /, **data: Any) -> Event:
        ev = Event(kind, role, data)
        if self._log_file and self._should_log(ev):
            try:
                self._log_file.write(json.dumps(
                    {"ts": ev.ts, "event": kind, "role": role, "data": data}, ensure_ascii=False, default=str) + "\n")
                self._log_file.flush()
            except Exception:
                pass
        for cb in list(self._subs):
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    def _should_log(self, ev: Event) -> bool:
        """status 는 전체 상태 스냅샷(수십 KB)이라 자주 오면 로그가 수백 MB 로 커진다.
        내용이 바뀌었고 마지막 기록 뒤 STATUS_LOG_INTERVAL 초가 지났을 때만 남긴다."""
        if ev.kind != "status":
            return True
        try:
            sig = hash(json.dumps(ev.data, sort_keys=True, default=str))
        except Exception:
            return True
        if sig == self._status_sig or ev.ts - self._status_ts < STATUS_LOG_INTERVAL:
            return False
        self._status_sig, self._status_ts = sig, ev.ts
        return True

    def close(self) -> None:
        if self._log_file:
            self._log_file.close()
            self._log_file = None
