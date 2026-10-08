"""Typed decisions never act on malformed answers or unbounded API calls."""
import asyncio
import copy
import json
import math

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from duet.core import jev
from duet.core.jev import JevClient, choice_confidence


QUESTIONS = {
    "route": {"type": "choice", "instructions": "Does the task need another attempt or review?",
              "criteria": {"continue": "An unfinished next step is identified.",
                           "review": "The assigned change is ready for review."}},
    "within_scope": {"type": "noul", "instructions": "Is the operation within the stated task?",
                     "criteria": {"true": "The task explicitly includes this operation.",
                                  "false": "The operation is unrelated or authorization is missing."}},
}


def response():
    return {"model": "jev-1.13.0", "answers": {
        "route": {"type": "choice", "choice": "review", "probabilities": {"continue": 0.03, "review": 0.97},
                  "confidence": 0.94},
        "within_scope": {"type": "noul", "noul": 0.95},
    }, "usage": {"input_tokens": 400, "output_tokens": 25}}


async def with_server(monkeypatch, handler, operation):
    app = web.Application()
    app.router.add_post("/v1/systemone", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(jev, "API_URL", str(server.make_url("/v1/systemone")))
    try:
        return await operation()
    finally:
        await server.close()


def test_wire_payload_and_typed_answers(monkeypatch):
    requests = []

    async def handler(request):
        requests.append((request.headers.get("Authorization"), request.content_type, await request.json()))
        return web.json_response(response())

    async def run():
        client = JevClient(" secret-key ")
        result = await with_server(monkeypatch, handler, lambda: client.evaluate({"task": "구현 완료"}, QUESTIONS))
        assert result.ok and result.error is None
        assert result.answers["route"]["choice"] == "review"
        assert result.answers["route"]["confidence"] == pytest.approx(0.94)
        assert result.answers["within_scope"] == {"type": "noul", "noul": 0.95}
        assert result.input_tokens == 400 and result.output_tokens == 25 and result.latency_ms >= 0
        assert requests == [("Bearer secret-key", "application/json", {
            "model": "jev-1.13.0", "state": {"task": "구현 완료"}, "questions": QUESTIONS})]
    asyncio.run(run())


@pytest.mark.parametrize("corrupt", [
    lambda data: data["answers"].pop("within_scope"),
    lambda data: data["answers"].update(unrequested={"type": "noul", "noul": 1}),
    lambda data: data["answers"]["route"].update(type="noul"),
    lambda data: data["answers"]["route"].update(choice="invented"),
    lambda data: data["answers"]["route"].update(choice="continue"),
    lambda data: data["answers"]["route"].update(probabilities={"continue": 0.03, "invented": 0.97}),
    lambda data: data["answers"]["route"].update(probabilities={"continue": 0.4, "review": 0.97}),
    lambda data: data["answers"]["route"]["probabilities"].update(review=math.nan),
    lambda data: data["answers"]["route"]["probabilities"].update(review=math.inf),
    lambda data: data["answers"]["route"].update(confidence=True),
    lambda data: data["answers"]["route"].pop("confidence"),
    lambda data: data["answers"]["within_scope"].update(noul="0.99"),
    lambda data: data["answers"]["within_scope"].update(noul=-0.1),
    lambda data: data["answers"]["within_scope"].update(noul=True),
    lambda data: data["usage"].update(input_tokens=-1),
    lambda data: data["usage"].update(input_tokens=True),
    lambda data: data.pop("usage"),
    lambda data: data.update(model=None),
    lambda data: data.update(model="sensitive request echo"),
])
def test_malformed_answers_abstain(monkeypatch, corrupt):
    data = response()
    corrupt(data)

    async def handler(request):
        return web.Response(text=json.dumps(data), content_type="application/json")

    async def run():
        result = await with_server(monkeypatch, handler, lambda: JevClient("secret").evaluate({}, QUESTIONS))
        assert not result.ok and result.error == "invalid_response" and result.answers == {}
    asyncio.run(run())


def test_confidence_cannot_be_inflated_by_server(monkeypatch):
    data = response()
    data["answers"]["route"].update(probabilities={"continue": 0.45, "review": 0.55}, confidence=1)

    async def handler(request):
        return web.json_response(data)

    async def run():
        result = await with_server(monkeypatch, handler, lambda: JevClient("secret").evaluate({}, QUESTIONS))
        assert result.ok and result.answers["route"]["confidence"] == pytest.approx(0.1)
    asyncio.run(run())


def test_confidence_matches_documented_formula():
    assert choice_confidence({"a": 0.6, "b": 0.3, "c": 0.1}) == pytest.approx(0.4)
    assert choice_confidence({"a": 0.5, "b": 0.5}) == 0


@pytest.mark.parametrize("status,error", [(401, "authentication_error"), (403, "authentication_error"),
                                          (422, "request_rejected"), (302, "request_rejected")])
def test_auth_validation_and_redirect_errors_are_not_retried_or_exposed(monkeypatch, status, error):
    calls = []

    async def handler(request):
        calls.append(1)
        return web.Response(status=status, text="secret-key PRIVATE state contents",
                            headers={"Location": "https://example.invalid/collect"})

    async def run():
        result = await with_server(monkeypatch, handler, lambda: JevClient("secret-key").evaluate({}, QUESTIONS))
        assert result.error == error and len(calls) == 1 and not result.ok
        assert "secret-key" not in repr(result) and "PRIVATE" not in repr(result)
    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 503, 529])
def test_transient_error_retries_once(monkeypatch, status):
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return web.Response(status=status, headers={"Retry-After": "0"})
        return web.json_response(response())

    async def run():
        result = await with_server(monkeypatch, handler, lambda: JevClient("secret").evaluate({}, QUESTIONS))
        assert result.ok and len(calls) == 2
    asyncio.run(run())


def test_retry_after_is_bounded_by_total_deadline(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": "600"})

    async def run():
        result = await with_server(monkeypatch, handler,
                                   lambda: JevClient("secret", timeout_sec=0.03).evaluate({}, QUESTIONS))
        assert not result.ok and result.error == "timeout" and len(calls) == 1
        assert result.latency_ms < 1000
    asyncio.run(run())


def test_timeout_and_cancellation(monkeypatch):
    async def slow(self, payload):
        await asyncio.Event().wait()

    monkeypatch.setattr(JevClient, "_evaluate", slow)

    async def run():
        result = await JevClient("secret", timeout_sec=0.01).evaluate({}, QUESTIONS)
        assert not result.ok and result.error == "timeout"
        pending = asyncio.create_task(JevClient("secret").evaluate({}, QUESTIONS))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    asyncio.run(run())


def test_network_errors_are_sanitized(monkeypatch):
    def fail(*args, **kwargs):
        raise aiohttp.ClientConnectionError("secret-key from private URL")

    monkeypatch.setattr(aiohttp.ClientSession, "post", fail)

    async def run():
        result = await JevClient("secret-key", max_retries=0).evaluate({}, QUESTIONS)
        assert result.error == "network_error" and "secret-key" not in repr(result)
    asyncio.run(run())


@pytest.mark.parametrize("text", ["not json", "x" * (jev._MAX_RESPONSE_BYTES + 1)], ids=["non-json", "oversized"])
def test_invalid_and_oversized_body_abstains(monkeypatch, text):
    async def handler(request):
        return web.Response(text=text)

    async def run():
        result = await with_server(monkeypatch, handler, lambda: JevClient("secret").evaluate({}, QUESTIONS))
        assert result.error == "invalid_response" and not result.ok
    asyncio.run(run())


def test_invalid_input_and_missing_key_do_not_call_network(monkeypatch):
    async def unexpected(*args):
        raise AssertionError("must not access the network")

    monkeypatch.setattr(JevClient, "_evaluate", unexpected)

    async def run():
        assert (await JevClient(None).evaluate({}, QUESTIONS)).error == "missing_api_key"
        assert (await JevClient("secret", model="private data").evaluate({}, QUESTIONS)).error == "invalid_request"
        assert (await JevClient("secret").evaluate({"nan": math.nan}, QUESTIONS)).error == "invalid_request"
        malformed = copy.deepcopy(QUESTIONS)
        malformed["route"]["criteria"] = {"only_one": "no decision"}
        assert (await JevClient("secret").evaluate({}, malformed)).error == "invalid_request"
        assert (await JevClient("secret").evaluate({}, {})).error == "invalid_request"
        assert (await JevClient("secret").evaluate("x" * jev._MAX_REQUEST_BYTES, QUESTIONS)).error == "invalid_request"
    asyncio.run(run())


@pytest.mark.parametrize("credential", ["bad\nheader", "bad\x00header", "nonascii-키", "x" * 4097],
                         ids=["newline", "control-character", "non-ascii", "oversized"])
def test_invalid_credentials_never_reach_http_or_error_messages(monkeypatch, credential):
    async def unexpected(*args):
        raise AssertionError("invalid credentials must not access the network")

    monkeypatch.setattr(JevClient, "_evaluate", unexpected)

    async def run():
        client = JevClient(credential)
        result = await client.evaluate({}, QUESTIONS)
        assert not result.ok and result.error == "invalid_api_key"
        assert credential not in repr(result)
        check = await client.test_connection()
        assert not check["ok"] and credential not in check["message"]
    asyncio.run(run())


def test_connection_probe_uses_a_small_literal_question(monkeypatch):
    seen = []

    async def handler(request):
        data = await request.json()
        seen.append(data)
        return web.json_response({"model": "jev-1.13.0", "answers": {
            "connection_check": {"type": "choice", "choice": "passed",
                                 "probabilities": {"passed": 1.0, "failed": 0.0}, "confidence": 1.0}},
            "usage": {"input_tokens": 60, "output_tokens": 4}})

    async def run():
        result = await with_server(monkeypatch, handler, JevClient("secret").test_connection)
        assert result["ok"] and "연결 성공" in result["message"] and "secret" not in result["message"]
        assert len(seen[0]["questions"]) == 1 and len(json.dumps(seen[0])) < 1000
    asyncio.run(run())
