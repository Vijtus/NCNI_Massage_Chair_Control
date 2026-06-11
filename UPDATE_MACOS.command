#!/bin/sh
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
  ".venv/bin/python" update.py "$@"
else
  python3 update.py "$@"
fi

read -r -p "Press Enter to close..." _
