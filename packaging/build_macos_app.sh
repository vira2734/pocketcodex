#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_VENV="${ROOT_DIR}/.build-venv"
DIST_DIR="${ROOT_DIR}/dist"
APP_NAME="PocketMac"
APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${APP_NAME}.dmg"

python3 -m venv "${BUILD_VENV}"
source "${BUILD_VENV}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${ROOT_DIR}/shared-backend/requirements-build.txt"

rm -rf "${ROOT_DIR}/build" "${DIST_DIR}/${APP_NAME}" "${APP_BUNDLE}" "${DMG_PATH}"

pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "${APP_NAME}" \
  --osx-bundle-identifier com.vira2734.pocketmac \
  --hidden-import app.main \
  --hidden-import sqlite3 \
  --hidden-import _sqlite3 \
  --hidden-import qrcode \
  --hidden-import qrcode.image.svg \
  --add-data "${ROOT_DIR}/shared-backend/app:app" \
  --add-data "${ROOT_DIR}/shared-backend/web:web" \
  "${ROOT_DIR}/shared-backend/pocketcodex_desktop.py"

TMP_DMG_DIR="$(mktemp -d "${ROOT_DIR}/.dmg-staging.XXXXXX")"
trap 'rm -rf "${TMP_DMG_DIR}"' EXIT
cp -R "${APP_BUNDLE}" "${TMP_DMG_DIR}/"
ln -s /Applications "${TMP_DMG_DIR}/Applications"
hdiutil create \
  -volname "${APP_NAME}" \
  -srcfolder "${TMP_DMG_DIR}" \
  -ov \
  -format UDZO \
  "${DMG_PATH}" >/dev/null

echo
echo "Built app bundle:"
echo "  ${APP_BUNDLE}"
echo "Built DMG:"
echo "  ${DMG_PATH}"
