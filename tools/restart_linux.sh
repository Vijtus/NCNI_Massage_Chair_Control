#!/usr/bin/env bash
# Kill-everything-and-restart helper for the NCNI chair bridge.
#
# Usage:
#   ./tools/restart_linux.sh            # stop bridge, reset Arduino, relaunch
#   ./tools/restart_linux.sh --stop     # stop bridge, reset Arduino, DO NOT relaunch
#                                 # (use this to hand the bus back to the
#                                 # chair's OEM panel)
#   SERIAL_PORT=/dev/ttyACM0 ./tools/restart_linux.sh   # override port
#   HTTP_PORT=8090 ./tools/restart_linux.sh             # override HTTP port

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

STOP_ONLY=0
if [ "${1:-}" = "--stop" ]; then
    STOP_ONLY=1
fi

SERIAL_PORT="${SERIAL_PORT:-/dev/ttyUSB0}"
HTTP_PORT="${HTTP_PORT:-8080}"
LOG_DIR="${HOME}/.cache/ncni_massage_chair/log"
LOG_FILE="${LOG_DIR}/bridge.log"
mkdir -p "$LOG_DIR"

say() { printf '[restart] %s\n' "$*"; }

# ---------- step 1: stop any running bridge (clean SIGTERM first) ----------
say "stopping any running bridge..."
# pkill returns 1 when nothing matched — that's fine, swallow it.
pkill -TERM -f 'python.*app\.py' 2>/dev/null || true
# Give the SIGTERM path up to ~2.5 s to send the SoftSerial release '#'
# and close the port cleanly.
for _ in 1 2 3 4 5; do
    if ! pgrep -f 'python.*app\.py' >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
# Force kill anything that ignored SIGTERM.
if pgrep -f 'python.*app\.py' >/dev/null 2>&1; then
    say "SIGTERM ignored — escalating to SIGKILL."
    pkill -KILL -f 'python.*app\.py' 2>/dev/null || true
    sleep 0.5
fi

# ---------- step 2: free the HTTP port if anything else holds it ----------
if command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti :"${HTTP_PORT}" 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        say "freeing TCP $HTTP_PORT (pids: $PIDS)..."
        kill -TERM $PIDS 2>/dev/null || true
        sleep 0.5
        kill -KILL $PIDS 2>/dev/null || true
    fi
fi

# ---------- step 3: free the serial port too, if a stray process has it -
if command -v lsof >/dev/null 2>&1 && [ -e "$SERIAL_PORT" ]; then
    PIDS="$(lsof -t "$SERIAL_PORT" 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        say "freeing $SERIAL_PORT (pids: $PIDS)..."
        kill -TERM $PIDS 2>/dev/null || true
        sleep 0.5
        kill -KILL $PIDS 2>/dev/null || true
    fi
fi

# ---------- step 4: reset Arduino to high-Z idle ----------
# Opening + closing the USB serial port pulses DTR, which resets the
# Nano. We send no bytes, so the SoftSerial latch stays unflipped and
# pins 10/11 remain INPUT high-Z. The chair's OEM panel can drive the
# bus until something talks to the Arduino again.
if [ -e "$SERIAL_PORT" ]; then
    say "resetting Arduino on $SERIAL_PORT (DTR pulse, no bytes sent)..."
    python3 - "$SERIAL_PORT" <<'PY' || say "(reset skipped — pyserial unavailable or port busy)"
import sys, time
try:
    import serial
except ImportError:
    sys.exit(1)
try:
    s = serial.Serial(sys.argv[1], 115200, timeout=0.5)
    time.sleep(0.4)
    s.close()
except Exception:
    sys.exit(1)
PY
else
    say "no Arduino at $SERIAL_PORT (skipping reset)"
fi

# ---------- step 5: optionally relaunch ----------
if [ "$STOP_ONLY" -eq 1 ]; then
    say "stopped. Arduino is in high-Z idle — the OEM panel should work."
    exit 0
fi

say "launching bridge..."
APP_ARGS=(--no-browser)
if [ "$HTTP_PORT" != "8080" ]; then
    APP_ARGS+=(--port "$HTTP_PORT")
fi
if [ -n "${SERIAL_PORT_OVERRIDE:-}" ]; then
    APP_ARGS+=(--serial-port "$SERIAL_PORT_OVERRIDE")
fi
nohup python3 app.py "${APP_ARGS[@]}" >>"$LOG_FILE" 2>&1 &
BPID=$!
disown "$BPID" 2>/dev/null || true

# Wait briefly so the user sees a real status, not "starting up..."
sleep 4
if pgrep -F /dev/null -f 'python.*app\.py' >/dev/null 2>&1 \
   || pgrep -f 'python.*app\.py' >/dev/null 2>&1; then
    URL="http://127.0.0.1:${HTTP_PORT}"
    say "bridge running (pid $BPID). URL: $URL"
    say "log: $LOG_FILE"
    # Best-effort open in a browser; not fatal if it fails.
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 &
    fi
else
    say "bridge process exited unexpectedly. Check $LOG_FILE"
    exit 1
fi
