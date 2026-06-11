"""NCNI Massage Chair Control Panel — entry point.

This is the file a doctor or therapist clicks. It starts the local web
server that serves the chair touch panel and opens the default browser.

Usage:
    python3 app.py                     # normal start, auto-open browser
    python3 app.py --no-browser        # start server without opening browser
    python3 app.py --local             # bind 127.0.0.1 only (developer)
    python3 app.py --port 9000         # custom port
    python3 app.py --serial-port /dev/ttyACM0
    python3 app.py --log-level DEBUG

The terminal output is intentionally minimal. Detailed logs are written
to ~/.cache/ncni_massage_chair/log/bridge.log.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import signal
import sys
import tempfile
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Make sure imports work even when launched by double-clicking from another
# working directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from bridge import config  # noqa: E402
from bridge.firmware import FirmwareSerialBridge  # noqa: E402
from bridge.http_server import (  # noqa: E402
    AppServer,
    build_public_url,
    get_lan_ip,
)
from bridge.state import ChairState  # noqa: E402
from bridge.svg import (  # noqa: E402
    load_root_view_markup,
    load_svg_markup,
    svg_newer_than_process_start,
)

PRODUCT_NAME = "NCNI Massage Chair Control Panel"


def configure_logging(level: str) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = config.LOG_FILE
        handler = RotatingFileHandler(
            log_file,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        fallback_dir = Path(tempfile.gettempdir()) / "ncni_massage_chair" / "log"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        log_file = fallback_dir / "bridge.log"
        handler = RotatingFileHandler(
            log_file,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def build_local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def build_startup_output(
    public_url: str,
    browser_url: str,
    *,
    browser_enabled: bool = True,
) -> str:
    """Doctor-friendly banner — no terminal QR, no implementation noise."""
    opening = (
        f"Opening browser: {browser_url}"
        if browser_enabled
        else "Browser auto-open disabled."
    )
    return "\n".join(
        [
            PRODUCT_NAME,
            opening,
            f"Panel on this computer: {browser_url}",
            f"LAN address: {public_url}",
            "Other devices must be on the same Wi-Fi/LAN.",
            "Press Ctrl+C to stop.",
        ]
    )


def print_startup_banner(
    host: str, port: int, lan_ip: str, *, browser_enabled: bool = True
) -> str:
    public_url = build_public_url(host, port, lan_ip)
    browser_url = _browser_target_url(host, port, lan_ip)
    output = build_startup_output(
        public_url, browser_url, browser_enabled=browser_enabled
    )
    print(output, flush=True)
    return public_url


print_doctor_banner = print_startup_banner


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=f"{PRODUCT_NAME} — local web bridge for the massage chair.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CHAIR_BRIDGE_HOST", config.DEFAULT_HOST),
        help="Bind host (default: 0.0.0.0 for LAN access).",
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help="Bind for LAN access. This is the default; kept for compatibility.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Bind to 127.0.0.1 for local-only developer use.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CHAIR_BRIDGE_HTTP_PORT", str(config.DEFAULT_PORT))),
        help=f"HTTP port (default: {config.DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("CHAIR_BRIDGE_SERIAL_PORT"),
        help="Serial device for the firmware (auto-detect if omitted).",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=int(
            os.environ.get("CHAIR_BRIDGE_BAUD", str(config.FIRMWARE_DEFAULT_BAUD))
        ),
        help="Serial baud rate.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CHAIR_BRIDGE_LOG_LEVEL", "INFO"),
        help="Log level for the rotating log file.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open a web browser at startup.",
    )
    parser.add_argument(
        "--browser-delay",
        type=float,
        default=float(os.environ.get("CHAIR_BRIDGE_BROWSER_DELAY", "0.6")),
        help="Seconds to wait before opening the browser.",
    )
    return parser.parse_args(argv)


def resolve_bind_host(args: argparse.Namespace) -> str:
    if args.local:
        return "127.0.0.1"
    if args.lan and args.host in {"127.0.0.1", "localhost"}:
        return "0.0.0.0"
    return args.host


def _browser_target_url(bind_host: str, port: int, lan_ip: str) -> str:
    """Pick the URL we ask the OS browser to open.

    On the host machine, http://127.0.0.1:<port> is the most reliable
    address — it does not depend on firewall, LAN routing, or whether
    Wi-Fi is actually up. Other devices on the network still use the
    LAN URL shown in the terminal and inside the UI.
    """
    if bind_host in {"127.0.0.1", "localhost", "::1"}:
        return build_public_url(bind_host, port, lan_ip)
    return build_local_url(port)


def open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2, autoraise=True))
    except Exception:
        logging.getLogger("electric_chair_bridge").warning(
            "Auto-open browser failed for %s", url, exc_info=True
        )
        return False


def open_browser_async(url: str, delay_seconds: float) -> threading.Thread:
    def _open() -> None:
        if not open_browser(url):
            print(f"Browser did not open automatically. Open: {url}", file=sys.stderr)

    timer = threading.Timer(max(0.0, delay_seconds), _open)
    timer.daemon = True
    timer.start()
    return timer


def _address_in_use(exc: OSError) -> bool:
    return exc.errno in {errno.EADDRINUSE, 48, 98, 10048}


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    args.host = resolve_bind_host(args)
    configure_logging(args.log_level)
    logger = logging.getLogger("electric_chair_bridge")
    if svg_newer_than_process_start(config.SVG_PATH):
        logger.warning(
            "seq=- SVG mtime is newer than process start: %s", config.SVG_PATH
        )
    lan_ip = get_lan_ip()
    state = ChairState()
    bridge = FirmwareSerialBridge(
        state=state, baud_rate=args.baud, port=args.serial_port
    )
    try:
        server = AppServer(
            (args.host, args.port),
            state=state,
            bridge=bridge,
            svg_markup=load_svg_markup(config.SVG_PATH),
            root_markup=load_root_view_markup(config.ROOT_VIEW_PATH),
            lan_ip=lan_ip,
        )
    except OSError as exc:
        if _address_in_use(exc):
            print(
                f"Port {args.port} is already in use on {args.host}.\n"
                f"Stop the existing panel or run with --port <other-port>.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        raise
    # SIGTERM must trigger the same clean shutdown as Ctrl+C so the bridge
    # gets a chance to send the SoftwareSerial release control to the
    # Arduino. Without this, `pkill app.py` leaves the chair bus owned by
    # the Arduino until somebody physically resets the board.
    def _sigterm_to_interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)

    try:
        bridge.start()
        actual_port = server.server_address[1]
        print_doctor_banner(
            args.host,
            actual_port,
            lan_ip,
            browser_enabled=not args.no_browser,
        )
        if not args.no_browser:
            open_browser_async(
                _browser_target_url(args.host, actual_port, lan_ip),
                args.browser_delay,
            )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        bridge.stop()


if __name__ == "__main__":
    main()
