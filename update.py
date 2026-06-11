from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def is_git_checkout() -> bool:
    return (ROOT / ".git").exists()


def update_git(enabled: bool) -> str:
    if not enabled:
        return "git pull: skipped"
    if not is_git_checkout():
        return "git pull: not a git checkout"
    result = run(["git", "pull", "--ff-only"], check=False)
    if result.returncode == 0:
        return "git pull: ok"
    return "git pull: warning\n" + result.stdout.strip()


def update_dependencies() -> str:
    python = venv_python() if venv_python().exists() else Path(sys.executable)
    if not REQUIREMENTS.exists():
        return "dependencies: requirements.txt missing"
    result = run(
        [str(python), "-m", "pip", "install", "--upgrade", "-r", str(REQUIREMENTS)],
        check=False,
    )
    if result.returncode == 0:
        return "dependencies: ok"
    return "dependencies: failed\n" + result.stdout.strip()


def run_verification() -> str:
    python = venv_python() if venv_python().exists() else Path(sys.executable)
    verifier = ROOT / "tools" / "verify_installation.py"
    if not verifier.exists():
        return "verification: tools/verify_installation.py missing"
    result = run([str(python), str(verifier), "--dry-run"], check=False)
    if result.returncode == 0:
        return "verification: ok"
    return "verification: warning\n" + result.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update NCNI massage chair panel")
    parser.add_argument(
        "--git-pull",
        action="store_true",
        help="Run git pull --ff-only before updating dependencies.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = [
        update_git(args.git_pull),
        update_dependencies(),
        run_verification(),
    ]
    print("Update summary:")
    for item in results:
        print(f"- {item}")
    print("No local files were deleted or reset.")


if __name__ == "__main__":
    main()
