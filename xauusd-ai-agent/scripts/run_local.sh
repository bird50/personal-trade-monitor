#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# Capability URLs contain the ingest secret. Keep access logs disabled.
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
