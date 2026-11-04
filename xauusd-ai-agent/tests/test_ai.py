import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai_analyzer import AIAnalyzer, AnalysisError, PROMPT_VERSION
from app.database import Database
from app.main import create_app
from app.models import Signal
from app.worker import AnalysisWorker
from test_receiver import settings, payload, auth


def output(payload):
    return {"decision": "WAIT", **{k: payload[k] for k in ("entry", "sl", "tp", "risk_reward")},
            "reasons": ["Market structure and confirmed support/resistance are missing."]}


def envelope(result, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(result), "reasoning_content": "ignored"}}]}


def execute(settings, payload, response):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            return await AIAnalyzer(settings, client).analyze(Signal.model_validate(payload))
    return asyncio.run(scenario())


def test_valid_result_and_schema_request(settings, payload):
    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["model"] == settings.lm_studio_model
        assert "market_structure" in body["messages"][1]["content"]
        return httpx.Response(200, json=envelope(output(payload)))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await AIAnalyzer(settings, client).analyze(Signal.model_validate(payload))
            assert result.decision == "WAIT"
            assert result.entry == payload["entry"]
    asyncio.run(scenario())


@pytest.mark.parametrize("effort", [None, "none"])
def test_reasoning_setting_preserves_schema_and_price_validation(settings, payload, effort):
    config = replace(settings, ai_reasoning_effort=effort)
    def respond(request):
        body = json.loads(request.content)
        if effort is None:
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == effort
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["max_tokens"] == 2048
        return httpx.Response(200, json=envelope(output(payload)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await AIAnalyzer(config,client).analyze(Signal.model_validate(payload))
            assert result.decision == "WAIT" and result.entry == payload["entry"]
    asyncio.run(run())


@pytest.mark.parametrize("change,code", [
    ({"entry": 2501}, "modified_prices_or_rr"),
    ({"risk_reward": 3}, "modified_prices_or_rr"),
    ({"decision": "SELL"}, "reversed_signal"),
    ({"decision": "BUY"}, "unsupported_confirmation"),
    ({"entry": "2500"}, "invalid_output"),
    ({"reasons": []}, "invalid_output"),
    ({"invented": 1}, "invalid_output"),
])
def test_invalid_outputs(settings, payload, change, code):
    result = {**output(payload), **change}
    with pytest.raises(AnalysisError, match=code):
        execute(settings, payload, httpx.Response(200, json=envelope(result)))


@pytest.mark.parametrize("body,code", [
    ({"choices": []}, "invalid_output"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "incomplete_response"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": None, "reasoning_content": "{}"}}]}, "invalid_output"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{}]}}]}, "unexpected_tool_call"),
])
def test_bad_envelopes(settings, payload, body, code):
    with pytest.raises(AnalysisError, match=code):
        execute(settings, payload, httpx.Response(200, json=body))


@pytest.mark.parametrize("status,retryable", [(500, True), (429, True), (401, False), (404, False)])
def test_http_errors(settings, payload, status, retryable):
    with pytest.raises(AnalysisError) as error:
        execute(settings, payload, httpx.Response(status))
    assert error.value.retryable == retryable


def test_response_size_limit(settings, payload):
    with pytest.raises(AnalysisError, match="response_too_large"):
        execute(settings, payload, httpx.Response(200, content=b"x" * 131073))


def test_timeout(settings, payload):
    def respond(request):
        raise httpx.ReadTimeout("private response detail")
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(AnalysisError, match="model_timeout"):
                await AIAnalyzer(settings, client).analyze(Signal.model_validate(payload))
    asyncio.run(scenario())


def test_durable_queue_and_result(settings, payload):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    db = Database(settings.database_path)
    db.initialize()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope(output(payload))))) as client:
            worker = AnalysisWorker(db, AIAnalyzer(settings, client), settings)
            assert await worker.process_once()
            assert not await worker.process_once()
    asyncio.run(scenario())
    result = db.get_analysis(record_id)
    assert result["status"] == "completed" and result["attempts"] == 1
    assert result["result"] == output(payload)
    assert result["prompt_version"] == PROMPT_VERSION
    assert "ignored" not in json.dumps(result)
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/signals/{record_id}/analysis").status_code == 401
        assert client.get(f"/signals/{record_id}/analysis", headers=auth(settings.api_token)).json() == result


def test_retry_exhaustion(settings, payload):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            worker = AnalysisWorker(db, AIAnalyzer(settings, client), settings)
            for attempt in range(3):
                assert await worker.process_once()
                assert db.get_analysis(record_id)["status"] == ("pending" if attempt < 2 else "failed")
                if attempt < 2:
                    assert not await worker.process_once()  # backoff is respected
                    with db.connect() as conn:
                        conn.execute("UPDATE analyses SET next_attempt_at=0")
            assert not await worker.process_once()
    asyncio.run(scenario())
    assert db.get_analysis(record_id)["effective_decision"] == "WAIT"


def test_lease_recovery_and_old_worker_cannot_overwrite(settings, payload):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    args = (settings.lm_studio_model, PROMPT_VERSION, 90, 3, 300)
    first = db.claim_analysis(*args)
    assert db.claim_analysis(*args) is None
    with db.connect() as conn:
        conn.execute("UPDATE analyses SET lease_until=0")
    second = db.claim_analysis(*args)
    assert second["attempt"] == 2
    assert not db.finish_analysis(first, "completed", output(payload), None)
    assert db.finish_analysis(second, "completed", output(payload), None)
    assert db.get_analysis(record_id)["attempts"] == 2


def test_expired_queue_not_sent(settings, payload):
    payload["bar_close_ms"] -= 3600000
    payload["h1"]["bar_close_ms"] -= 3600000
    payload["signal_id"] = f'OANDA:XAUUSD:5:{payload["bar_close_ms"]}:BUY'
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    assert db.claim_analysis(settings.lm_studio_model, PROMPT_VERSION, 90, 3, 300) is None
    assert db.get_analysis(record_id)["status"] == "expired"


def test_result_expires_during_inference(settings, payload, monkeypatch):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    def respond(request):
        monkeypatch.setattr("app.worker.time.time", lambda: payload["bar_close_ms"] / 1000 + 301)
        return httpx.Response(200, json=envelope(output(payload)))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await AnalysisWorker(db, AIAnalyzer(settings, client), settings).process_once()
    asyncio.run(scenario())
    assert db.get_analysis(record_id)["status"] == "expired"
    assert db.get_analysis(record_id)["result"] is None


def test_additive_migration_keeps_existing_signal(settings, payload):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(payload))
    with db.connect() as conn:
        conn.execute("DROP TABLE analyses")
        conn.execute("PRAGMA user_version=0")
    db.initialize()
    assert db.get_signal(record_id)["payload"]["entry"] == payload["entry"]
    assert db.get_analysis(record_id)["status"] == "pending"


def test_ai_worker_does_not_block_webhook(settings, payload, monkeypatch):
    async def slow(self, signal):
        await asyncio.sleep(10)
    monkeypatch.setattr(AIAnalyzer, "analyze", slow)
    with TestClient(create_app(replace(settings, ai_enabled=True))) as client:
        started = time.monotonic()
        response = client.post("/webhook", headers=auth(settings.webhook_token), json=payload)
        assert response.status_code == 201
        assert time.monotonic() - started < 1
        assert client.get("/health").json()["ai_worker"] == "running"
    # Lifespan must cancel outstanding inference rather than wait ten seconds.
    assert time.monotonic() - started < 3
