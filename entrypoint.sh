#!/bin/bash
# SPTNR Entrypoint - Starts both Flask web app and queue processor
# This allows both services to run in the same container

set -e

echo "=== Popularr Starting ==="

# Run a lightweight startup migration before starting services.
# This avoids importing app.py during boot (which can delay startup significantly).
echo "Running startup queue migration..."
python3 migrations/startup_queue_columns_fast.py || echo "⚠ startup queue migration failed (non-fatal)"
echo "Startup queue migration complete."

echo "Starting queue processor and Flask web application..."

# Verify ffmpeg availability for download conversion features.
if command -v ffmpeg >/dev/null 2>&1; then
    echo "✓ ffmpeg found: $(ffmpeg -version | head -n 1)"
else
    echo "⚠ ffmpeg not found in PATH. FLAC→MP3 conversion will be unavailable."

    if [ "${SPTNR_AUTO_INSTALL_FFMPEG:-0}" = "1" ]; then
        if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
            echo "Attempting runtime ffmpeg install (SPTNR_AUTO_INSTALL_FFMPEG=1)..."
            apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
            if command -v ffmpeg >/dev/null 2>&1; then
                echo "✓ ffmpeg installed at startup"
            else
                echo "⚠ Runtime ffmpeg install attempted but ffmpeg is still unavailable"
            fi
        else
            echo "⚠ Cannot auto-install ffmpeg (requires apt-get and root user)"
        fi
    fi
fi

# Start queue processor in background (processing interval: 30 seconds)
echo "Starting download queue processor (interval: 30s)..."
export SPTNR_QUEUE_PROCESSOR_MANAGED_EXTERNALLY=1
python3 queue_processor.py 30 > /config/queue_processor.log 2>&1 &
QUEUE_PID=$!
echo "✓ Queue processor started (PID: $QUEUE_PID)"

# Function to cleanup on exit
cleanup() {
    echo "Stopping SPTNR..."
    if [ ! -z "$QUEUE_PID" ]; then
        echo "Stopping queue processor (PID: $QUEUE_PID)..."
        kill $QUEUE_PID 2>/dev/null || true
        wait $QUEUE_PID 2>/dev/null || true
    fi
}

# Setup signal handlers
trap cleanup SIGTERM SIGINT EXIT

# Pre-flight syntax check — fail fast before Gunicorn tries to load the module.
# This surfaces stale/corrupt source immediately instead of looping through
# "Worker failed to boot" errors with no actionable message.
echo "Running pre-flight syntax check on app.py..."
if ! python3 -m py_compile app.py; then
    echo "✗ FATAL: app.py has a syntax error (shown above). Fix and rebuild the image."
    exit 1
fi
echo "✓ app.py syntax OK"

# Start Flask app in foreground (this blocks)
echo "Starting Flask web application (port 5000)..."
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 4 \
    --worker-class gthread \
    --threads 4 \
    --timeout 300 \
    --graceful-timeout 60 \
    --keep-alive 5 \
    --worker-tmp-dir /dev/shm \
    --access-logfile /config/access.log \
    --error-logfile /config/error.log \
    --log-level info \
    "app:app"
