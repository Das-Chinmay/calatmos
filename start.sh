#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh  —  One-command launcher for the CA Emissions Intelligence Dashboard
# Usage:  bash start.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   California Emissions Intelligence Dashboard                ║"
echo "║   Chinmay Das  |  UCR MSCS  |  Data Engineer                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Python check ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌  python3 not found. Install Python 3.10+ and re-run."
  exit 1
fi
PYTHON=$(command -v python3)
echo "✔  Python: $($PYTHON --version)"

# ── 2. Virtualenv ─────────────────────────────────────────────────────────────
VENV="$ROOT/.venv"
if [ ! -d "$VENV" ]; then
  echo "→  Creating virtual environment…"
  $PYTHON -m venv "$VENV"
fi
source "$VENV/bin/activate" 2>/dev/null || source "$VENV/Scripts/activate"
echo "✔  Virtual environment active"

# ── 3. Install deps ───────────────────────────────────────────────────────────
echo "→  Installing dependencies…"
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✔  Dependencies installed"

# ── 4. Pre-fetch data ─────────────────────────────────────────────────────────
mkdir -p "$ROOT/data"
echo "→  Pre-fetching data (first run only)…"
$PYTHON - <<'PYEOF'
import asyncio, sys
sys.path.insert(0, '.')
from backend.data_fetcher import load_ghg_emissions, load_county_emissions, load_aqi_data, load_counties_geojson

async def prefetch():
    await load_ghg_emissions()
    await load_county_emissions()
    await load_aqi_data()
    await load_counties_geojson()
    print("✔  Data cached in ./data/")

asyncio.run(prefetch())
PYEOF

# ── 5. Launch backend ─────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────────────────────────────"
echo "  Starting FastAPI backend on  http://localhost:8000"
echo "  API docs at                  http://localhost:8000/docs"
echo ""
echo "  Open the dashboard:          frontend/index.html"
echo "  (double-click or open in browser)"
echo "─────────────────────────────────────────────────────────────────"
echo ""

uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
