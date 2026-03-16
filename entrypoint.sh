#!/bin/bash
# SPTNR Entrypoint - Starts both Flask web app and queue processor
# This allows both services to run in the same container

set -e

echo "=== SPTNR Starting ==="
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

# Start Flask app in foreground (this blocks)
echo "Starting Flask web application (port 5000)..."
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 4 \
    --worker-class sync \
    --timeout 120 \
    --access-logfile /config/access.log \
    --error-logfile /config/error.log \
    --log-level info \
    "app:app"
