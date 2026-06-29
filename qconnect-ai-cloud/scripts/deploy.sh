#!/usr/bin/env bash
# =====================================================================
# QConnect-AI Cloud :: build + push + deploy
# ---------------------------------------------------------------------
# Builds and pushes the qc-evaluation and ml-inference images, then
# applies the kustomize overlay with the image tag overridden.
#
# Configuration (env or flags; flags win):
#   IMAGE_REGISTRY   registry prefix      (default registry.example.com/qconnect-ai)
#   IMAGE_TAG        image tag            (default git short SHA, else "latest")
#   K8S_CONTEXT      kubectl context      (default: current context)
#   NAMESPACE        target namespace     (default qconnect-ai)
#
# Flags:
#   --registry R  --tag T  --context C  --namespace N
#   --dry-run     print/build but do not push or apply
#   -h | --help
#
# Usage:
#   IMAGE_REGISTRY=registry.example.com/qconnect-ai ./scripts/deploy.sh
#   ./scripts/deploy.sh --tag v1.2.3 --context prod --dry-run
# =====================================================================
set -euo pipefail

# --- resolve repo root (this script lives in <root>/scripts) ---------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${REPO_ROOT}/deploy/k8s"

# --- defaults --------------------------------------------------------
IMAGE_REGISTRY="${IMAGE_REGISTRY:-registry.example.com/qconnect-ai}"
IMAGE_TAG="${IMAGE_TAG:-}"
K8S_CONTEXT="${K8S_CONTEXT:-}"
NAMESPACE="${NAMESPACE:-qconnect-ai}"
DRY_RUN="false"

# Service name -> docker build context (relative to repo root).
SERVICES=("qc-evaluation" "ml-inference")
declare -A SERVICE_CONTEXT=(
  ["qc-evaluation"]="cloud/services/qc_evaluation"
  ["ml-inference"]="cloud/services/ml_inference"
)

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --- arg parsing -----------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)  IMAGE_REGISTRY="${2:?--registry needs a value}"; shift 2 ;;
    --tag)       IMAGE_TAG="${2:?--tag needs a value}"; shift 2 ;;
    --context)   K8S_CONTEXT="${2:?--context needs a value}"; shift 2 ;;
    --namespace) NAMESPACE="${2:?--namespace needs a value}"; shift 2 ;;
    --dry-run)   DRY_RUN="true"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown argument: $1 (try --help)" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found in PATH. $2"
}

# --- tool checks -----------------------------------------------------
require_cmd docker  "Install Docker to build/push images."
require_cmd kubectl "Install kubectl (>=1.28) to apply manifests."

# kustomize may be standalone or bundled in kubectl; detect either.
KUSTOMIZE_MODE=""
if command -v kustomize >/dev/null 2>&1; then
  KUSTOMIZE_MODE="standalone"
elif kubectl kustomize --help >/dev/null 2>&1; then
  KUSTOMIZE_MODE="kubectl"
else
  die "neither 'kustomize' nor 'kubectl kustomize' is available."
fi

# --- derive tag if not supplied -------------------------------------
if [[ -z "${IMAGE_TAG}" ]]; then
  if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --short HEAD >/dev/null 2>&1; then
    IMAGE_TAG="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
  else
    IMAGE_TAG="latest"
    warn "no git SHA available; falling back to IMAGE_TAG=latest"
  fi
fi

log "registry : ${IMAGE_REGISTRY}"
log "tag      : ${IMAGE_TAG}"
log "namespace: ${NAMESPACE}"
log "context  : ${K8S_CONTEXT:-<current>}"
log "kustomize: ${KUSTOMIZE_MODE}"
log "dry-run  : ${DRY_RUN}"

KUBECTL=(kubectl)
[[ -n "${K8S_CONTEXT}" ]] && KUBECTL+=(--context "${K8S_CONTEXT}")

# --- build + push ----------------------------------------------------
for svc in "${SERVICES[@]}"; do
  ctx="${REPO_ROOT}/${SERVICE_CONTEXT[$svc]}"
  image="${IMAGE_REGISTRY}/${svc}:${IMAGE_TAG}"
  [[ -d "${ctx}" ]] || die "build context not found for ${svc}: ${ctx}"

  log "building ${image}  (context: ${ctx})"
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "  [dry-run] docker build -t ${image} ${ctx}"
  else
    docker build -t "${image}" "${ctx}"
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    log "  [dry-run] docker push ${image}"
  else
    log "pushing ${image}"
    docker push "${image}"
  fi
done

# --- set image overrides in a temp copy so the tree stays clean ------
WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT
cp -R "${K8S_DIR}/." "${WORK_DIR}/"

set_image() {
  local original="$1" new="$2"
  if [[ "${KUSTOMIZE_MODE}" == "standalone" ]]; then
    ( cd "${WORK_DIR}" && kustomize edit set image "${original}=${new}" )
  else
    # kubectl-bundled kustomize has no `edit`; patch kustomization.yaml inline.
    ( cd "${WORK_DIR}" && kustomize_edit_fallback "${original}" "${new}" )
  fi
}

# Minimal newName/newTag rewrite for the kubectl-bundled case.
kustomize_edit_fallback() {
  local original="$1" new="$2" newname newtag
  newname="${new%:*}"
  newtag="${new##*:}"
  python3 - "$original" "$newname" "$newtag" <<'PY'
import sys, re
orig, newname, newtag = sys.argv[1], sys.argv[2], sys.argv[3]
path = "kustomization.yaml"
with open(path) as f:
    text = f.read()
# Replace the newName/newTag lines belonging to the matching `- name:` entry.
pat = re.compile(
    r"(- name:\s*" + re.escape(orig) + r"\n)(\s*)newName:.*\n(\s*)newTag:.*\n"
)
def repl(m):
    return f"{m.group(1)}{m.group(2)}newName: {newname}\n{m.group(3)}newTag: {newtag}\n"
new_text, n = pat.subn(repl, text)
if n == 0:
    sys.exit(f"could not patch image override for {orig}")
with open(path, "w") as f:
    f.write(new_text)
PY
}

for svc in "${SERVICES[@]}"; do
  set_image "registry.example.com/qconnect-ai/${svc}" "${IMAGE_REGISTRY}/${svc}:${IMAGE_TAG}"
done

# --- apply -----------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
  log "[dry-run] rendered manifests:"
  if [[ "${KUSTOMIZE_MODE}" == "standalone" ]]; then
    ( cd "${WORK_DIR}" && kustomize build . )
  else
    "${KUBECTL[@]}" kustomize "${WORK_DIR}"
  fi
  log "[dry-run] would run: ${KUBECTL[*]} apply -k ${WORK_DIR}  (namespace ${NAMESPACE})"
  log "dry-run complete; nothing pushed or applied."
  exit 0
fi

log "applying manifests to namespace '${NAMESPACE}'"
"${KUBECTL[@]}" apply -k "${WORK_DIR}"

log "waiting for rollouts to complete"
for svc in "${SERVICES[@]}"; do
  "${KUBECTL[@]}" -n "${NAMESPACE}" rollout status "deployment/${svc}" --timeout=180s
done

log "deploy complete: ${IMAGE_REGISTRY}/*:${IMAGE_TAG}"
