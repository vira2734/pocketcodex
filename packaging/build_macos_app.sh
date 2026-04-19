#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_VENV="${ROOT_DIR}/.build-venv"
DIST_DIR="${ROOT_DIR}/dist"
APP_NAME="PocketMac"
APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${APP_NAME}.dmg"
BUILD_TMP_DIR="${ROOT_DIR}/.packaging-tmp"
BUNDLED_TOOLS_DIR="${BUILD_TMP_DIR}/bundled-tools"
ARCH="$(uname -m)"

mkdir -p "${BUILD_TMP_DIR}"

python3 -m venv "${BUILD_VENV}"
source "${BUILD_VENV}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${ROOT_DIR}/shared-backend/requirements-build.txt"

rm -rf "${ROOT_DIR}/build" "${DIST_DIR}/${APP_NAME}" "${APP_BUNDLE}" "${DMG_PATH}" "${BUNDLED_TOOLS_DIR}"
mkdir -p "${BUNDLED_TOOLS_DIR}"

download_cloudflared() {
  local url
  case "${ARCH}" in
    arm64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
      ;;
    x86_64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
      ;;
    *)
      echo "Unsupported macOS architecture: ${ARCH}" >&2
      exit 1
      ;;
  esac

  local archive="${BUILD_TMP_DIR}/cloudflared.tgz"
  curl -fsSL "${url}" -o "${archive}"
  tar -xzf "${archive}" -C "${BUNDLED_TOOLS_DIR}"
  chmod +x "${BUNDLED_TOOLS_DIR}/cloudflared"
}

download_node_and_localtunnel() {
  local node_version
  node_version="$(
    python3 - <<'PY'
import json, urllib.request

with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=20) as response:
    releases = json.load(response)

for release in releases:
    lts = release.get("lts")
    files = release.get("files") or []
    if lts and "osx-arm64-tar" in files and "osx-x64-tar" in files:
        print(release["version"])
        break
else:
    raise SystemExit("Unable to find an LTS Node.js release with macOS tarballs.")
PY
  )"

  local node_filename
  case "${ARCH}" in
    arm64)
      node_filename="node-${node_version}-darwin-arm64.tar.gz"
      ;;
    x86_64)
      node_filename="node-${node_version}-darwin-x64.tar.gz"
      ;;
    *)
      echo "Unsupported macOS architecture: ${ARCH}" >&2
      exit 1
      ;;
  esac

  local node_url="https://nodejs.org/dist/${node_version}/${node_filename}"
  local node_archive="${BUILD_TMP_DIR}/${node_filename}"
  curl -fsSL "${node_url}" -o "${node_archive}"
  tar -xzf "${node_archive}" -C "${BUILD_TMP_DIR}"

  local extracted_dir="${BUILD_TMP_DIR}/node-${node_version}-darwin-${ARCH/x86_64/x64}"
  mkdir -p "${BUNDLED_TOOLS_DIR}/node"
  cp -R "${extracted_dir}/bin" "${BUNDLED_TOOLS_DIR}/node/"
  cp -R "${extracted_dir}/lib" "${BUNDLED_TOOLS_DIR}/node/"
  cp -R "${extracted_dir}/share" "${BUNDLED_TOOLS_DIR}/node/"

  mkdir -p "${BUNDLED_TOOLS_DIR}/localtunnel"
  "${BUNDLED_TOOLS_DIR}/node/bin/npm" install \
    --prefix "${BUNDLED_TOOLS_DIR}/localtunnel" \
    --omit=dev \
    --no-bin-links \
    --no-fund \
    --no-audit \
    localtunnel
  rm -rf "${BUNDLED_TOOLS_DIR}/localtunnel/node_modules/.bin"
}

download_cloudflared
download_node_and_localtunnel

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
  --add-data "${BUNDLED_TOOLS_DIR}:bundled-tools" \
  "${ROOT_DIR}/shared-backend/pocketcodex_desktop.py"

TMP_DMG_DIR="$(mktemp -d "${ROOT_DIR}/.dmg-staging.XXXXXX")"
trap 'rm -rf "${TMP_DMG_DIR}" "${BUNDLED_TOOLS_DIR}" "${BUILD_TMP_DIR}/cloudflared.tgz" "${BUILD_TMP_DIR}"/node-*.tar.gz "${BUILD_TMP_DIR}"/node-v*-darwin-*' EXIT
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
