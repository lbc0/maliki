#!/bin/sh
# Run with: ./start.sh   (or: bash start.sh)
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "تعذّر إيجاد Python 3 على هذا الجهاز. ثبّته ثم أعد المحاولة."
  exit 1
fi
python3 server.py
