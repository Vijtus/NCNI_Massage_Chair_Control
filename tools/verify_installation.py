from __future__ import annotations

import argparse
import importlib
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "verification_report.txt"

# Make the bridge package importable when this script is run directly
# (e.g. `python3 tools/verify_installation.py`).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Check:
    status: str
    name: str
    detail: str


def add(checks: list[Check], status: str, name: str, detail: str) -> None:
    checks.append(Check(status, name, detail))


def import_check(
    checks: list[Check], module: str, label: str, *, optional: bool = False
) -> None:
    try:
        importlib.import_module(module)
    except Exception as exc:
        status = "WARNING" if optional else "FAILED"
        add(checks, status, label, f"import failed: {exc}")
    else:
        add(checks, "PASSED", label, "import ok")


def file_check(checks: list[Check], relpath: str) -> None:
    path = ROOT / relpath
    add(
        checks,
        "PASSED" if path.exists() else "FAILED",
        relpath,
        "exists" if path.exists() else "missing",
    )


def serial_ports_detail() -> tuple[str, str]:
    try:
        from serial.tools import list_ports
    except Exception as exc:
        return "WARNING", f"serial port scan unavailable: {exc}"
    ports = list(list_ports.comports())
    if not ports:
        return "HARDWARE NOT CONNECTED", "no serial ports detected"
    names = ", ".join(port.device for port in ports)
    return "PASSED", f"serial ports detected: {names}"


def dry_server_check(checks: list[Check]) -> None:
    try:
        from bridge.firmware import FirmwareSerialBridge
        from bridge.http_server import AppServer, get_lan_ip
        from bridge.state import ChairState
        from bridge.svg import load_root_view_markup, load_svg_markup
        import bridge.config as config
    except Exception as exc:
        add(checks, "FAILED", "server imports", f"import failed: {exc}")
        return

    state = ChairState()
    bridge = FirmwareSerialBridge(state=state, baud_rate=115200)
    try:
        server = AppServer(
            ("127.0.0.1", 0),
            state=state,
            bridge=bridge,
            svg_markup=load_svg_markup(config.SVG_PATH),
            root_markup=load_root_view_markup(config.ROOT_VIEW_PATH),
            lan_ip=get_lan_ip(),
        )
    except Exception as exc:
        if isinstance(exc, PermissionError):
            add(checks, "NOT TESTED", "server dry construction", str(exc))
        else:
            add(checks, "FAILED", "server dry construction", str(exc))
        return

    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for route in ("/", "/network", "/debug", "/api/state", "/api/network"):
            url = f"http://127.0.0.1:{port}{route}"
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    detail = f"HTTP {response.status}"
                    status = "PASSED" if response.status == 200 else "FAILED"
            except Exception as exc:
                status = "FAILED"
                detail = str(exc)
            add(checks, status, f"route {route}", detail)

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/qr.svg", timeout=2.0
            ) as response:
                add(checks, "PASSED", "route /qr.svg", f"HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                add(checks, "WARNING", "route /qr.svg", "QR dependency unavailable")
            else:
                add(checks, "FAILED", "route /qr.svg", f"HTTP {exc.code}")
        except Exception as exc:
            add(checks, "FAILED", "route /qr.svg", str(exc))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def run_checks() -> list[Check]:
    checks: list[Check] = []
    version_ok = sys.version_info >= (3, 10)
    add(
        checks,
        "PASSED" if version_ok else "FAILED",
        "Python version",
        sys.version.split()[0],
    )
    for relpath in (
        "app.py",
        "README.md",
        "requirements.txt",
        "install.py",
        "update.py",
        "START_WINDOWS.bat",
        "START_MACOS.command",
        "START_LINUX.sh",
        "bridge/config.py",
        "bridge/http_server.py",
        "static/root-view.js",
        "static/app.css",
        "static/debug.html",
        "assets/display/massage_display_interface.svg",
        "docs/CREDITS.md",
        "docs/SAFETY.md",
        "docs/BASIC_TROUBLESHOOTING.md",
    ):
        file_check(checks, relpath)

    import_check(checks, "serial", "pyserial")
    import_check(checks, "qrcode", "qrcode", optional=True)
    import_check(checks, "bridge.config", "bridge config")

    try:
        from bridge.http_server import get_lan_ip

        lan_ip = get_lan_ip()
        status = "PASSED" if lan_ip else "WARNING"
        add(checks, status, "LAN IP detection", lan_ip or "no address detected")
    except Exception as exc:
        add(checks, "WARNING", "LAN IP detection", str(exc))

    try:
        webbrowser.get()
    except Exception as exc:
        add(checks, "WARNING", "default browser", f"not available: {exc}")
    else:
        add(checks, "PASSED", "default browser", "webbrowser controller available")

    status, detail = serial_ports_detail()
    add(checks, status, "serial ports", detail)
    dry_server_check(checks)
    add(
        checks,
        "PHYSICAL CHAIR NOT VALIDATED",
        "physical chair validation",
        "not performed by this verification script",
    )
    return checks


def write_report(checks: list[Check], output: Path) -> None:
    lines = [
        "NCNI Massage Chair Control Panel - Verification Report",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for check in checks:
        lines.append(f"[{check.status}] {check.name}: {check.detail}")
    lines.extend(
        [
            "",
            "Hardware note: serial-port detection is not physical chair testing.",
            "Physical chair not validated unless a human performs the manual QA.",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the NCNI chair panel install")
    parser.add_argument("--dry-run", action="store_true", help="Do not start hardware")
    parser.add_argument("--output", default=str(REPORT), help="Report output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    checks = run_checks()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    write_report(checks, output)
    failed = [check for check in checks if check.status == "FAILED"]
    print(f"Verification report written: {output}")
    print(f"Checks: {len(checks)} total, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
