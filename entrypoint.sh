#!/bin/bash
# Popularr/SPTNR container entrypoint.
# Orchestration only; database logic lives in db/ and migrations/.

set -Eeuo pipefail

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

run_queue_startup_schema() {
    log "Running minimal queue startup schema bootstrap..."
    if python3 migrations/ensure_queue_startup_schema.py; then
        ok "Minimal queue startup schema bootstrap complete"
    else
        warn "Minimal queue startup schema bootstrap failed (non-fatal)"
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
    log "Starting download queue processor (interval: ${interval}s)..."
    export SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY=1
    python3 queue_processor.py "$interval" > "$log_file" 2>&1 &
    QUEUE_PID=$!
    ok "Queue processor started (PID: $QUEUE_PID)"
}

preflight_python() {
    log "Running Python syntax checks..."
    python3 -m py_compile app.py
    python3 -m py_compile queue_processor.py
    python3 -m py_compile db/utils.py
    python3 -m py_compile db/context.py
    python3 -m py_compile db/schema.py
    python3 -m py_compile db/schema_helpers.py
    python3 -m py_compile db/bootstrap.py
    python3 -m py_compile db/cleanup.py
    python3 -m py_compile db/database.py
    python3 -m py_compile db/repositories/artists.py
    python3 -m py_compile db/repositories/bookmarks.py
    python3 -m py_compile db/repositories/genres.py
    python3 -m py_compile db/repositories/library.py
    python3 -m py_compile db/repositories/navidrome.py
    python3 -m py_compile db/repositories/tracks.py
    python3 -m py_compile migrations/ensure_queue_startup_schema.py
    ok "Python syntax checks passed"
}

start_web_app() {
    log "Starting Flask web application..."
    exec gunicorn             --bind "${SPTNR_GUNICORN_BIND:-0.0.0.0:5000}"             --workers "${SPTNR_GUNICORN_WORKERS:-4}"             --worker-class gthread             --threads "${SPTNR_GUNICORN_THREADS:-4}"             --timeout "${SPTNR_GUNICORN_TIMEOUT:-300}"             --graceful-timeout "${SPTNR_GUNICORN_GRACEFUL_TIMEOUT:-60}"             --keep-alive "${SPTNR_GUNICORN_KEEP_ALIVE:-5}"             --worker-tmp-dir "${SPTNR_GUNICORN_WORKER_TMP_DIR:-/dev/shm}"             --access-logfile "${SPTNR_ACCESS_LOG:-/config/access.log}"             --error-logfile "${SPTNR_ERROR_LOG:-/config/error.log}"             --log-level "${SPTNR_LOG_LEVEL:-info}"             "app:app"
}

main() {
    trap cleanup SIGTERM SIGINT EXIT
    log "=== Popularr Starting ==="
    run_queue_startup_schema
    check_ffmpeg
    start_queue_processor
    preflight_python
    start_web_app
}

main "$@"
