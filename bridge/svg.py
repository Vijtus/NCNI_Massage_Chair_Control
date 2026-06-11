from __future__ import annotations

import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from . import config


def load_svg_markup(path: Path) -> str:
    tree = ET.parse(path)
    root = tree.getroot()
    for element in root.iter():
        label = element.attrib.get(f"{{{config.INKSCAPE_NS}}}label")
        if label:
            element.set("data-layer", label)
    return ET.tostring(root, encoding="unicode")


# Overlay injected into ROOT-VIEW.html on serve. Adds:
#   * a universal font stack (system-ui-first) so Polish labels render
#     identically across Windows / macOS / Linux / Android / iOS,
#   * label-overflow protection so multi-line button text stays inside
#     the button shape (fixes the issues shown in Problem.png),
#   * a balanced power-icon stroke so the centre line stops looking
#     like a fat "I",
#   * an in-UI NCNI / LAN / Support card in the bottom-right corner,
#   * a small project-credit line in the bottom-left corner,
#   * a fetch to /api/network so the LAN URL is filled in client-side.
_OVERLAY_HEAD = """\
<style id="ncni-overlay-style">
  :root {
    --ncni-font-stack: system-ui, -apple-system, BlinkMacSystemFont,
                       "Segoe UI", Roboto, Helvetica, Arial,
                       "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
  }
  body, .label, .btn, .power, .ncni-info, .ncni-info * {
    font-family: var(--ncni-font-stack);
  }
  .label {
    overflow-wrap: anywhere;
    word-break: break-word;
    text-wrap: balance;
    hyphens: auto;
    line-height: 1.05;
    max-inline-size: 100%;
    padding-inline: .45em;
  }
  .label > span { display: block; line-height: 1.02; }
  .power-svg .power__stroke { stroke-width: 6; stroke-linecap: round; stroke-linejoin: round; }
  .power-svg line.power__stroke { stroke-width: 5; }
  .ncni-info {
    position: fixed;
    inset-inline-end: clamp(.4rem, 1.2vmin, 1rem);
    inset-block-end: clamp(.4rem, 1.2vmin, 1rem);
    z-index: 30;
    display: grid;
    gap: .25rem;
    max-inline-size: min(22rem, 42vw);
    padding: .55rem .75rem;
    border: 1px solid rgba(255, 255, 255, .22);
    border-radius: .55rem;
    background: rgba(0, 0, 0, .68);
    color: #fff;
    font-size: clamp(.7rem, 1.4vmin, .9rem);
    line-height: 1.3;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
  }
  .ncni-info__title { font-weight: 600; color: #ffd9c8; letter-spacing: .02em; }
  .ncni-info__row { display: flex; gap: .35rem; align-items: baseline; }
  .ncni-info__label { color: rgba(255, 255, 255, .72); font-size: .92em; }
  .ncni-info__value { font-weight: 600; overflow-wrap: anywhere; word-break: break-all; }
  .ncni-info__value a { color: #ffd9c8; text-decoration: none; }
  .ncni-info__value a:hover { text-decoration: underline; }
  .ncni-info__links { display: flex; gap: .5rem; flex-wrap: wrap; margin-block-start: .15rem; }
  .ncni-info__links a {
    display: inline-block; padding: .15rem .5rem;
    border: 1px solid rgba(255, 255, 255, .25); border-radius: .4rem;
    color: #fff; text-decoration: none; font-size: .85em;
  }
  .ncni-info__links a:hover { border-color: #ffd9c8; color: #ffd9c8; }
  .ncni-info__credit { color: rgba(255, 255, 255, .55); font-size: .78em; margin-block-start: .2rem; }
  .ncni-credit {
    position: fixed;
    inset-inline-start: clamp(.4rem, 1.2vmin, 1rem);
    inset-block-end: clamp(.4rem, 1.2vmin, 1rem);
    z-index: 30;
    color: rgba(255, 255, 255, .55);
    font-size: clamp(.62rem, 1.1vmin, .76rem);
  }
  .ncni-credit a { color: rgba(255, 255, 255, .72); text-decoration: none; }
  .ncni-credit a:hover { color: #ffd9c8; text-decoration: underline; }
  @media (orientation: portrait), (max-aspect-ratio: 6 / 5) and (max-width: 900px) {
    .ncni-info, .ncni-credit { z-index: 5; }
  }
  @media (max-width: 380px) {
    .ncni-info { display: none; }
  }
</style>
"""

_OVERLAY_BODY = """\
<aside class="ncni-info" aria-label="Connection and support">
  <div class="ncni-info__title">NCNI Massage Chair</div>
  <div class="ncni-info__row">
    <span class="ncni-info__label">LAN:</span>
    <span class="ncni-info__value"><a id="ncniLanLink" href="#">detecting…</a></span>
  </div>
  <div class="ncni-info__row">
    <span class="ncni-info__label">Port:</span>
    <span class="ncni-info__value" id="ncniPort">—</span>
  </div>
  <div class="ncni-info__links">
    <a href="/network" title="Show LAN address and QR">LAN&nbsp;page</a>
    <a href="/debug" title="Engineering view">Debug</a>
    <a href="/qr.svg" title="QR code (SVG)">QR</a>
  </div>
  <div class="ncni-info__credit">Same Wi-Fi/LAN required</div>
</aside>
<div class="ncni-credit" aria-label="Project credits">
  NCNI Wrocław · software/hardware mods:
  <a href="https://www.vijtus.com" target="_blank" rel="noopener">Wiktor &ldquo;Vijtus&rdquo; Dębowski</a>
</div>
<script id="ncni-overlay-script">
(function () {
  function paint(data) {
    var lanUrl = data && data.lan_url ? data.lan_url : null;
    var displayUrl = data && data.local_only && data.local_url ? data.local_url : lanUrl;
    var link = document.getElementById('ncniLanLink');
    var port = document.getElementById('ncniPort');
    if (link) {
      if (displayUrl) { link.textContent = displayUrl; link.href = displayUrl; }
      else { link.textContent = 'open this address on same Wi-Fi'; link.removeAttribute('href'); }
    }
    if (port && data && data.port) port.textContent = String(data.port);
  }
  fetch('/api/network', { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d) paint(d); })
    .catch(function () {});
})();
</script>
"""


def _inject_overlay(html: str) -> str:
    if 'class="connection-panel"' in html:
        return html
    if "ncni-overlay-style" not in html:
        html = html.replace("</head>", _OVERLAY_HEAD + "</head>", 1)
    if 'class="ncni-info"' not in html:
        html = html.replace("</body>", _OVERLAY_BODY + "\n</body>", 1)
    return html


def load_root_view_markup(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    html = re.sub(
        r"<script>[\s\S]*?</script>\s*</body>",
        '  <script src="/root-view.js" defer></script>\n</body>',
        html,
        count=1,
    )
    return _inject_overlay(html)


def svg_newer_than_process_start(
    path: Path, process_started_at: float | None = None
) -> bool:
    started = process_started_at if process_started_at is not None else time.time()
    return path.exists() and path.stat().st_mtime > started
