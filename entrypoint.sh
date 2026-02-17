#!/bin/bash
# SPTNR Entrypoint - Starts both Flask web app and queue processor
# This allows both services to run in the same container

set -e

echo "=== SPTNR Starting ==="
echo "Starting queue processor and Flask web application..."

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
    "server:sptnr"
