#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn app.mt5_bridge:app --host 127.0.0.1 --port 8001 --workers 1 --no-access-log --no-proxy-headers
