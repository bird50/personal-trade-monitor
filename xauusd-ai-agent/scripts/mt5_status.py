"""Read local collector health and latest quote without printing credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from app.mt5_bridge import BridgeSettings

settings = BridgeSettings.from_env()
symbol = sys.argv[1] if len(sys.argv) > 1 else "GOLD"
try:
    with httpx.Client(base_url="http://127.0.0.1:8001", timeout=5, trust_env=False) as client:
        client.get("/health").raise_for_status()
        response = client.get("/mt5/latest", params={"symbol": symbol},
                              headers={"Authorization": "Bearer " + settings.token})
        if response.status_code == 404:
            raise SystemExit("Receiver ready; no MT5 data received yet.")
        response.raise_for_status()
        data = response.json()
        if data["status"] == "stale":
            diagnostic = client.get("/mt5/status", headers={"Authorization": "Bearer " + settings.token})
            if diagnostic.status_code == 200:
                print("Last bridge attempt:", diagnostic.json()["last_attempt"])
        payload = data["payload"]
        print(f"{symbol}: {data['status']}, age={data['age_seconds']}s, "
              f"M5={len(payload['m5'])}, H1={len(payload['h1'])}")
        print(f"Bid={payload['quote']['bid']}, Ask={payload['quote']['ask']}")
        print("Mode: market_data_only; broker bar times retained as server time.")
except httpx.HTTPError:
    raise SystemExit("Collector unavailable or request rejected; check receiver and local configuration.") from None
