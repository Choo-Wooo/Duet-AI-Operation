"""Small asynchronous client for TypeSafe's typed decisions API.

The transport owns validation, deadlines and retries; callers own prompts,
confidence thresholds and the actions a decision is allowed to cause.
Protocol: https://docs.typesafe.ai/api
Confidence: https://docs.typesafe.ai/confidence
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
_MAX_REQUEST_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_RETRYABLE_STATUSES = frozenset((429, 500, 502, 503, 504, 529))
_MODEL_ID = re.compile(r"jev-[A-Za-z0-9._-]{1,80}")


@dataclass
class JevResult:
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answers)


def _probability(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return float(value)


def _usage(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid usage")
    return value


def choice_confidence(probabilities: dict[str, float]) -> float:
    """TypeSafe's Choice confidence, not the winning option's probability."""
    count = len(probabilities)
    if count < 2:
        raise ValueError("a choice needs at least two options")
    return max(0.0, min(1.0, (max(probabilities.values()) - 1 / count) / (1 - 1 / count)))


def _validate_questions(questions: Any) -> None:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 32:
        raise ValueError("invalid questions")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or not isinstance(question, dict):
            raise ValueError("invalid question")
        if not isinstance(question.get("instructions"), (str, dict, list)) or not question["instructions"]:
            raise ValueError("missing instructions")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 32:
                raise ValueError("invalid choice criteria")
            if any(not isinstance(option, str) or not option for option in criteria):
                raise ValueError("invalid choice key")
        elif kind == "noul":
            if criteria is not None and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"}):
                raise ValueError("invalid noul criteria")
        else:
            raise ValueError("unsupported question type")


def _parse_response(data: Any, questions: dict[str, dict]) -> JevResult:
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    model, answers, usage = data.get("model"), data.get("answers"), data.get("usage")
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        raise ValueError("missing model")
    if not isinstance(answers, dict) or set(answers) != set(questions) or not isinstance(usage, dict):
        raise ValueError("invalid response fields")
    parsed = {}
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise ValueError("invalid answer type")
        if question["type"] == "noul":
            parsed[key] = {"type": "noul", "noul": _probability(answer.get("noul"))}
            continue
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise ValueError("invalid choice options")
        probabilities = {option: _probability(value) for option, value in probabilities.items()}
        if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-5):
            raise ValueError("invalid probability sum")
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in probabilities:
            raise ValueError("unknown choice")
        if probabilities[choice] < max(probabilities.values()) - 1e-9:
            raise ValueError("choice is not the most probable option")
        # Check the documented field, then derive it ourselves: a malformed
        # confidence must never bypass a caller's threshold.
        _probability(answer.get("confidence"))
        parsed[key] = {"type": "choice", "choice": choice, "probabilities": probabilities,
                       "confidence": choice_confidence(probabilities)}
    return JevResult(answers=parsed, model=model, input_tokens=_usage(usage.get("input_tokens")),
                     output_tokens=_usage(usage.get("output_tokens")))


def _http_error(status: int) -> str:
    if status in (401, 403):
        return "authentication_error"
    if status == 429:
        return "rate_limited"
    if status in _RETRYABLE_STATUSES:
        return "service_unavailable"
    return "request_rejected"


class JevClient:
    """A call's deadline includes all HTTP attempts and retry delays.

    Each call creates and closes its session, so live configuration changes
    cannot leave old keys in a shared connection or require shutdown hooks.
    Error values are fixed codes: API response bodies and exception messages
    can contain request data and must never reach application logs or the UI.
    """

    def __init__(self, api_key: str | None, *, model: str = DEFAULT_MODEL,
                 timeout_sec: float = 8.0, max_retries: int = 1):
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.model = model
        if type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive and finite")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be between zero and two")
        self.timeout_sec = float(timeout_sec)
        self.max_retries = max_retries

    async def evaluate(self, state: Any, questions: dict[str, dict]) -> JevResult:
        started = time.monotonic()
        if not self._api_key:
            return JevResult(error="missing_api_key")
        if len(self._api_key) > 4096 or any(not 33 <= ord(c) <= 126 for c in self._api_key):
            return JevResult(error="invalid_api_key")
        try:
            _validate_questions(questions)
            if (not isinstance(state, (str, dict, list)) or not isinstance(self.model, str)
                    or not _MODEL_ID.fullmatch(self.model)):
                raise ValueError("invalid input")
            # Snapshot the payload before yielding; reject non-JSON/non-finite
            # data instead of silently altering what the caller wants judged.
            payload = json.dumps({"model": self.model, "state": state, "questions": questions},
                                 ensure_ascii=False, allow_nan=False)
            if len(payload.encode("utf-8")) > _MAX_REQUEST_BYTES:
                raise ValueError("input too large")
            snapshot = json.loads(payload)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return JevResult(error="invalid_request")
        try:
            result = await asyncio.wait_for(self._evaluate(snapshot), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            result = JevResult(error="timeout")
        # CancelledError is deliberately not caught: stop/pause must remain
        # responsive even while a judgment or retry is in flight.
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    async def _evaluate(self, payload: dict) -> JevResult:
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec, sock_connect=min(5.0, self.timeout_sec))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for attempt in range(self.max_retries + 1):
                delay = 0.25 * (2 ** attempt)
                try:
                    async with session.post(API_URL, json=payload,
                                            headers={"Authorization": f"Bearer {self._api_key}"},
                                            allow_redirects=False) as response:
                        if response.status != 200:
                            error = _http_error(response.status)
                            if response.status not in _RETRYABLE_STATUSES or attempt == self.max_retries:
                                return JevResult(error=error)
                            retry_after = response.headers.get("Retry-After", "")
                            try:
                                seconds = float(retry_after)
                                if math.isfinite(seconds) and seconds >= 0:
                                    # The outer deadline bounds even a long server delay.
                                    delay = max(delay, seconds)
                            except (TypeError, ValueError, OverflowError):
                                pass
                        else:
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(16384):
                                body.extend(chunk)
                                if len(body) > _MAX_RESPONSE_BYTES:
                                    return JevResult(error="invalid_response")
                            try:
                                return _parse_response(json.loads(body), payload["questions"])
                            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                                return JevResult(error="invalid_response")
                except asyncio.TimeoutError:
                    if attempt == self.max_retries:
                        return JevResult(error="timeout")
                except (aiohttp.ClientError, OSError):
                    if attempt == self.max_retries:
                        return JevResult(error="network_error")
                await asyncio.sleep(delay)
        return JevResult(error="service_unavailable")

    async def test_connection(self) -> dict[str, Any]:
        """A tiny real decision checks credentials and response compatibility."""
        result = await self.evaluate(
            {"event": "The automated test suite finished successfully."},
            {"connection_check": {"type": "choice", "instructions": "What happened to the test suite in state.event?",
                                  "criteria": {"passed": "The test suite finished successfully.",
                                               "failed": "The test suite reported a failure."}}},
        )
        if result.ok:
            return {"ok": True, "message": f"JEV 연결 성공 · {result.model} · 입력 {result.input_tokens} 토큰"}
        messages = {
            "missing_api_key": "API 키를 먼저 입력하세요.",
            "invalid_api_key": "API 키 형식이 올바르지 않습니다. 공백 없는 영문 키를 다시 입력하세요.",
            "authentication_error": "API 키 인증에 실패했습니다. 키와 계정 상태를 확인하세요.",
            "rate_limited": "호출 한도에 도달했습니다. 잠시 후 다시 시도하세요.",
            "service_unavailable": "JEV 서비스가 일시적으로 응답하지 않습니다.",
            "timeout": "JEV 연결 시간이 초과되었습니다.",
            "network_error": "JEV 서버에 연결할 수 없습니다.",
            "invalid_response": "JEV 응답 형식이 올바르지 않습니다.",
            "invalid_request": "JEV 요청 설정이 올바르지 않습니다.",
            "request_rejected": "JEV가 요청을 거절했습니다. 모델 설정과 계정 상태를 확인하세요.",
        }
        return {"ok": False, "message": messages.get(result.error, "JEV 연결에 실패했습니다.")}
