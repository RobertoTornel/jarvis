#!/bin/bash

# Test script for the Claire Desktop App

# Parse arguments
VOICE_DEBUG=0
for arg in "$@"; do
    case $arg in
        --voice-debug)
            VOICE_DEBUG=1
            shift
            ;;
    esac
done

# Navigate to project root first
cd "$(dirname "$0")/.." || exit

echo "🔧 Testing Claire Desktop App locally..."
if [ "$VOICE_DEBUG" = "1" ]; then
    echo "   📋 Voice debug: ENABLED"
fi
echo ""

# Find a suitable Python (3.10+) on PATH.
PYTHON=""
for candidate in python3.12 python3.11 python3.10; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # Fall back to python3 and hope it's new enough
    PYTHON="python3"
fi

# Set up / activate virtual environment
if [ ! -d .venv ]; then
    echo "📦 Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi
source .venv/bin/activate

# Check Python version
echo "📋 Checking Python version..."
python --version
PY_MINOR=$(python -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MINOR" -lt 10 ]; then
    echo "⚠️  Python 3.10+ is required. Found $(python --version)."
    echo "   Recreating .venv with $PYTHON..."
    deactivate 2>/dev/null
    rm -rf .venv
    "$PYTHON" -m venv .venv
    source .venv/bin/activate
    echo "   Now using: $(python --version)"
fi
echo ""

# Install dependencies from requirements.txt
echo "📦 Installing dependencies..."
pip install -q -r requirements.txt
echo ""

# Generate icons
echo "🎨 Generating icons..."
python src/desktop_app/desktop_assets/generate_icons.py
echo ""

# Run the desktop app
echo "🚀 Starting desktop app..."
echo "   Click the system tray icon to open menu"
echo "   Select 'Start Listening' from menu to begin"
echo "   Or press Ctrl+C to quit"
echo ""

# Set PYTHONPATH to include src directory (already at project root)
export PYTHONPATH="$(pwd)/src:$PYTHONPATH"

# Set voice debug environment variable if requested
if [ "$VOICE_DEBUG" = "1" ]; then
    export CLAIRE_VOICE_DEBUG=1
fi

python -m desktop_app

