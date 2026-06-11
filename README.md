# NCNI Massage Chair Control Panel

Local web control panel for the NCNI massage chair. Click the launcher
for your operating system and the chair panel opens in your browser.

> Software and hardware modifications: **Wiktor "Vijtus" Dębowski** —
> [www.vijtus.com](https://www.vijtus.com)
> for **Naukowe Centrum Neuroinnowacji (NCNI), Wrocław** —
> [www.ncni.pl](https://www.ncni.pl) · kontakt@ncni.pl · +48 600 608 333

---

## What to click

| Operating system | Start (every day)        | Install (once)              | Update (occasional)          |
| ---------------- | ------------------------ | --------------------------- | ---------------------------- |
| Windows          | `START_WINDOWS.bat`      | `INSTALL_WINDOWS.bat`       | `UPDATE_WINDOWS.bat`         |
| macOS            | `START_MACOS.command`    | `INSTALL_MACOS.command`     | `UPDATE_MACOS.command`       |
| Linux            | `START_LINUX.sh`         | `INSTALL_LINUX.sh`          | `UPDATE_LINUX.sh`            |

## What should happen

1. A small terminal window opens.
2. The default web browser opens on the chair control panel.
3. Use the panel exactly like the chair's own touch screen.
4. Stop the panel with `Ctrl+C` in the terminal window.

## Open it on a phone or tablet

* The phone/tablet must be on the **same Wi-Fi/LAN** as the computer
  that started the panel.
* On the panel, tap the small **Sieć** button (top-right corner) — it
  opens a window with the LAN address, IP, port, QR code, and a
  Copy button.
* Open that LAN address in the phone's browser, or scan the QR code.
* Use **landscape orientation**. Portrait shows a "Rotate device"
  overlay on purpose.

## Troubleshooting

| Symptom                            | What to do                                                       |
| ---------------------------------- | ---------------------------------------------------------------- |
| Browser does not open              | Read the LAN address from the terminal and open it manually.     |
| Phone cannot connect               | Confirm same Wi-Fi/LAN; allow Python through the firewall.       |
| "Port already in use"              | Close the other panel, or run with `--port 8081`.                |
| QR code missing                    | Run the install launcher again.                                  |
| Chair does not respond             | Check the USB cable, then `tools/verify_installation.py`.        |

More detail: [`docs/BASIC_TROUBLESHOOTING.md`](docs/BASIC_TROUBLESHOOTING.md).

## Support

NCNI Wrocław · **kontakt@ncni.pl** · **+48 600 608 333**
[www.ncni.pl](https://www.ncni.pl)

---

## Developer notes

> The sections below are for the technical team that maintains the
> panel. A doctor or therapist does not need to read them.

### Project structure

```text
START_WINDOWS.bat / START_MACOS.command / START_LINUX.sh   launchers
INSTALL_*.* / UPDATE_*.*                                   one-shot helpers
app.py                                                     entry point
install.py / update.py                                     setup helpers
requirements.txt                                           Python deps
ROOT-VIEW.html                                             main operator UI
bridge/                                                    HTTP + serial bridge
static/                                                    JS, CSS, debug page
assets/display/                                            runtime SVG display
docs/                                                      user + maintainer docs
tools/                                                     verifier, release packager, restart helper
tests/                                                     pytest suite
firmware/electric_chair_firmware/                          PlatformIO firmware
```

### Routes

* `/` — main operator panel.
* `/network` — LAN address + QR + setup page.
* `/api/network` — JSON, used by the in-page **Sieć** modal.
* `/api/state` — current chair state (ETag-cached).
* `/api/command` — `POST` chair button presses.
* `/qr.svg` — QR code for the LAN URL (returns 503 if `qrcode` missing).
* `/debug` — frame timeline / diff for technicians.

### Building a clinic release

```bash
python3 tools/build_release_package.py
```

Produces `dist/NCNI_Massage_Chair_Control/` with only the files a
clinic should see: no `tests/`, no firmware project, no build caches,
no local archive, no `.git`. See
[`docs/file_inventory.md`](docs/file_inventory.md).

### Tests

```bash
ruff format .
ruff check .
pytest
python3 tools/verify_installation.py --dry-run
python3 app.py --help
python3 app.py --no-browser --local      # local smoke
```

### Safety

This drives real motors, recliner actuators, and heating electronics.
Do not run the chair unattended. See [`docs/SAFETY.md`](docs/SAFETY.md).

No firmware command bytes, UART semantics, heating logic, timer
calibration, or A1/A2/b1/b2 verification semantics are changed by
this packaging pass. **Physical chair hardware was not validated in
this pass.**

### Credits

[`docs/CREDITS.md`](docs/CREDITS.md). NCNI ownership; software/hardware
modifications by Wiktor "Vijtus" Dębowski.
