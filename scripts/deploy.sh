#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG_PATH="${KUBECONFIG_PATH:-/home/borg/.kube/k3s-remote}"
THUNDERBALL_SSH="${THUNDERBALL_SSH:-borg@thunderball}"
THUNDERBALL_PROJECT_DIR="${THUNDERBALL_PROJECT_DIR:-/home/borg/identity}"
IMAGE="${IMAGE:-ghcr.io/xgalvanize/global-identity:latest}"
NAMESPACE="global-identity"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required" >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

MONGO_PASSWORD="$(openssl rand -base64 24 | tr -d '\n')"
JWT_SECRET="$(openssl rand -base64 48 | tr -d '\n')"

cat >"${ROOT_DIR}/k8s/secrets.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-secret
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  username: root
  password: ${MONGO_PASSWORD}
---
apiVersion: v1
kind: Secret
metadata:
  name: identity-secret
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  jwt-secret: ${JWT_SECRET}
  mongo-uri: mongodb://root:${MONGO_PASSWORD}@mongodb.${NAMESPACE}.svc.cluster.local:27017
EOF

echo "Syncing project to thunderball..."
rsync -av --delete "${ROOT_DIR}/" "${THUNDERBALL_SSH}:${THUNDERBALL_PROJECT_DIR}/"

echo "Building image on thunderball into k3s containerd cache..."
if ssh "${THUNDERBALL_SSH}" "command -v nerdctl >/dev/null 2>&1"; then
  if ssh "${THUNDERBALL_SSH}" "command -v sudo >/dev/null 2>&1"; then
    ssh -t "${THUNDERBALL_SSH}" "cd ${THUNDERBALL_PROJECT_DIR} && sudo nerdctl --namespace k8s.io build -t ${IMAGE} ." || \
      ssh "${THUNDERBALL_SSH}" "cd ${THUNDERBALL_PROJECT_DIR} && nerdctl --namespace k8s.io build -t ${IMAGE} ."
  else
    ssh "${THUNDERBALL_SSH}" "cd ${THUNDERBALL_PROJECT_DIR} && nerdctl --namespace k8s.io build -t ${IMAGE} ."
  fi
elif ssh "${THUNDERBALL_SSH}" "command -v docker >/dev/null 2>&1 && command -v k3s >/dev/null 2>&1"; then
  echo "nerdctl not found. Using docker build + k3s image import fallback..."
  ssh -t "${THUNDERBALL_SSH}" "cd ${THUNDERBALL_PROJECT_DIR} && docker build -t ${IMAGE} . && docker save ${IMAGE} | sudo k3s ctr images import -"
elif ssh "${THUNDERBALL_SSH}" "command -v podman >/dev/null 2>&1 && command -v k3s >/dev/null 2>&1"; then
  echo "nerdctl not found. Using podman build + k3s image import fallback..."
  ssh -t "${THUNDERBALL_SSH}" "cd ${THUNDERBALL_PROJECT_DIR} && podman build -t ${IMAGE} . && podman save --format docker-archive ${IMAGE} -o /tmp/global-identity-image.tar && sudo k3s ctr images import /tmp/global-identity-image.tar && rm -f /tmp/global-identity-image.tar"
elif ssh "${THUNDERBALL_SSH}" "command -v k3s >/dev/null 2>&1" && command -v docker >/dev/null 2>&1; then
  echo "No remote builder found. Building locally with docker and streaming to thunderball k3s..."
  LOCAL_IMAGE_TAR="/tmp/global-identity-image.tar"
  REMOTE_IMAGE_TAR="/tmp/global-identity-image.tar"
  docker build -t "${IMAGE}" "${ROOT_DIR}"
  docker save -o "${LOCAL_IMAGE_TAR}" "${IMAGE}"
  scp "${LOCAL_IMAGE_TAR}" "${THUNDERBALL_SSH}:${REMOTE_IMAGE_TAR}"
  ssh -t "${THUNDERBALL_SSH}" "sudo k3s ctr images import ${REMOTE_IMAGE_TAR} && rm -f ${REMOTE_IMAGE_TAR}"
  rm -f "${LOCAL_IMAGE_TAR}"
elif ssh "${THUNDERBALL_SSH}" "command -v k3s >/dev/null 2>&1" && command -v podman >/dev/null 2>&1; then
  echo "No remote builder found. Building locally with podman and streaming to thunderball k3s..."
  LOCAL_IMAGE_TAR="/tmp/global-identity-image.tar"
  REMOTE_IMAGE_TAR="/tmp/global-identity-image.tar"
  podman build -t "${IMAGE}" "${ROOT_DIR}"
  podman save --format docker-archive -o "${LOCAL_IMAGE_TAR}" "${IMAGE}"
  scp "${LOCAL_IMAGE_TAR}" "${THUNDERBALL_SSH}:${REMOTE_IMAGE_TAR}"
  ssh -t "${THUNDERBALL_SSH}" "sudo k3s ctr images import ${REMOTE_IMAGE_TAR} && rm -f ${REMOTE_IMAGE_TAR}"
  rm -f "${LOCAL_IMAGE_TAR}"
else
  echo "No supported build path found on thunderball." >&2
  echo "Install one of: nerdctl, or docker + k3s, or podman + k3s." >&2
  echo "Or install docker/podman on speedball and rerun for local-build streaming fallback." >&2
  exit 1
fi

echo "Applying manifests with kubeconfig ${KUBECONFIG_PATH}..."
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/namespace.yaml"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/secrets.yaml"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/mongodb.yaml"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/identity-api.yaml"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/ingress.yaml"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${ROOT_DIR}/k8s/network-policy.yaml"

echo "Waiting for rollout..."
KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n "${NAMESPACE}" rollout status deploy/identity-api

echo "Done. Resources:"
KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n "${NAMESPACE}" get pods,svc,ingress
