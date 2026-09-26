#!/bin/bash
# Double-click this file in Finder to start the editor (macOS).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "تعذّر إيجاد Python 3 على هذا الجهاز."
  echo "ثبّته من https://www.python.org/downloads/ ثم شغّل هذا الملف مرة أخرى."
  read -p "اضغط Enter للإغلاق"
  exit 1
fi
python3 server.py
echo ""
read -p "توقف الخادم. اضغط Enter للإغلاق"
