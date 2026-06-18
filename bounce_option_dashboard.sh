#!/usr/bin/env bash
# bounce_option_dashboard.sh
# Kills any running option_dashboard.py server, verifies the correct Python
# environment, and restarts the server.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD="$SCRIPT_DIR/option_dashboard.py"
NLM_PYTHON="/Users/joeandbabs/public-env/bin/python"
PORT=5051

# ── 1. Kill any running instance ─────────────────────────────────────────────
_kill_port() {
    # Try lsof first, then fuser as fallback
    local pids
    pids=$(lsof -ti ":$PORT" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        pids=$(fuser "$PORT/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' || true)
    fi
    echo "$pids"
}

PIDS=$(_kill_port)
if [ -n "$PIDS" ]; then
    echo "Stopping server on port $PORT (PID(s): $PIDS)..."
    kill -15 $PIDS 2>/dev/null || true
    sleep 2
    # Force-kill anything that survived SIGTERM
    REMAINING=$(_kill_port)
    if [ -n "$REMAINING" ]; then
        echo "Force-killing stubborn process(es): $REMAINING"
        kill -9 $REMAINING 2>/dev/null || true
        sleep 1
    fi
else
    echo "No server running on port $PORT."
fi

# Also kill by script name in case lsof/fuser missed it
pkill -15 -f "option_dashboard.py" 2>/dev/null || true
# Kill any caffeinate spawned by a previous bounce
pkill -15 -f "caffeinate.*option_dashboard" 2>/dev/null || true
sleep 1

# ── 2. Verify correct Python ──────────────────────────────────────────────────
if [ ! -x "$NLM_PYTHON" ]; then
    echo "ERROR: nlm-env Python not found at $NLM_PYTHON"
    echo "  Check your nlm-env path and update NLM_PYTHON in this script."
    exit 1
fi

PY_VERSION=$("$NLM_PYTHON" --version 2>&1)
echo "Using: $NLM_PYTHON ($PY_VERSION)"

# Verify yfinance is importable
if ! "$NLM_PYTHON" -c "import yfinance" 2>/dev/null; then
    echo "WARNING: yfinance not found in nlm-env — installing..."
    "$NLM_PYTHON" -m pip install yfinance --quiet
fi

# Verify flask is importable
if ! "$NLM_PYTHON" -c "import flask" 2>/dev/null; then
    echo "WARNING: flask not found in public-env — installing..."
    "$NLM_PYTHON" -m pip install flask --quiet
fi

# Verify requests is importable
if ! "$NLM_PYTHON" -c "import requests" 2>/dev/null; then
    echo "WARNING: requests not found in public-env — installing..."
    "$NLM_PYTHON" -m pip install requests --quiet
fi

# Verify python-dotenv is importable
if ! "$NLM_PYTHON" -c "import dotenv" 2>/dev/null; then
    echo "WARNING: python-dotenv not found in public-env — installing..."
    "$NLM_PYTHON" -m pip install python-dotenv --quiet
fi

# Verify notebooklm-py is importable
if ! "$NLM_PYTHON" -c "import notebooklm" 2>/dev/null; then
    echo "WARNING: notebooklm-py not found in public-env — installing..."
    "$NLM_PYTHON" -m pip install "notebooklm-py[browser]" --quiet
fi

# Alerts
export PUSHOVER_USER_KEY="u3eykaash8p7s9nimrd29wwukiffg6"
export PUSHOVER_APP_TOKEN="a6c4f946vwtn2nky71zqu838yq2jmh"

# ── 3. Start the server ───────────────────────────────────────────────────────
echo "Starting Option Dashboard (with caffeinate to prevent sleep)..."
cd "$SCRIPT_DIR"
export _PORTFOLIO_BOUNCE=1
nohup caffeinate -i "$NLM_PYTHON" "$DASHBOARD" --web \
    > "$SCRIPT_DIR/option_dashboard.log" 2>&1 &
echo "Started (PID $!). Log: $SCRIPT_DIR/option_dashboard.log"
