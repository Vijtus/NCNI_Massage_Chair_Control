#!/bin/sh
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  python3 install.py
else
  echo
  echo "Python 3 was not found on this Mac."
  echo "Install Python 3 from https://www.python.org/downloads/ and run again."
  echo
fi

read -r -p "Press Enter to close..." _
