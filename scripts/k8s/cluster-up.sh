#!/usr/bin/env bash
# kind cluster for CI: images loaded, metrics-server, kube-prometheus-stack, optionally Argo Rollouts.
# Needs helm, kind and kubectl on the PATH (scripts/k8s/install-tools.sh). Used by the k8s job in
# ci.yml and by gameday.yml.
#   IMAGES_DIR   folder with image-<service>/image.tar from the build job (required)
#   ARGO_ROLLOUTS=1   also install Argo Rollouts (controller + CRDs)
set -euo pipefail

KIND_NODE_IMAGE=kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
METRICS_SERVER_URL=https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.1/components.yaml
METRICS_SERVER_SHA256=3c7f57cdbee0f57fb0259a150e8f6d6cb4f29d72d1aa5dce8803151dd6d4c2a1
KPS_VERSION=92.9.0
KPS_SHA256=d99bc9dca70d8a2844e59ca18e589bfd69014b5c46d0be05ba8587932048b07f
ARGO_ROLLOUTS_VERSION=v1.10.2
ARGO_ROLLOUTS_SHA256=b8280994791eaee7eb91c8b84450efcc6e0292fca5aafc18c1b0ebc5d18dbefe

cd "$(dirname "$0")/../.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "::group::kind cluster (1 control plane + 2 workers)"
kind create cluster --name rush-hour --config deploy/kind/cluster.yaml --image "$KIND_NODE_IMAGE" --wait 180s
for svc in catalog inventory checkout payment_mock order_worker control_room; do
  kind load image-archive "${IMAGES_DIR:?}/image-${svc}/image.tar" --name rush-hour
done
echo "::endgroup::"

echo "::group::metrics-server (kubelet TLS check off: kind uses self-signed certs)"
curl -fsSL --retry 3 -o "$tmp/metrics-server.yaml" "$METRICS_SERVER_URL"
echo "$METRICS_SERVER_SHA256  $tmp/metrics-server.yaml" | sha256sum -c -
kubectl apply -f "$tmp/metrics-server.yaml"
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
echo "::endgroup::"

echo "::group::kube-prometheus-stack ${KPS_VERSION} (light values)"
curl -fsSL --retry 3 -o "$tmp/kps.tgz" "https://github.com/prometheus-community/helm-charts/releases/download/kube-prometheus-stack-${KPS_VERSION}/kube-prometheus-stack-${KPS_VERSION}.tgz"
echo "$KPS_SHA256  $tmp/kps.tgz" | sha256sum -c -
helm upgrade --install prom "$tmp/kps.tgz" --namespace monitoring --create-namespace \
  -f deploy/observability/values-ci.yaml --wait --timeout 8m
echo "::endgroup::"

if [ "${ARGO_ROLLOUTS:-}" = 1 ]; then
  echo "::group::Argo Rollouts ${ARGO_ROLLOUTS_VERSION}"
  curl -fsSL --retry 3 -o "$tmp/argo.yaml" "https://github.com/argoproj/argo-rollouts/releases/download/${ARGO_ROLLOUTS_VERSION}/install.yaml"
  echo "$ARGO_ROLLOUTS_SHA256  $tmp/argo.yaml" | sha256sum -c -
  kubectl create namespace argo-rollouts
  kubectl apply -n argo-rollouts --server-side -f "$tmp/argo.yaml" # CRDs are too big for client-side apply
  kubectl -n argo-rollouts rollout status deployment/argo-rollouts --timeout=180s
  echo "::endgroup::"
fi
