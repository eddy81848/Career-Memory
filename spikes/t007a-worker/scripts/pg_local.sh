#!/usr/bin/env bash
# Throwaway local PostgreSQL for manual T-007A runs. Unix socket only, no TCP listener.
#   scripts/pg_local.sh start   -> prints `export T007A_DATABASE_URL=...`
#   scripts/pg_local.sh stop    -> stops and deletes the cluster
set -euo pipefail
ROOT=${T007A_PG_ROOT:-/tmp/t7a-pg-manual}
PORT=${T007A_PG_PORT:-56432}
PG_BIN=${PG_BIN:-$(dirname "$(command -v initdb 2>/dev/null || echo /opt/homebrew/opt/postgresql@18/bin/initdb)")}

case "${1:-}" in
  start)
    [ -x "$PG_BIN/initdb" ] || { echo "initdb not found (set PG_BIN)" >&2; exit 2; }
    mkdir -p "$ROOT/sock"
    [ -d "$ROOT/data" ] || "$PG_BIN/initdb" -D "$ROOT/data" -U t007a --auth=trust -E UTF8 \
        --locale=C --no-sync --no-instructions >/dev/null
    "$PG_BIN/pg_ctl" -D "$ROOT/data" -l "$ROOT/pg.log" -w \
        -o "-p $PORT -k $ROOT/sock -c listen_addresses=''" start >/dev/null
    DSN="host=$ROOT/sock port=$PORT user=t007a dbname=postgres"
    T007A_DATABASE_URL="$DSN" "$(dirname "$0")/../.venv/bin/python" -c \
        "import os; from t007a.db import apply_schema; apply_schema(os.environ['T007A_DATABASE_URL'])"
    echo "export T007A_DATABASE_URL='$DSN'"
    ;;
  stop)
    "$PG_BIN/pg_ctl" -D "$ROOT/data" -m fast -w stop >/dev/null 2>&1 || true
    rm -rf "$ROOT"
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 64 ;;
esac
