#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG_PATH="${KUBECONFIG_PATH:-/home/borg/.kube/k3s-remote}"
NAMESPACE="${NAMESPACE:-global-identity}"

KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n "${NAMESPACE}" get pods,svc,ingress
KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n "${NAMESPACE}" get hpa
KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n "${NAMESPACE}" get networkpolicy
