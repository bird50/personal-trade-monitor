"""Show XM engine, candidate queue and analysis state without printing credentials."""
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
            response = client.get("/xm/status")
            response.raise_for_status()
            status = response.json()
            health = client.get("/health")
            health.raise_for_status()
            result = {"health": health.json(), **status}
            paper=client.get('/paper/account')
            if paper.status_code==200:
                result['paper_account']=paper.json()
            signals = client.get("/signals", params={"limit": 100})
            signals.raise_for_status()
            rows = signals.json()["items"]
            result["first_page_candidate_count"] = len(rows)
            if rows:
                # Status of the last evaluated candidate, if available; history is paginated.
                record_id = status["engine"].get("signal_record_id") or rows[-1]["id"]
                analysis = client.get(f"/signals/{record_id}/analysis")
                analysis.raise_for_status()
                result["analysis"] = analysis.json()
            print(json.dumps(result, indent=2))
    except httpx.HTTPError:
        raise SystemExit("XM service unavailable or unauthorized; run scripts/run_xm.sh and check local config.") from None


if __name__ == "__main__":
    main()
