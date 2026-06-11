from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
SVG_PATH = ROOT / "assets" / "display" / "massage_display_interface.svg"
ROOT_VIEW_PATH = ROOT / "ROOT-VIEW.html"

FIRMWARE_DEFAULT_BAUD = 115200
RECONNECT_INTERVAL_SECONDS = 1.5
SERIAL_LOOP_SLEEP_SECONDS = 0.02
WRITE_INTERVAL_SECONDS = 0.02
LISTEN_OFF_RETRY_SECONDS = 0.35
LISTEN_OFF_CONTROL_PAYLOAD = "!!!!!!!!"
LISTEN_ON_CONTROL_PAYLOAD = "~"
# Sent once on bridge shutdown so the Arduino releases its SoftwareSerial
# pins back to high-impedance INPUT, letting the chair's OEM panel take
# over the bus without requiring a physical Arduino reset.
RELEASE_CONTROL_PAYLOAD = "#"
ACK_TIMEOUT_SECONDS = 2.0
DONE_TIMEOUT_SECONDS = 5.0
VERIFY_SETTLE_SECONDS = 2.5
DEFAULT_RETRIES = 1
HOLD_MAX_SECONDS = 8.0
HOLD_STOP_TIMEOUT_SECONDS = 2.0
HOLD_CANCEL_CACHE_SECONDS = 10.0
LISTEN_INITIAL_RETRY_SECONDS = 1.8
LISTEN_MAX_RETRY_SECONDS = 30.0
LISTEN_BACKOFF_AFTER_FAILURES = 3
CONNECT_SETTLE_SECONDS = 4.0
STARTUP_DRAIN_SECONDS = 0.8
STARTUP_DRAIN_EXTEND_SECONDS = 0.12
STARTUP_DRAIN_SLEEP_SECONDS = 0.03

PROMPT_SECONDS = 1.0
OVERLAY_SECONDS = 0.9
POWER_OFF_TEXT_SECONDS = 1.4
STATE_POLL_HINT_MS = 150
MUTE_SECONDS = 0.75
DRIFT_SECONDS = 2.0
FRAME_STALE_SECONDS = 5.0
FAILED_FLASH_SECONDS = 0.9
COMMAND_FLASH_SECONDS = 0.22

# Visible timer display offset. Live bench observation says the chair LCD
# stays on the start minute longer than the bridge's canonical countdown.
# Tune this value for Czas-NUMBER only. On 2026-04-30 the browser changed
# 15 -> 14 around 25s while the hardware LCD changed around 45s, so the
# visible offset is set to 45s.
TIMER_DISPLAY_OFFSET_SECONDS = 45

# Auto speed-program offset. This is intentionally separate from the visible
# timer display offset: the user observed that sharing the display offset made
# Predkosc-LVL animations far too delayed. Keep the default at 0 to match the
# previously better-aligned speed-bar timing; tune independently only if a
# speed-level bench run proves the program itself needs an offset.
AUTO_SPEED_PROGRAM_OFFSET_SECONDS = 0

# Boot stabilization gate. After a local power-on press the bridge
# rejects manual zone / direction / scalar commands until the chair
# has settled — both a minimum monotonic delay AND at least N stable
# running frames must have arrived. The hard timeout is fail-open in
# case the chair never sends frames for some reason.
BOOT_SETTLE_MIN_SECONDS = 1.0
BOOT_SETTLE_FRAMES = 3
BOOT_SETTLE_TIMEOUT_SECONDS = 5.0

BACKEND_LOG_LIMIT = 200
COMMAND_HISTORY_LIMIT = 200
FAILED_COMMAND_LIMIT = 200
FRAME_HISTORY_LIMIT = 200

INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
FULL_FRAME_LENGTH = 33
FRAME_TRAILER = [0x00, 0x00, 0x00, 0x00]

HTTP_SERVER_VERSION = "ElectricChairBridge/1.0"
# Default bind makes the panel reachable from other devices on the same LAN
# (phones, tablets, second computers). Use --local to bind loopback only.
DEFAULT_HOST = "0.0.0.0"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
LOG_DIR = Path.home() / ".cache" / "ncni_massage_chair" / "log"
LOG_FILE = LOG_DIR / "bridge.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5
