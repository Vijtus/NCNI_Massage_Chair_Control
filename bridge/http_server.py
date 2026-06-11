from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config
from .commands import COMMAND_INDEX, HOLD_TO_ADJUST_COMMANDS
from .firmware import FirmwareSerialBridge
from .state import ChairState

qrcode: Any | None
try:
    import qrcode  # type: ignore
    import qrcode.image.svg  # type: ignore

    HAS_QRCODE = True
except ImportError:
    qrcode = None
    HAS_QRCODE = False


def compute_state_etag(snapshot: dict[str, Any]) -> str:
    # Hash a stable subset only. Excludes frame_age_ms, frame_seen_at,
    # time_text, command_history, backend_log, buttons[*].failed (time-decay).
    # remaining_seconds is included so the timer tick invalidates the cache.
    buttons = snapshot.get("buttons") or {}
    failed = snapshot.get("failed_commands") or []
    unverified = snapshot.get("unverified_commands") or []
    drift = snapshot.get("drift") or []
    payload = {
        "connected": snapshot.get("connected"),
        "listening": snapshot.get("listening"),
        "board_ready": snapshot.get("board_ready"),
        "last_error": snapshot.get("last_error"),
        "last_command": snapshot.get("last_command"),
        "power_on": snapshot.get("power_on"),
        "mode": snapshot.get("mode"),
        "auto_profile": snapshot.get("auto_profile"),
        "timer_minutes": snapshot.get("timer_minutes"),
        "remaining_seconds": snapshot.get("remaining_seconds"),
        "raw_frame": snapshot.get("raw_frame"),
        "frame_signature": snapshot.get("frame_signature"),
        "frame_stale": snapshot.get("frame_stale"),
        "levels": snapshot.get("levels"),
        "drift_fields": sorted(item.get("field") for item in drift),
        "failed_seqs": [(item.get("seq"), item.get("command")) for item in failed],
        "unverified_seqs": [
            (item.get("seq"), item.get("command")) for item in unverified
        ],
        "active": {key: value.get("active") for key, value in buttons.items()},
        "blocked": {key: value.get("blocked") for key, value in buttons.items()},
        "bridge_busy": snapshot.get("bridge_busy"),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class AppHandler(BaseHTTPRequestHandler):
    server_version = config.HTTP_SERVER_VERSION

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes(
                self.server.root_markup.encode("utf-8"), "text/html; charset=utf-8"
            )
            return
        if parsed.path in {"/ROOT-VIEW.html", "/root-view"}:
            self._send_bytes(
                self.server.root_markup.encode("utf-8"), "text/html; charset=utf-8"
            )
            return
        if parsed.path == "/static/ROOT-VIEW.html":
            self._serve_file(
                config.STATIC_DIR / "ROOT-VIEW.html", "text/html; charset=utf-8"
            )
            return
        if parsed.path in {"/debug", "/debug.html"}:
            self._serve_file(
                config.STATIC_DIR / "debug.html", "text/html; charset=utf-8"
            )
            return
        if parsed.path == "/static/debug.html":
            self._serve_file(
                config.STATIC_DIR / "debug.html", "text/html; charset=utf-8"
            )
            return
        if parsed.path == "/network":
            html = build_network_page(
                self.server.server_address[1],
                self.server.lan_ip,
                self.server.bind_host,
            )
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/network":
            payload = json.dumps(
                build_network_payload(
                    self.server.server_address[1],
                    self.server.lan_ip,
                    self.server.bind_host,
                )
            ).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/qr.svg":
            svg = generate_qr_svg(self.server.public_url)
            if svg:
                self._send_bytes(svg.encode("utf-8"), "image/svg+xml; charset=utf-8")
            else:
                self.send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "qrcode library not installed"
                )
            return
        if parsed.path == "/app.css":
            self._serve_file(config.STATIC_DIR / "app.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._serve_file(
                config.STATIC_DIR / "app.js", "application/javascript; charset=utf-8"
            )
            return
        if parsed.path == "/root-view.js":
            self._serve_file(
                config.STATIC_DIR / "root-view.js",
                "application/javascript; charset=utf-8",
            )
            return
        if parsed.path == "/display.svg":
            self._send_bytes(
                self.server.svg_markup.encode("utf-8"), "image/svg+xml; charset=utf-8"
            )
            return
        if parsed.path == "/api/state":
            snapshot = self._state_payload()
            etag = f'W/"{compute_state_etag(snapshot)}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            payload = json.dumps(snapshot).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8", etag=etag)
            return
        if parsed.path == "/api/log":
            query = parse_qs(parsed.query)
            since = float(query.get("since", ["0"])[0] or 0)
            payload = json.dumps(self._log_since(since)).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/frames":
            query = parse_qs(parsed.query)
            since_str = query.get("since", [None])[0]
            frames = self._frames_since(since_str)
            payload = json.dumps(frames).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/press_diagnostics":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["25"])[0])
            except (TypeError, ValueError):
                limit = 25
            limit = max(1, min(200, limit))
            entries = self.server.state.press_diagnostics(limit=limit)
            payload = json.dumps({"entries": entries, "count": len(entries)}).encode(
                "utf-8"
            )
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/command":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if self.headers.get("X-Requested-With") != "XMLHttpRequest":
            self.send_error(HTTPStatus.FORBIDDEN, "Missing X-Requested-With")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        command = body.get("command")
        if command not in COMMAND_INDEX:
            self.send_error(HTTPStatus.BAD_REQUEST, "Unknown command")
            return
        action = body.get("action", "press")
        if action not in {"press", "hold_start", "hold_stop"}:
            self.send_error(HTTPStatus.BAD_REQUEST, "Unknown action")
            return
        hold_id = body.get("hold_id")
        if action in {"hold_start", "hold_stop"}:
            if command not in HOLD_TO_ADJUST_COMMANDS:
                self.send_error(HTTPStatus.BAD_REQUEST, "Command is not holdable")
                return
            if not isinstance(hold_id, str) or not (1 <= len(hold_id) <= 80):
                self.send_error(HTTPStatus.BAD_REQUEST, "Missing hold_id")
                return
        elif command in HOLD_TO_ADJUST_COMMANDS:
            self.send_error(HTTPStatus.BAD_REQUEST, "Hold action required")
            return

        if action == "hold_start":
            result = self.server.bridge.send_hold_start(command, hold_id)
            ok = result.ok
            queued = result.queued
            seq = result.seq
            error = result.error
        elif action == "hold_stop":
            result = self.server.bridge.send_hold_stop(command, hold_id)
            ok = result.ok
            queued = result.queued
            seq = result.seq
            error = result.error
        else:
            seq = self.server.bridge.send_command(command)
            ok = seq is not None
            queued = seq is not None
            error = None if seq is not None else "bridge busy or command blocked"
        payload = json.dumps(
            {
                "ok": ok,
                "queued": queued,
                "seq": seq,
                "action": action,
                "error": error,
                "state": self._state_payload(),
            }
        ).encode("utf-8")
        self._send_bytes(payload, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Missing file")
            return
        self._send_bytes(path.read_bytes(), content_type)

    def _send_bytes(
        self, payload: bytes, content_type: str, etag: str | None = None
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(payload)

    def _state_payload(self) -> dict[str, Any]:
        snapshot = self.server.state.snapshot()
        busy = self.server.bridge.is_busy()
        active_hold_command = self.server.bridge.active_hold_command()
        snapshot["bridge_busy"] = busy
        if active_hold_command:
            for command, meta in snapshot["buttons"].items():
                if command == active_hold_command or command == "power":
                    continue
                meta["blocked"] = True
        elif busy:
            for meta in snapshot["buttons"].values():
                meta["blocked"] = True
        return snapshot

    def _log_since(self, since: float) -> dict[str, Any]:
        snapshot = self._state_payload()
        return {
            "since": since,
            "command_history": [
                item
                for item in snapshot["command_history"]
                if float(item.get("at", 0)) > since
            ],
            "backend_log": snapshot["backend_log"],
        }

    def _frames_since(self, since_str: str | None) -> dict[str, Any]:
        with self.server.state.lock:
            history = list(self.server.state.frame_history)
        cutoff = None
        if since_str:
            try:
                cutoff = float(since_str)
            except (ValueError, TypeError):
                cutoff = None
        frames = []
        for rec in history:
            if cutoff is not None and rec.wall_time <= cutoff:
                continue
            frames.append(
                {
                    "ts": rec.wall_time,
                    "iso": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.gmtime(rec.wall_time)
                    )
                    + f".{int((rec.wall_time % 1) * 1_000_000):06d}"
                    + "Z",
                    "mono": rec.monotonic_time,
                    "raw": rec.raw,
                    "signature": rec.signature,
                    "b3b4b5b6": rec.bytes_3_to_6,
                    "mode": rec.mode,
                    "power_on": rec.power_on,
                    "intensity": rec.levels["intensity"],
                    "speed": rec.levels["speed"],
                    "foot_speed": rec.levels["foot_speed"],
                    "zones": rec.zones,
                }
            )
        return {"frames": frames, "count": len(frames)}


class AppServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        state: ChairState,
        bridge: FirmwareSerialBridge,
        svg_markup: str,
        root_markup: str,
        lan_ip: str = "127.0.0.1",
    ) -> None:
        super().__init__(server_address, AppHandler)
        self.state = state
        self.bridge = bridge
        self.svg_markup = svg_markup
        self.root_markup = root_markup
        self.lan_ip = lan_ip
        self.bind_host = self.server_address[0]
        self.public_url = build_public_url(
            self.bind_host, self.server_address[1], lan_ip
        )


def _is_usable_open_host(host: str | None) -> bool:
    return bool(host and host not in {"0.0.0.0", "::"})


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def choose_public_host(bind_host: str, lan_ip: str) -> str:
    if bind_host in {"", "0.0.0.0", "::"}:
        return lan_ip if _is_usable_open_host(lan_ip) else "127.0.0.1"
    if _is_loopback_host(bind_host):
        return "127.0.0.1"
    return bind_host


def build_public_url(
    bind_host: str, port: int, lan_ip: str, scheme: str = "http"
) -> str:
    public_host = choose_public_host(bind_host, lan_ip)
    return f"{scheme}://{_host_for_url(public_host)}:{port}"


def _valid_ipv4(value: str) -> str | None:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if addr.version != 4 or addr.is_unspecified or addr.is_loopback:
        return None
    return str(addr)


def _score_lan_candidate(
    value: str, source_priority: int = 1
) -> tuple[int, int, int, int]:
    addr = ipaddress.ip_address(value)
    scope_score = 2 if addr.is_link_local else 0
    private_score = 0 if addr.is_private else 1
    return (scope_score, source_priority, private_score, int(addr))


def _iter_hostname_ipv4s() -> list[str]:
    candidates: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return candidates
    for info in infos:
        ip = _valid_ipv4(info[4][0])
        if ip and ip not in candidates:
            candidates.append(ip)
    return candidates


def _is_likely_virtual_interface(name: str) -> bool:
    lowered = name.lower()
    return lowered == "lo" or lowered.startswith(
        (
            "br-",
            "cni",
            "docker",
            "flannel",
            "podman",
            "veth",
            "virbr",
        )
    )


def _iter_interface_ipv4s() -> list[str]:
    candidates: list[str] = []
    try:
        import fcntl
        import struct
    except ImportError:
        return candidates

    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return candidates

    for _index, name in interfaces:
        if _is_likely_virtual_interface(name):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                request = struct.pack("256s", name[:15].encode("utf-8"))
                raw = fcntl.ioctl(sock.fileno(), 0x8915, request)
                ip = _valid_ipv4(socket.inet_ntoa(raw[20:24]))
        except OSError:
            continue
        if ip and ip not in candidates:
            candidates.append(ip)
    return candidates


def _ip_from_udp_route(target: str) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect((target, 80))
            return _valid_ipv4(sock.getsockname()[0])
    except OSError:
        return None


def get_lan_ip() -> str:
    candidates: dict[str, int] = {}

    def add_candidate(ip: str | None, source_priority: int) -> None:
        if not ip:
            return
        current = candidates.get(ip)
        if current is None or source_priority < current:
            candidates[ip] = source_priority

    # UDP connect does not send packets. These targets only ask the OS which
    # local address it would use for normal LAN/default-route traffic.
    for target in ("192.0.2.1", "224.0.0.1", "8.8.8.8"):
        add_candidate(_ip_from_udp_route(target), 0)

    for ip in _iter_interface_ipv4s():
        add_candidate(ip, 1)
    for ip in _iter_hostname_ipv4s():
        add_candidate(ip, 2)

    if candidates:
        return min(
            candidates,
            key=lambda ip: _score_lan_candidate(ip, candidates[ip]),
        )
    return "127.0.0.1"


def _make_qr(data: str) -> Any | None:
    if not HAS_QRCODE or qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def generate_terminal_qr(data: str) -> str | None:
    qr = _make_qr(data)
    if qr is None:
        return None
    matrix = qr.get_matrix()
    return "\n".join(
        "".join("██" if cell else "  " for cell in row).rstrip() for row in matrix
    )


def generate_qr_svg(data: str) -> str | None:
    qr = _make_qr(data)
    if qr is None or qrcode is None:
        return None
    import io

    factory = qrcode.image.svg.SvgPathImage
    img = qr.make_image(image_factory=factory)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def build_network_page(
    port: int, lan_ip: str, bind_host: str = config.DEFAULT_HOST
) -> str:
    network = build_network_payload(port, lan_ip, bind_host)
    public_url = network["public_url"]
    local_url = network["local_url"]
    qr_note = (
        "QR code opens the LAN panel address."
        if network["qr_available"]
        else "QR support is not installed. Use the LAN address above."
    )
    return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>NCNI Massage Chair - Network Access</title>
  <style>
    :root {{ --bg: #081217; --panel: #10232c; --ink: #e9f5ff; --muted: #9ab5c7; --accent: #66e0ff; --radius: 8px; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100vh; background: var(--bg); color: var(--ink); font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
    .wrap {{ max-width: 600px; margin: 0 auto; padding: 40px 24px; text-align: center; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 8px; }}
    .sub {{ color: var(--muted); margin-bottom: 32px; }}
    .card {{ background: var(--panel); border: 1px solid rgba(255,255,255,0.08); border-radius: var(--radius); padding: 24px; margin-bottom: 20px; }}
    .card h2 {{ font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin: 0 0 10px; }}
    .url {{ font-size: 1.3rem; color: var(--accent); word-break: break-all; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    #qr {{ margin: 20px auto; display: inline-block; background: #fff; padding: 16px; border-radius: var(--radius); }}
    #qr img, #qr svg {{ display: block; width: 240px; height: 240px; }}
    .back {{ display: inline-block; margin-top: 24px; padding: 12px 24px; border: 1px solid rgba(255,255,255,0.12); border-radius: var(--radius); color: var(--ink); text-decoration: none; }}
    .back:hover {{ border-color: var(--accent); }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>NCNI Massage Chair</h1>
    <p class="sub">Open the panel from another device on the same Wi-Fi/LAN.</p>
    <div class="card">
      <h2>LAN panel address</h2>
      <p class="url"><a href="{public_url}">{public_url}</a></p>
    </div>
    <div class="card">
      <h2>This computer</h2>
      <p class="url"><a href="{local_url}">{local_url}</a></p>
    </div>
    <div class="card">
      <h2>Scan QR Code</h2>
      <div id="qr"><img id="qrImg" src="/qr.svg" alt="QR code for LAN URL" onerror="this.parentElement.innerHTML='<p style=\\'color:#000;font-size:1rem;padding:8px\\'>QR unavailable. Install: pip install qrcode</p>'"></div>
      <p class="sub">{qr_note}</p>
    </div>
    <a class="back" href="/">Back to Control Panel</a>
  </div>
</body>
</html>"""


def build_network_payload(
    port: int, lan_ip: str, bind_host: str = config.DEFAULT_HOST
) -> dict[str, Any]:
    public_url = build_public_url(bind_host, port, lan_ip)
    local_url = build_public_url(config.LOOPBACK_HOST, port, lan_ip)
    local_only = _is_loopback_host(bind_host)
    panel_url = local_url if local_only else public_url
    return {
        "product": "NCNI Massage Chair Control Panel",
        "public_url": panel_url,
        "lan_url": public_url,
        "local_url": local_url,
        "lan_ip": "127.0.0.1" if local_only else choose_public_host(bind_host, lan_ip),
        "port": port,
        "local_only": local_only,
        "debug_url": f"{panel_url}/debug",
        "network_url": f"{panel_url}/network",
        "qr_url": "/qr.svg",
        "qr_available": HAS_QRCODE,
        "serial_url": f"{panel_url}/debug",
        "same_lan_note": "Same Wi-Fi/LAN required",
        "support": {
            "organization": "Naukowe Centrum Neuroinnowacji",
            "short_name": "NCNI Wroclaw",
            "phone": "+48 600 608 333",
            "email": "kontakt@ncni.pl",
            "website": "https://www.ncni.pl",
        },
    }
