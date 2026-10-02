#!/usr/bin/env bash
# 停止用户态 PostgreSQL。
set -euo pipefail
PGDATA="$HOME/.local/pgdata"
export LD_LIBRARY_PATH="$HOME/.local/pgsql/usr/lib/postgresql/15/lib:${LD_LIBRARY_PATH:-}"
export PATH="$HOME/.local/pgsql/usr/lib/postgresql/15/bin:$PATH"
pg_ctl -D "$PGDATA" stop
