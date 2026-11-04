import asyncio
import copy
import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai_analyzer import AIAnalyzer, PROMPT_VERSION
from app.config import ROOT
from app.database import Database
from app.main import create_app
from app.models import Signal
from app.risk_manager import RiskManager, RiskPolicy
from app.worker import AnalysisWorker
from test_ai import envelope, output
from test_receiver import settings, payload, auth
from test_structure import with_structure


@pytest.fixture
def policy():
    return RiskPolicy.load(ROOT / "config/risk-paper.json")


def changed(policy, **updates):
    return RiskPolicy.model_validate({**policy.model_dump(), **updates})


def evaluate(policy, payload, **result_updates):
    signal = Signal.model_validate(payload)
    result = {**output(payload), "decision": signal.side, **result_updates}
    return RiskManager(policy).evaluate(signal, result, now=payload["bar_close_ms"] / 1000 + 1)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_paper_position_math(policy, payload, side):
    data = with_structure(payload, side)
    report = evaluate(policy, data)
    c = report["calculations"]
    assert report["status"] == "passed"
    assert Decimal(c["risk_budget_usd"]) == 50
    assert Decimal(c["loss_usd_per_unit"]) == Decimal("5.50")
    assert Decimal(c["reward_usd_per_unit"]) == Decimal("9.50")
    assert Decimal(c["candidate_quantity"]) == 4
    assert Decimal(c["planned_loss_usd"]) == 22
    assert Decimal(c["notional_usd"]) == 10000
    assert report["mode"] == "paper_simulation"
    assert "live_spread" in report["not_checked"]
    assert c["entry"] == str(float(data["entry"]))


def test_rounds_quantity_down_and_never_up_to_minimum(policy, payload):
    data = with_structure(payload)
    result = evaluate(changed(policy, risk_percent="0.1"), data)
    assert Decimal(result["approved_quantity"]) == Decimal("1.81")
    assert Decimal(result["calculations"]["planned_loss_usd"]) == Decimal("9.9550")
    result = evaluate(changed(policy, risk_percent="0.00001"), data)
    assert result["status"] == "rejected"
    assert result["approved_quantity"] is None
    assert "quantity_below_minimum" in result["reason_codes"]


@pytest.mark.parametrize("update,reason", [
    ({"min_net_reward_risk": "2"}, "net_reward_risk_too_low"),
    ({"min_stop_atr": "2"}, "stop_atr_out_of_range"),
    ({"max_stop_atr": "1.5"}, "stop_atr_out_of_range"),
    ({"spread_price": "1"}, "spread_atr_too_high"),
    ({"price_tick": "3"}, "price_not_on_tick"),
    ({"allowed_symbols": ["OTHER:XAUUSD"]}, "risk_symbol_not_allowed"),
    ({"commission_usd_per_unit_round_trip": "11"}, "net_reward_risk_too_low"),
])
def test_risk_limits_reject(policy, payload, update, reason):
    report = evaluate(changed(policy, **update), with_structure(payload))
    assert report["status"] == "rejected" and reason in report["reason_codes"]
    assert report["approved_quantity"] is None


@pytest.mark.parametrize("update", [
    {"risk_percent": "NaN"}, {"risk_percent": 0}, {"equity_usd": "Infinity"},
    {"quantity_step": 0}, {"max_quantity": "0.001"}, {"min_quantity": "0.015"},
    {"min_stop_atr": 10}, {"mode": "live"}, {"currency": "THB"},
    {"max_notional_equity_multiple": 100}, {"spread_price": -1}, {"max_signal_age_seconds": True},
])
def test_invalid_policy_fails_closed(policy, update):
    with pytest.raises(ValidationError):
        changed(policy, **update)


def test_structural_gate_and_ai_price_changes(policy, payload):
    assert evaluate(policy, payload)["status"] == "rejected"
    data = with_structure(payload)
    assert "ai_changed_prices_or_rr" in evaluate(policy, data, entry=2501)["reason_codes"]
    assert "invalid_ai_decision" in evaluate(policy, data, decision="SELL")["reason_codes"]


@pytest.mark.parametrize("age,reason", [(301, "stale_signal"), (-31, "future_signal")])
def test_freshness(policy, payload, age, reason):
    data = with_structure(payload)
    report = RiskManager(policy).evaluate(Signal.model_validate(data), output(data), now=data["bar_close_ms"] / 1000 + age)
    assert reason in report["reason_codes"]


def test_policy_snapshot_hash_changes(policy, payload):
    data = with_structure(payload)
    original = evaluate(policy, data)
    revised = evaluate(changed(policy, risk_percent="0.1"), data)
    assert original["policy_sha256"] != revised["policy_sha256"]
    assert original["policy"]["equity_usd"] == "10000"


def process(settings, data, decision):
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(data))
    result = {**output(data), "decision": decision}
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope(result)))) as client:
            await AnalysisWorker(db, AIAnalyzer(settings, client), settings).process_once()
    asyncio.run(scenario())
    return db, record_id


def test_ai_buy_and_passed_risk_produce_effective_buy(settings, payload):
    db, record_id = process(settings, with_structure(payload), "BUY")
    assert db.get_analysis(record_id)["effective_decision"] == "BUY"
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/signals/{record_id}/risk").status_code == 401
        report = client.get(f"/signals/{record_id}/risk", headers=auth(settings.api_token)).json()
        assert report["status"] == "passed"
        assert Decimal(report["approved_quantity"]) == 4


def test_ai_buy_cannot_override_rejected_risk(settings, payload, policy, tmp_path):
    path = tmp_path / "risk.json"
    path.write_text(changed(policy, min_net_reward_risk="2").model_dump_json())
    db, record_id = process(replace(settings, risk_policy_path=path), with_structure(payload), "BUY")
    report = db.get_analysis(record_id)
    assert report["ai_decision"] == "BUY"
    assert report["risk_status"] == "rejected"
    assert report["effective_decision"] == "WAIT"


def test_ai_wait_stays_wait_after_risk_pass(settings, payload):
    db, record_id = process(settings, with_structure(payload), "WAIT")
    report = db.get_analysis(record_id)
    assert report["risk_status"] == "passed"
    assert report["effective_decision"] == "WAIT"


def test_historical_approval_expires_on_read(settings, payload, monkeypatch):
    data = with_structure(payload)
    db, record_id = process(settings, data, "BUY")
    monkeypatch.setattr("app.database.time.time", lambda: data["bar_close_ms"] / 1000 + 301)
    report = db.get_analysis(record_id)
    assert report["risk_status"] == "expired"
    assert report["effective_decision"] == "WAIT"
    assert db.get_risk(record_id)["status"] == "passed"  # historical audit retained


def test_old_completion_without_risk_cannot_confirm(settings, payload):
    data = with_structure(payload)
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(data))
    job = db.claim_analysis(settings.lm_studio_model, PROMPT_VERSION, 90, 3, 300)
    db.finish_analysis(job, "completed", {**output(data), "decision": "BUY"}, None)
    assert db.get_analysis(record_id)["effective_decision"] == "WAIT"
    assert db.get_analysis(record_id)["risk_status"] == "not_evaluated"


def test_old_lease_cannot_overwrite_risk(settings, payload, policy):
    data = with_structure(payload)
    db = Database(settings.database_path)
    db.initialize()
    record_id, _ = db.insert(Signal.model_validate(data))
    args = (settings.lm_studio_model, PROMPT_VERSION, 90, 3, 300)
    old = db.claim_analysis(*args)
    with db.connect() as conn:
        conn.execute("UPDATE analyses SET lease_until=0")
    new = db.claim_analysis(*args)
    report = evaluate(policy, data)
    assert db.finish_analysis(new, "completed", output(data), None, risk_report=report)
    tampered = copy.deepcopy(report)
    tampered["approved_quantity"] = "999"
    assert not db.finish_analysis(old, "completed", output(data), None, risk_report=tampered)
    assert db.get_risk(record_id)["approved_quantity"] == report["approved_quantity"]


def test_additive_risk_migration_keeps_analysis(settings, payload):
    db, record_id = process(settings, payload, "WAIT")
    with db.connect() as conn:
        conn.execute("DROP TABLE risk_evaluations")
        conn.execute("PRAGMA user_version=1")
    db.initialize()
    assert db.get_analysis(record_id)["status"] == "completed"
    assert db.get_analysis(record_id)["risk_status"] == "not_evaluated"


def test_missing_policy_prevents_startup(settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        with TestClient(create_app(replace(settings, risk_policy_path=tmp_path / "missing.json"))):
            pass


def test_policy_change_requires_new_evaluation(settings, payload, policy, tmp_path):
    db, record_id = process(settings, with_structure(payload), "BUY")
    path = tmp_path / "stricter-risk.json"
    path.write_text(changed(policy, risk_percent="0.1").model_dump_json())
    with TestClient(create_app(replace(settings, risk_policy_path=path))) as client:
        result = client.get(f"/signals/{record_id}/analysis", headers=auth(settings.api_token)).json()
        assert result["risk_status"] == "policy_changed"
        assert result["effective_decision"] == "WAIT"
        assert result["confirmed_quantity"] is None
        audit = client.get(f"/signals/{record_id}/risk", headers=auth(settings.api_token)).json()
        assert audit["status"] == "passed" and audit["current_status"] == "policy_changed"
