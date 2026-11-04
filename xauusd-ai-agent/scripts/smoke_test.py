"""Exercise actual loopback HTTP using disposable SQLite and temporary tokens."""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="xauusd-smoke-") as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        ingest_token = secrets.token_urlsafe(32)
        read_token = secrets.token_urlsafe(32)
        environment = {
            **os.environ, "DATABASE_PATH": str(Path(directory) / "smoke.db"),
            "WEBHOOK_TOKEN": ingest_token, "API_TOKEN": read_token,
            "ALLOWED_SYMBOLS": "OANDA:XAUUSD", "MAX_SIGNAL_AGE_SECONDS": "300",
            "MAX_FUTURE_SECONDS": "30", "MAX_BODY_BYTES": "16384",
            "WEBHOOK_RATE_PER_MINUTE": "120",
            "AI_ENABLED": "false",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
             "--no-access-log", "--no-proxy-headers", "--log-level", "warning"],
            cwd=ROOT, env=environment, pass_fds=(listener.fileno(),),
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Server exited before readiness")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Server did not become ready")
                    time.sleep(0.1)
                payload = json.loads((ROOT / "examples/signal.json").read_text())
                close_ms = int(time.time() // 300) * 300_000
                payload["bar_close_ms"] = close_ms
                payload["h1"]["bar_close_ms"] = ((close_ms - 300_000) // 3_600_000) * 3_600_000
                payload["signal_id"] = f"OANDA:XAUUSD:5:{close_ms}:BUY"
                started = time.monotonic()
                response = client.post("/webhook", headers={"Authorization": f"Bearer {ingest_token}"}, json=payload)
                elapsed_ms = (time.monotonic() - started) * 1000
                assert response.status_code == 201, response.status_code
                duplicate = client.post(f"/webhook/tradingview/{ingest_token}", json=payload)
                assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
                records = client.get("/signals", headers={"Authorization": f"Bearer {read_token}"})
                assert records.status_code == 200 and len(records.json()["items"]) == 1
                assert client.get("/signals").status_code == 401
                print(f"PASS: HTTP health, insert, deduplication, authenticated read; insert {elapsed_ms:.1f} ms")
                print("Temporary database only; server will stop now.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
