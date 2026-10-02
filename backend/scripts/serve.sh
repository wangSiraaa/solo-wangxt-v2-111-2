#!/usr/bin/env bash
# 启动 FastAPI（先确保 PostgreSQL 已起）。
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 "$@"
