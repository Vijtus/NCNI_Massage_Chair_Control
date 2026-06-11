"""Build a clinic-ready release package.

Result: ``dist/NCNI_Massage_Chair_Control/`` with a deliberately tiny
top-level — only what a doctor or therapist actually needs to see:

    NCNI_Massage_Chair_Control/
        README.md
        START_WINDOWS.bat
        START_MACOS.command
        START_LINUX.sh
        app/                    ← everything else lives in here, do not touch

The three top-level START launchers auto-bootstrap a virtualenv and
dependencies on first run, so the doctor never needs to find or open
INSTALL_*. INSTALL_* / UPDATE_* are kept inside ``app/`` for the
clinic technician.

Usage:
    python3 tools/build_release_package.py
    python3 tools/build_release_package.py --output X
    python3 tools/build_release_package.py --zip
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_NAME = "NCNI_Massage_Chair_Control"

# Files that go into <release>/app/ from the source repo root.
APP_FILES = [
    "app.py",
    "install.py",
    "update.py",
    "requirements.txt",
    "ROOT-VIEW.html",
    "INSTALL_WINDOWS.bat",
    "INSTALL_MACOS.command",
    "INSTALL_LINUX.sh",
    "UPDATE_WINDOWS.bat",
    "UPDATE_MACOS.command",
    "UPDATE_LINUX.sh",
]

APP_DIRS = [
    "bridge",
    "static",
    "assets",
    "tools",
]

# Doctor-relevant docs subset; lands at app/docs/.
APP_DOCS = ["CREDITS.md", "SAFETY.md", "BASIC_TROUBLESHOOTING.md"]

# Skipped during copytree.
EXCLUDE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".pio",
    ".vscode",
    "node_modules",
    "build_release_package.py",  # don't ship the packager itself
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".swp", ".swo", ".kate-swp"}

# Top-level paths that must NEVER appear anywhere in the release tree.
FORBIDDEN_DIR_NAMES = {
    ".git",
    ".github",
    ".claude",
    ".codex",
    "tests",
    "developer",
    "COORDINATION",
    "_archive_unused",
    "electric_chair_firmware",
    "notes",
    "dist",
}
FORBIDDEN_FILES = {
    "verification_report.txt",
    ".codex",
}


# ---------------------------------------------------------------------------
# Top-level launchers (live at <release>/, exec into <release>/app/).
# Each launcher:
#   * cd's into its own directory (so double-click works);
#   * if app/.venv exists, uses it;
#   * else if app/ has install.py, runs install.py once (auto-bootstrap);
#   * then runs app/app.py.
# ---------------------------------------------------------------------------

START_WINDOWS_BAT = r"""@echo off
setlocal
cd /d "%~dp0app"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup. This may take a minute...
  where py >nul 2>nul && (py -3 install.py) || (python install.py)
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py %*
) else (
  echo.
  echo Could not start. Open app\INSTALL_WINDOWS.bat and try again.
  pause
  exit /b 1
)

if errorlevel 1 (
  echo.
  echo The panel stopped with an error. Run app\INSTALL_WINDOWS.bat to repair.
  pause
)
"""

START_MACOS_COMMAND = r"""#!/bin/sh
cd "$(dirname "$0")/app" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "First-time setup. This may take a minute..."
  python3 install.py || exit 1
fi

exec ".venv/bin/python" app.py "$@"
"""

START_LINUX_SH = r"""#!/bin/sh
cd "$(dirname "$0")/app" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "First-time setup. This may take a minute..."
  python3 install.py || exit 1
fi

exec ".venv/bin/python" app.py "$@"
"""

LAUNCHERS = {
    "START_WINDOWS.bat": START_WINDOWS_BAT,
    "START_MACOS.command": START_MACOS_COMMAND,
    "START_LINUX.sh": START_LINUX_SH,
}

# Tiny doctor-facing README written into the release root. The full
# README in the source repo is much longer (developer notes too).
RELEASE_README = """\
# NCNI Massage Chair Control Panel

## Click one of these:

| Your computer | Double-click               |
| ------------- | -------------------------- |
| Windows       | `START_WINDOWS.bat`        |
| macOS         | `START_MACOS.command`      |
| Linux         | `START_LINUX.sh`           |

A small terminal window opens, the panel sets itself up the first
time, and your default browser opens on the chair control panel.
Use it in **landscape** orientation. Stop with `Ctrl+C`.

## Open it on a phone or tablet

1. Phone/tablet must be on the **same Wi-Fi/LAN** as the computer.
2. On the panel, tap the small **Sieć** button (top-right corner).
3. Use the LAN address shown there, or scan the QR.

## If something breaks

* `app/docs/BASIC_TROUBLESHOOTING.md`
* Or contact NCNI: **kontakt@ncni.pl** · **+48 600 608 333**

## Folder layout

```
START_WINDOWS.bat / START_MACOS.command / START_LINUX.sh   ← double-click these
README.md                                                  ← this file
app/                                                       ← do not touch
```

Inside `app/` are the Python program, the SVG display, the operator
panel HTML, and the install/update helpers for technicians.

---

Software and hardware modifications: **Wiktor "Vijtus" Dębowski**
([www.vijtus.com](https://www.vijtus.com))
for **Naukowe Centrum Neuroinnowacji (NCNI), Wrocław**
([www.ncni.pl](https://www.ncni.pl)).
"""


def _copy_filter(_src: str, names: list[str]) -> list[str]:
    skip: list[str] = []
    for name in names:
        if name in EXCLUDE_NAMES or name in FORBIDDEN_DIR_NAMES:
            skip.append(name)
            continue
        if any(name.endswith(suffix) for suffix in EXCLUDE_SUFFIXES):
            skip.append(name)
            continue
        if name in FORBIDDEN_FILES:
            skip.append(name)
    return skip


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_copy_filter, dirs_exist_ok=True)


def write_text(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def build(out_root: Path) -> Path:
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)
    app_dir = out_root / "app"
    app_dir.mkdir()

    # Top-level: README + three START launchers.
    write_text(out_root / "README.md", RELEASE_README)
    write_text(out_root / "START_WINDOWS.bat", LAUNCHERS["START_WINDOWS.bat"])
    write_text(
        out_root / "START_MACOS.command",
        LAUNCHERS["START_MACOS.command"],
        executable=True,
    )
    write_text(
        out_root / "START_LINUX.sh", LAUNCHERS["START_LINUX.sh"], executable=True
    )

    # Everything else under app/.
    for rel in APP_FILES:
        src = ROOT / rel
        if not src.exists():
            print(f"  [SKIP] {rel} (missing in source tree)")
            continue
        dst = app_dir / rel
        copy_file(src, dst)
        if rel.endswith((".sh", ".command")):
            dst.chmod(0o755)
    for rel in APP_DIRS:
        src = ROOT / rel
        if not src.exists():
            print(f"  [SKIP] {rel}/ (missing)")
            continue
        copy_tree(src, app_dir / rel)
    docs_dst = app_dir / "docs"
    docs_dst.mkdir(exist_ok=True)
    for name in APP_DOCS:
        src = ROOT / "docs" / name
        if src.exists():
            copy_file(src, docs_dst / name)

    # Hard-fail if anything forbidden leaked anywhere in the tree.
    for path in out_root.rglob("*"):
        rel = path.relative_to(out_root).as_posix()
        parts = rel.split("/")
        for bad in FORBIDDEN_DIR_NAMES:
            if bad in parts:
                raise SystemExit(f"Release contains forbidden path: {rel}")
        if path.is_file() and path.name in FORBIDDEN_FILES:
            raise SystemExit(f"Release contains forbidden file: {rel}")

    # Top-level must contain exactly README + three launchers (+ app/).
    expected = {
        "README.md",
        "START_WINDOWS.bat",
        "START_MACOS.command",
        "START_LINUX.sh",
        "app",
    }
    actual = {p.name for p in out_root.iterdir()}
    extra = actual - expected
    missing = expected - actual
    if extra or missing:
        raise SystemExit(
            f"Top-level mismatch — extra: {sorted(extra)}, missing: {sorted(missing)}"
        )

    return out_root


def make_zip(out_root: Path) -> Path:
    archive = out_root.with_suffix(".zip")
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_root.parent))
    return archive


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        default=str(ROOT / "dist" / RELEASE_NAME),
        help="Release folder (default: ./dist/NCNI_Massage_Chair_Control)",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Also produce a .zip alongside the release folder.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.output).resolve()
    print(f"Building clinic release at {out} ...")
    build(out)
    print()
    print("Top-level (what a doctor sees):")
    for entry in sorted(out.iterdir()):
        suffix = "/" if entry.is_dir() else ""
        print(f"  {entry.name}{suffix}")
    if args.zip:
        archive = make_zip(out)
        print(f"\nWrote zip: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
