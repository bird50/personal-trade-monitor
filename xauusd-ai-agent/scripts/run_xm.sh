#!/bin/sh
# Alert-only service: MT5 snapshots -> RSI(2) strategy -> Risk level checks -> Mac alerts (no AI, no Paper).
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn app.alert_main:create_alert_app --factory --host 127.0.0.1 --port 8002 --workers 1 --no-access-log --no-proxy-headers
