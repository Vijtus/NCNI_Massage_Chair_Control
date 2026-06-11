# File Inventory

Updated: 2026-06-11.

This project keeps the GitHub tree small enough to understand, while the
clinic release package stays even smaller.

## Root Files

- `README.md` - start here; written for normal users first.
- `START_WINDOWS.bat`, `START_MACOS.command`, `START_LINUX.sh` - everyday launchers.
- `INSTALL_*` and `UPDATE_*` - one-shot setup and update helpers.
- `app.py` - starts the local web server and browser.
- `install.py`, `update.py`, `requirements.txt` - Python setup.
- `ROOT-VIEW.html` - main operator panel served at `/`.

## Source Folders

- `bridge/` - HTTP routes, chair state model, serial bridge, SVG loading.
- `static/` - browser JavaScript, CSS, support view, and debug page.
- `assets/display/` - runtime SVG display used by the panel.
- `firmware/electric_chair_firmware/` - PlatformIO firmware source and firmware tests.
- `tools/` - verification, release packaging, and technician restart helper.
- `tests/` - Python regression tests.
- `docs/` - troubleshooting, safety, architecture, credits, and this inventory.

## Local-Only Archive

- `to_trash/` is ignored by git and is not part of the GitHub project.
- The 2026-06-11 cleanup moved stale reference files, old local context,
  generated caches, and build output under `to_trash/github_cleanup_2026-06-11/`.
- Nothing was permanently deleted by this cleanup.

## Generated Files That Should Stay Out Of Git

- `__pycache__/`
- `.pytest_cache/`
- `.ruff_cache/`
- `.pio/`
- `.venv/`
- `dist/`
- `verification_report.txt`
- `*.zip`, `*.hex`, `*.elf`
