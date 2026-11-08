#!/bin/sh
# Previous AI/Risk/Paper service (not started by XM Control; run manually only).
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn app.xm_main:create_xm_app --factory --host 127.0.0.1 --port 8002 --workers 1 --no-access-log --no-proxy-headers
