#!/usr/bin/env bash
# =============================================================================
# QConnect-AI Edge :: lab node provisioner
# -----------------------------------------------------------------------------
# Provisions (or removes) a single lab edge node:
#   - verifies docker + docker compose
#   - ensures /opt/qconnect-ai-edge layout and a .env
#   - installs + enables + starts the systemd unit
#
# Usage:
#   sudo ./deploy/install.sh             # install / update (idempotent)
#   sudo ./deploy/install.sh --uninstall # stop, disable, remove the unit
#   ./deploy/install.sh --help
#
# Env overrides:
#   INSTALL_DIR  (default /opt/qconnect-ai-edge)
#   UNIT_NAME    (default qconnect-edge.service)
# =============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/qconnect-ai-edge}"
UNIT_NAME="${UNIT_NAME:-qconnect-edge.service}"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../deploy
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"                  # edge repo root
UNIT_SRC="${SCRIPT_DIR}/systemd/${UNIT_NAME}"

MODE="install"

log()  { printf '\033[1;32m[edge-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[edge-install] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[edge-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown argument: $1 (try --help)" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found. $2"
}

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "this action needs root; re-run with sudo."
  fi
}

# --- common tool checks ------------------------------------------------------
require_cmd docker     "Install Docker Engine first."
if ! docker compose version >/dev/null 2>&1; then
  die "'docker compose' (v2 plugin) not available. Install the compose plugin."
fi
require_cmd systemctl  "This installer targets systemd-based hosts."

# =============================================================================
# UNINSTALL
# =============================================================================
if [[ "${MODE}" == "uninstall" ]]; then
  need_root
  log "uninstalling ${UNIT_NAME}"

  if systemctl list-unit-files | grep -q "^${UNIT_NAME}"; then
    systemctl stop "${UNIT_NAME}" 2>/dev/null || warn "unit was not running"
    systemctl disable "${UNIT_NAME}" 2>/dev/null || true
  else
    warn "unit ${UNIT_NAME} is not installed"
  fi

  if [[ -f "${UNIT_DEST}" ]]; then
    rm -f "${UNIT_DEST}"
    systemctl daemon-reload
    log "removed ${UNIT_DEST}"
  fi

  warn "left ${INSTALL_DIR} (and its data volume) in place on purpose."
  warn "remove it manually if you really want to wipe lab data:"
  warn "    docker compose -f ${INSTALL_DIR}/docker-compose.yml -f ${INSTALL_DIR}/deploy/docker-compose.prod.yml down -v"
  warn "    rm -rf ${INSTALL_DIR}"
  log "uninstall complete."
  exit 0
fi

# =============================================================================
# INSTALL (idempotent)
# =============================================================================
need_root
log "installing edge node into ${INSTALL_DIR}"

[[ -f "${UNIT_SRC}" ]] || die "systemd unit not found: ${UNIT_SRC}"

# 1) layout ------------------------------------------------------------------
if [[ ! -d "${INSTALL_DIR}" ]]; then
  log "creating ${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}"
fi

# Sync the repo (compose files, deploy/, edge/, Dockerfiles) into place.
# Prefer rsync; fall back to cp -a. Skip VCS + local env/data.
log "syncing project files -> ${INSTALL_DIR}"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude '.env' --exclude 'data' \
    --exclude '*.pyc' --exclude '__pycache__' \
    "${REPO_ROOT}/" "${INSTALL_DIR}/"
else
  warn "rsync not found; using cp (will not prune removed files)"
  cp -a "${REPO_ROOT}/." "${INSTALL_DIR}/"
fi

# 2) .env --------------------------------------------------------------------
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  if [[ -f "${INSTALL_DIR}/.env.example" ]]; then
    cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
    warn "created ${INSTALL_DIR}/.env from template."
    warn "EDIT IT NOW: set LAB_ID, LAB_NAME, CLOUD_URL, LAB_AUTH_TOKEN before the stack is useful."
  else
    die "no .env and no .env.example present in ${INSTALL_DIR}; cannot continue."
  fi
else
  log ".env already present; leaving it untouched."
  chmod 600 "${INSTALL_DIR}/.env" || true
fi

# 3) systemd unit ------------------------------------------------------------
log "installing systemd unit -> ${UNIT_DEST}"
install -m 0644 "${UNIT_SRC}" "${UNIT_DEST}"
systemctl daemon-reload
systemctl enable "${UNIT_NAME}"

# 4) start / restart ---------------------------------------------------------
log "starting ${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

log "done. Useful commands:"
log "    systemctl status ${UNIT_NAME}"
log "    journalctl -u ${UNIT_NAME} -f"
log "    (edit config) \$EDITOR ${INSTALL_DIR}/.env  &&  systemctl restart ${UNIT_NAME}"
