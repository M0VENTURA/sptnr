#!/bin/bash
# Popularr container entrypoint.
# Orchestration only; database logic lives in db/ and migrations/.

set -Eeuo pipefail

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              Popularr — Music Popularity Scanner         ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

QUEUE_PID=""

log()  { echo "$*"; }
warn() { echo "⚠ $*"; }
ok()   { echo "✓ $*"; }

cleanup() {
    log "Stopping Popularr..."
    if [ -n "${QUEUE_PID:-}" ] && kill -0 "$QUEUE_PID" 2>/dev/null; then
        log "Stopping queue processor (PID: $QUEUE_PID)..."
        kill "$QUEUE_PID" 2>/dev/null || true
        wait "$QUEUE_PID" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Wait for PostgreSQL to accept connections (Docker Compose support)
# ---------------------------------------------------------------------------
wait_for_db() {
    local host="${PG_HOST:-}"
    local port="${PG_PORT:-5432}"
    local user="${PG_USER:-popularr}"
    local retries=15
    local delay=2

    # Skip if no PG_HOST is set (SQLite fallback)
    if [ -z "$host" ] || [ "$host" = "localhost" ]; then
        return 0
    fi

    log "Waiting for PostgreSQL at $host:$port..."
    for i in $(seq 1 $retries); do
        if command -v pg_isready >/dev/null 2>&1; then
            if pg_isready -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
                ok "PostgreSQL is ready (attempt $i)"
                return 0
            fi
        else
            # Fallback: try a TCP connection using python
            if python3 -c "import socket; s=socket.create_connection(('$host',$port), timeout=3); s.close()" 2>/dev/null; then
                ok "PostgreSQL port is open (attempt $i)"
                return 0
            fi
        fi
        if [ "$i" -lt "$retries" ]; then
            log "  PostgreSQL not ready yet, retrying in ${delay}s... ($i/$retries)"
            sleep "$delay"
        fi
    done
    warn "PostgreSQL did not become ready after $retries attempts — continuing anyway"
}

run_queue_startup_schema() {
    log "Running minimal queue startup schema bootstrap..."
    if python3 migrations/ensure_queue_startup_schema.py; then
        ok "Minimal queue startup schema bootstrap complete"
    else
        warn "Minimal queue startup schema bootstrap failed (non-fatal)"
    fi
}

run_alembic_migrations() {
    if [ -f alembic.ini ] && [ -d migrations/versions ]; then
        log "Running Alembic database migrations..."
        if python3 -m alembic upgrade head 2>/dev/null; then
            ok "Alembic migrations applied"
        else
            warn "Alembic migrations failed (non-fatal — legacy schema bootstrap covers it)"
        fi
    fi
}

run_schema_bootstrap() {
    log "Running database schema bootstrap..."
    if python3 -m db.bootstrap 2>&1; then
        ok "Database schema bootstrap complete"
    else
        warn "Database schema bootstrap failed (non-fatal — app will retry on startup)"
    fi
}

check_ffmpeg() {
    log "Checking ffmpeg availability..."
    if command -v ffmpeg >/dev/null 2>&1; then
        ok "ffmpeg found: $(ffmpeg -version | head -n 1)"
        return 0
    fi
    warn "ffmpeg not found in PATH. FLAC→MP3 conversion will be unavailable."
    if [ "${SPTNR_AUTO_INSTALL_FFMPEG:-0}" = "1" ] && command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
        log "Attempting runtime ffmpeg install..."
        apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
    fi
}

start_queue_processor() {
    local interval="${SPTNR_QUEUE_PROCESSOR_INTERVAL:-30}"
    local log_file="${SPTNR_QUEUE_PROCESSOR_LOG:-/config/queue_processor.log}"
    # Ensure log directory exists before redirect
    local log_dir
    log_dir="$(dirname "$log_file")"
    mkdir -p "$log_dir" 2>/dev/null || true
    log "Starting download queue processor (interval: ${interval}s)..."
    export SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY=1
    if python3 -m services.queue.queue_worker "$interval" > "$log_file" 2>&1 &
    then
        QUEUE_PID=$!
        ok "Queue processor started (PID: $QUEUE_PID)"
    else
        warn "Queue processor failed to start (non-fatal, continuing)"
    fi
}

preflight_python() {
    log "Running Python syntax checks..."
    local errors=0
    local failed_files=""
    local files=(
        app.py
        services/queue/queue_worker.py
        db/utils.py
        db/context.py
        db/schema.py
        db/schema_helpers.py
        db/bootstrap.py
        db/cleanup.py
        db/database.py
        db/repositories/artists.py
        db/repositories/bookmarks.py
        db/repositories/genres.py
        db/repositories/library.py
        db/repositories/navidrome.py
        db/repositories/tracks.py
        migrations/ensure_queue_startup_schema.py
    )
    for f in "${files[@]}"; do
        if ! python3 -m py_compile "$f" 2>&1; then
            errors=$((errors + 1))
            failed_files="${failed_files}  - ${f}\n"
            warn "Syntax check FAILED: $f"
        fi
    done
    if [ "$errors" -gt 0 ]; then
        warn "Python syntax checks completed with ${errors} error(s):"
        printf "%b" "$failed_files"
        warn "Continuing despite syntax errors (non-fatal)"
    else
        ok "Python syntax checks passed"
    fi
}

start_web_app() {
    log "Starting web server (hypercorn, ${SPTNR_GUNICORN_WORKERS:-4} workers)..."
    echo ""
    exec hypercorn             --bind "${SPTNR_GUNICORN_BIND:-0.0.0.0:5000}"             --workers "${SPTNR_GUNICORN_WORKERS:-4}"             --worker-class asyncio             --keep-alive "${SPTNR_GUNICORN_KEEP_ALIVE:-5}"             --access-logfile "${SPTNR_ACCESS_LOG:-/config/access.log}"             --error-logfile -             --log-level "${SPTNR_LOG_LEVEL:-info}"             "app:app"
}

main() {
    trap cleanup SIGTERM SIGINT EXIT
    trap 'warn "Fatal error on line $LINENO — see above for details"' ERR

    log "── Startup ─────────────────────────────────────────────────"

    wait_for_db

    run_queue_startup_schema || true
    run_alembic_migrations || true
    run_schema_bootstrap || true
    check_ffmpeg || true

    echo ""
    log "── Background Services ──────────────────────────────────────"

    start_queue_processor || true

    echo ""
    log "── Pre-flight Checks ───────────────────────────────────────"

    preflight_python || true

    # Test Quart app import BEFORE hypercorn
    if python3 -c "import sys; sys.path.insert(0, '.'); from app import app" 2>&1; then
        ok "App import OK"
    else
        warn "App import FAILED — see error above"
    fi

    echo ""
    log "── Launch ──────────────────────────────────────────────────"

    start_web_app

    # If we reach here, hypercorn failed to start
    warn "Web server did not start — check the error above"
    sleep 60
}

main "$@"
