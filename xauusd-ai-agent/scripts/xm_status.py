"""Show the alert engine state and the latest trade alerts without printing credentials."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import Settings


def main():
    config = Settings.from_env()
    try:
        with httpx.Client(base_url="http://127.0.0.1:8002", timeout=5, trust_env=False,
                          headers={"Authorization": "Bearer " + config.api_token}) as client:
            health = client.get("/health")
            health.raise_for_status()
            status = client.get("/xm/status")
            status.raise_for_status()
            alerts = client.get("/dashboard/state")
            alerts.raise_for_status()
            state = alerts.json()
            print(json.dumps({"health": health.json(), **status.json(), "ready": state["ready"],
                              "muted": state["muted"], "alert_counts": state["alert_counts"],
                              "latest_alerts": state["alerts"][:5]}, indent=2, ensure_ascii=False))
    except httpx.HTTPError:
        raise SystemExit("Alert service unavailable or unauthorized; run scripts/run_xm.sh and check local config.") from None


if __name__ == "__main__":
    main()
