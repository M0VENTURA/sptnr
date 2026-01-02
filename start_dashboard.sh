#!/bin/bash
# SPTNR Dashboard Startup Script

echo "🎧 Starting SPTNR Dashboard..."
echo ""
echo "The dashboard will be available at:"
echo "  http://localhost:${DASHBOARD_PORT:-5000}"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

cd "$(dirname "$0")"
python dashboard.py
