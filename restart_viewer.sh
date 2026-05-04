#!/bin/bash

# MeshCore Bot Data Viewer Restart Script
# Manual restart tool for troubleshooting and development
# Use when integrated web viewer has issues or for standalone testing

echo "Restarting MeshCore Bot Data Viewer in standalone mode..."

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed."
    exit 1
fi

# Check if we're in the right directory
if [ ! -f "modules/web_viewer/app.py" ]; then
    echo "Error: app.py not found. Please run from the project root directory."
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Set environment variables for better performance
PYTHONPATH="${PYTHONPATH}:$(pwd)"
export PYTHONPATH
export FLASK_ENV=production

# Kill any existing web viewer processes on port 8083
echo "Checking for existing web viewer processes..."
if lsof -ti:8083 >/dev/null 2>&1; then
    echo "Found existing processes on port 8083, stopping them..."
    lsof -ti:8083 | xargs kill -9 2>/dev/null || true
    sleep 2
fi

# Start the web viewer in standalone mode
echo "Starting web viewer in standalone mode on http://127.0.0.1:8083"
echo "Note: This runs independently of the main bot"
echo "Press Ctrl+C to stop"
echo ""

python3 modules/web_viewer/app.py --host 0.0.0.0 --port 8083
