"""Explicit live LM Studio/Codex check with synthetic data and disposable DB."""
import json
import sys
import tempfile
import time
import argparse
import hashlib
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import ROOT, Settings
from app.main import create_app
from app.risk_manager import RiskPolicy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider',choices=('lm_studio','codex'),default='lm_studio')
    variants = parser.add_mutually_exclusive_group()
    variants.add_argument("--v2", action="store_true", help="Test complete synthetic pivot evidence")
    variants.add_argument("--xm", action="store_true", help="Test synthetic XM schema 3 with XM risk policy")
    parser.add_argument("--report", type=Path, help="Save the non-secret integration result as JSON")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="xauusd-ai-check-") as directory:
        config = replace(Settings.from_env(), database_path=Path(directory) / "test.db", ai_enabled=True,
                         ai_provider=args.provider,
                         ai_max_attempts=1, ai_max_signal_age_seconds=600)
        if args.xm:
            config = replace(config, allowed_symbols=("XM:XAUUSD",), risk_policy_path=ROOT / "config/risk-xm-paper.json")
        # Isolated integration test: allow inference across a 5-minute boundary.
        policy = RiskPolicy.load(config.risk_policy_path)
        policy_data = policy.model_dump(mode="json")
        policy_data["max_signal_age_seconds"] = 600
        test_policy_path = Path(directory) / "risk-test.json"
        test_policy_path.write_text(RiskPolicy.model_validate(policy_data).model_dump_json())
        config = replace(config, risk_policy_path=test_policy_path)
        structured = args.v2 or args.xm
        payload = json.loads((ROOT / "examples" / ("signal-v2.json" if structured else "signal.json")).read_text())
        close_ms = int(time.time() // 300) * 300_000
        time_shift = close_ms - payload["bar_close_ms"]
        payload["bar_close_ms"] = close_ms
        payload["h1"]["bar_close_ms"] = ((close_ms - 300000) // 3600000) * 3600000
        payload["signal_id"] = f"OANDA:XAUUSD:5:{close_ms}:BUY" + (":0.2" if structured else "")
        if structured:
            structure = payload["context"]["market_structure"]
            for key in ("recent_high", "previous_high", "recent_low", "previous_low"):
                structure[key]["pivot_close_ms"] += time_shift
                structure[key]["confirmed_at_ms"] += time_shift
        if args.xm:
            # Entirely synthetic quote/offset, not an assertion about the live feed.
            now = int(time.time())
            offset = 7200
            payload.update(schema_version=3, strategy_version="xm-0.1", symbol="XM:XAUUSD",
                           signal_id=f"XM:XAUUSD:5:{close_ms}:BUY:xm-0.1")
            payload["h1"]["bar_close_ms"] += offset * 1000
            for key in ("recent_high", "previous_high", "recent_low", "previous_low"):
                for field in ("pivot_close_ms", "confirmed_at_ms"):
                    payload["context"]["market_structure"][key][field] += offset * 1000
            payload["source"] = {
                "provider": "xm_mt5_demo", "server": policy.source_server,
                "broker_symbol": policy.source_broker_symbol, "engine_version": "xm-0.1",
                "context_time_basis": "broker_server_wall_clock", "bar_close_server_ms": close_ms + offset * 1000,
                "snapshot_utc_s": now, "quote_server_ms": (now + offset) * 1000,
                "observed_utc_offset_s": offset, "bid": 2500, "ask": 2500.53, "price_tick": 0.01,
                "input_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            }
        with TestClient(create_app(config)) as client:
            started = time.monotonic()
            response = client.post("/webhook", headers={"Authorization": "Bearer " + config.webhook_token}, json=payload)
            assert response.status_code == 201
            receipt_ms = (time.monotonic() - started) * 1000
            record_id = response.json()["id"]
            deadline = time.monotonic() + config.ai_timeout_seconds + 10
            while time.monotonic() < deadline:
                analysis = client.get(f"/signals/{record_id}/analysis", headers={"Authorization": "Bearer " + config.api_token}).json()
                if analysis["status"] in ("completed", "failed", "expired"):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Worker did not finish before deadline")
            risk = client.get(f"/signals/{record_id}/risk", headers={"Authorization": "Bearer " + config.api_token}).json()
            report = {"synthetic_data": True, "temporary_database": True,
                              "variant": "xm" if args.xm else "v2" if args.v2 else "v1",
                              "inference_settings": {"provider":args.provider,
                                                     "reasoning_effort": 'low' if args.provider=='codex' else config.ai_reasoning_effort,
                                                     "max_tokens": None if args.provider=='codex' else 2048,
                                                     "output_mode":'cli_json_schema' if args.provider=='codex' else 'chat_completions_json_schema',
                                                     "timeout_seconds": config.ai_timeout_seconds},
                              "tested_at_utc_s": time.time(),
                              "webhook_receipt_ms": round(receipt_ms, 1),
                              "elapsed_seconds": round(time.monotonic() - started, 1), "analysis": analysis,
                              "risk": risk}
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
            assert analysis["status"] == "completed", analysis["error_code"]
            # Even complete evidence does not require the model to approve.
            assert analysis["result"]["decision"] in (("BUY", "WAIT") if structured else ("WAIT",))
            assert risk["status"] == ("passed" if structured else "rejected")
            assert analysis["effective_decision"] == (analysis["ai_decision"] if structured else "WAIT")


if __name__ == "__main__":
    main()
