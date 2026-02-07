#!/bin/bash

# Vinyl Digitizer Web Interface Startup Script

cd "$(dirname "$0")"

echo "🎵 Starting Vinyl Digitizer Web Interface..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📡 Server will be available at:"
echo "   http://localhost:8000"
echo "   http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000 (from other devices)"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Start uvicorn server
/Library/Developer/CommandLineTools/usr/bin/python3 -m uvicorn backend.api:app --host 0.0.0.0 --port 8000 --reload
