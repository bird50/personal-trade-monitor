"""Real loopback HTTP test using synthetic data and a temporary database only."""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        token = secrets.token_urlsafe(32)
        env = {**os.environ, "MT5_BRIDGE_TOKEN": token,
               "MT5_DATABASE_PATH": str(Path(directory) / "market.db"),
               "MT5_SERVER": "XMGlobal-MT5 7", "MT5_SYMBOLS": "GOLD"}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.mt5_bridge:app",
                                   "--host", "127.0.0.1", "--port", str(port),
                                   "--no-access-log", "--no-proxy-headers"], cwd=root, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("Collector exited before startup")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Collector did not start")
                now = int(time.time())
                clock = now + 7200
                def bars(period):
                    end = clock // period * period
                    return [{"open_server_s": end - i * period, "open": 2500,
                             "high": 2502, "low": 2499, "close": 2501,
                             "tick_volume": 20, "spread_points": 30} for i in range(300, 0, -1)]
                payload = {"schema_version": 1, "source": "mt5", "account_mode": "demo",
                           "server": "XMGlobal-MT5 7", "broker_symbol": "GOLD",
                           "captured_at_utc_s": now, "server_time_s": clock,
                           "observed_utc_offset_s": 7200, "digits": 2, "point": 0.01,
                           "tick_size": 0.01, "quote": {"time_server_ms": clock * 1000,
                           "bid": 2500, "ask": 2500.3}, "m5": bars(300), "h1": bars(3600)}
                headers = {"Authorization": "Bearer " + token}
                assert client.post("/mt5/snapshot", json=payload).status_code == 401
                assert client.post("/mt5/snapshot", headers=headers, json=payload).status_code == 201
                assert client.post("/mt5/snapshot", headers=headers, json=payload).status_code == 200
                row = client.get("/mt5/latest?symbol=GOLD", headers=headers).json()
                assert row["payload"] == payload and row["status"] == "fresh"
                payload["account_mode"] = "real"
                assert client.post("/mt5/snapshot", headers=headers, json=payload).status_code == 422
                print("PASS: real HTTP auth, M5/H1 storage, deduplication, readback, demo-only validation.")
                print("Synthetic prices; temporary database only. No MT5 terminal connection tested.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
