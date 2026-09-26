"""Antigravity CLI(agy) PreToolUse 훅 → duet 권한 정책 연결 (표준 라이브러리만 사용).

duet 이 띄운 agy 에서만 동작한다 (환경변수 DUET_AGY_BRIDGE 가 있을 때).
그 밖의 agy 실행(사람이 직접 쓰는 agy)에서는 아무 것도 출력하지 않아 agy 기본 동작을 그대로 둔다.

agy 헤드리스 모드는 훅의 allow 를 무시하지만 deny 는 지킨다. 그래서 duet 은 agy 를
--dangerously-skip-permissions 로 띄우고, 이 훅이 duet 정책에서 거부된 도구만 막는다.
"""
from __future__ import annotations

import json
import os
import socket
import sys


def main() -> int:
    bridge = os.environ.get("DUET_AGY_BRIDGE")
    if not bridge:
        return 0  # duet 밖의 agy: 개입하지 않음
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    req = {"token": os.environ.get("DUET_AGY_TOKEN", ""), "payload": payload}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(float(os.environ.get("DUET_AGY_HOOK_TIMEOUT", "3500")))
            s.connect(bridge)
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        answer = json.loads(buf.decode() or "{}")
    except Exception as e:  # duet 과 연결이 끊겼으면 안전하게 거부
        answer = {"decision": "deny", "reason": f"duet 권한 확인에 실패했습니다: {e}"}
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
