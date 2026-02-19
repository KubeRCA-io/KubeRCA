#!/usr/bin/env bash
# KubeRCA E2E cleanup — removes all test resources.
set -euo pipefail

NS="kuberca-e2e"

echo "=== KubeRCA E2E Cleanup ==="

# Delete test workloads
echo "[1/3] Deleting test workloads..."
kubectl delete pods -n "$NS" -l e2e-test=true --ignore-not-found --wait=false

# Uninstall Helm release
echo "[2/3] Uninstalling Helm release..."
helm uninstall kuberca -n "$NS" --wait 2>/dev/null || echo "  (no Helm release found)"

# Delete namespace
echo "[3/3] Deleting namespace..."
kubectl delete namespace "$NS" --ignore-not-found --wait=false

echo "=== Cleanup complete ==="
