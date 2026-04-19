#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_VENV="${ROOT_DIR}/.build-venv"
DIST_DIR="${ROOT_DIR}/dist"

python3 -m venv "${BUILD_VENV}"
source "${BUILD_VENV}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${ROOT_DIR}/shared-backend/requirements-build.txt"

rm -rf "${ROOT_DIR}/build" "${DIST_DIR}/PocketMac" "${DIST_DIR}/PocketMac.app"

pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name PocketMac \
  --osx-bundle-identifier com.vira2734.pocketmac \
  --hidden-import app.main \
  --hidden-import sqlite3 \
  --hidden-import _sqlite3 \
  --hidden-import qrcode \
  --hidden-import qrcode.image.svg \
  --add-data "${ROOT_DIR}/shared-backend/app:app" \
  --add-data "${ROOT_DIR}/shared-backend/web:web" \
  "${ROOT_DIR}/shared-backend/pocketcodex_desktop.py"

echo
echo "Built app bundle:"
echo "  ${DIST_DIR}/PocketMac.app"
