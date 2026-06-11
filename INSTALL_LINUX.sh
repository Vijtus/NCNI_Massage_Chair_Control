#!/bin/sh
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  exec python3 install.py
fi

echo
echo "Python 3 was not found."
echo "Install with your package manager, then run this file again."
echo "  Debian/Ubuntu:  sudo apt install python3 python3-pip python3-venv"
echo "  Fedora:         sudo dnf install python3 python3-pip"
echo "  Arch:           sudo pacman -S python python-pip"
echo
exit 1
