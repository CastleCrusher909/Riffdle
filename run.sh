#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Activate venv (create if missing)
if [ ! -d "venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi

source venv/bin/activate

echo "Installing / updating dependencies…"
pip install -q -r requirements.txt

echo ""
echo "Starting Riffdle on http://localhost:5001"
echo ""

python3 backend/app.py
