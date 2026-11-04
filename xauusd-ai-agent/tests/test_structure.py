import copy

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.ai_analyzer import AnalysisError
from app.models import Signal
from app.main import create_app
from app.structure import confirmation_blockers
from test_receiver import settings, payload
from test_ai import execute, envelope, output


def with_structure(payload, side="BUY"):
    data = copy.deepcopy(payload)
    data.update(schema_version=2, strategy_version="0.2", side=side)
    data["signal_id"] = f'OANDA:XAUUSD:5:{data["bar_close_ms"]}:{side}:0.2'
    def point(price, age):
        return {"price": price, "age_bars": age, "active": True,
                "confirmed_at_ms": data["bar_close_ms"] - age * 300000,
                "pivot_close_ms": data["bar_close_ms"] - (age + 3) * 300000}
    if side == "BUY":
        high, old_high, low, old_low = 2512, 2508, 2497, 2490
    else:
        data.update(sl=2505, tp=2490)
        data["h1"].update(trend="bearish", close=2502, ema_fast=2510, ema_slow=2520)
        data["m5"].update(ema=2501, rsi=45, plus_di=15, minus_di=28)
        high, old_high, low, old_low = 2503, 2510, 2488, 2492
    data["context"]["market_structure"] = {
        "timeframe": "5", "method": "confirmed_pivots", "left_bars": 3, "right_bars": 3,
        "max_age_bars": 120, "state": "bullish" if side == "BUY" else "bearish",
        "recent_high": point(high, 10), "previous_high": point(old_high, 30),
        "recent_low": point(low, 5), "previous_low": point(old_low, 40),
    }
    return data


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_complete_evidence_can_confirm(settings, payload, side):
    data = with_structure(payload, side)
    assert confirmation_blockers(Signal.model_validate(data)) == []
    result = {**output(data), "decision": side, "reasons": ["Confirmed pivots align and levels allow the proposed trade."]}
    assert execute(settings, data, httpx.Response(200, json=envelope(result))).decision == side


def test_wait_allowed_with_complete_evidence(settings, payload):
    data = with_structure(payload)
    assert execute(settings, data, httpx.Response(200, json=envelope(output(data)))).decision == "WAIT"


@pytest.mark.parametrize("case", ["missing", "stale", "ranging", "opposite", "broken", "target", "stop", "equal_level"])
def test_insufficient_evidence_cannot_confirm(settings, payload, case):
    data = with_structure(payload)
    structure = data["context"]["market_structure"]
    if case == "missing":
        structure["previous_low"] = None
        structure["state"] = "insufficient"
    elif case == "stale":
        point = structure["previous_low"]
        point.update(age_bars=121, confirmed_at_ms=data["bar_close_ms"] - 121 * 300000,
                     pivot_close_ms=data["bar_close_ms"] - 124 * 300000)
        structure["state"] = "insufficient"
    elif case == "ranging":
        structure["recent_high"]["price"] = structure["previous_high"]["price"]
        structure["state"] = "ranging"
    elif case == "opposite":
        structure["previous_high"]["price"] = 2520
        structure["previous_low"]["price"] = 2499
        structure["state"] = "bearish"
    elif case == "broken":
        structure["recent_low"]["active"] = False
    elif case == "target":
        structure["recent_high"]["price"] = 2509
    elif case == "stop":
        structure["recent_low"]["price"] = 2494
    else:
        structure["recent_low"]["price"] = data["entry"]
    assert confirmation_blockers(Signal.model_validate(data))
    result = {**output(data), "decision": "BUY"}
    with pytest.raises(AnalysisError, match="unsupported_confirmation"):
        execute(settings, data, httpx.Response(200, json=envelope(result)))


@pytest.mark.parametrize("case", ["false_state", "too_early", "future", "bad_age", "unordered", "broken_active", "wrong_version", "old_id"])
def test_invalid_pivot_payload_rejected(payload, case):
    data = with_structure(payload)
    structure = data["context"]["market_structure"]
    point = structure["recent_low"]
    if case == "false_state":
        structure["state"] = "bearish"
    elif case == "too_early":
        point["pivot_close_ms"] = point["confirmed_at_ms"] - 300000
    elif case == "future":
        point["confirmed_at_ms"] = data["bar_close_ms"] + 300000
    elif case == "bad_age":
        point["age_bars"] = 0
    elif case == "unordered":
        structure["previous_low"] = copy.deepcopy(point)
    elif case == "broken_active":
        structure["recent_low"]["price"] = 2501
    elif case == "wrong_version":
        data["schema_version"] = 1
    else:
        data["signal_id"] = data["signal_id"].removesuffix(":0.2")
    with pytest.raises(ValidationError):
        Signal.model_validate(data)


def test_v1_still_accepted_but_cannot_confirm(payload):
    assert confirmation_blockers(Signal.model_validate(payload)) == ["missing_structure"]


def test_v2_ingestion_and_queue_roundtrip(settings, payload):
    data = with_structure(payload)
    with TestClient(create_app(settings)) as client:
        response = client.post('/webhook', json=data, headers={'Authorization': 'Bearer ' + settings.webhook_token})
        assert response.status_code == 201
        record_id = response.json()['id']
        headers = {'Authorization': 'Bearer ' + settings.api_token}
        stored = client.get(f'/signals/{record_id}', headers=headers).json()['payload']
        assert stored['context']['market_structure'] == data['context']['market_structure']
        assert client.get(f'/signals/{record_id}/analysis', headers=headers).json()['status'] == 'pending'
        # Old and new alert streams cannot collide at the same timestamp.
        old = client.post('/webhook', json=payload, headers={'Authorization': 'Bearer ' + settings.webhook_token})
        assert old.status_code == 201 and old.json()['id'] != record_id
