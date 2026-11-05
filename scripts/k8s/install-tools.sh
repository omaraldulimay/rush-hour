#!/usr/bin/env bash
# Install pinned CLI tools (linux/amd64) into $HOME/.local/bin, each verified against a SHA-256 that
# is committed here. Usage: install-tools.sh helm kind kubeconform k6
set -euo pipefail

BIN=${BIN_DIR:-$HOME/.local/bin}
mkdir -p "$BIN"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fetch() { # url sha256 file
  curl -fsSL --retry 3 -o "$tmp/$3" "$1"
  echo "$2  $tmp/$3" | sha256sum -c -
}

for tool in "$@"; do
  case "$tool" in
    helm) # v3.22.1
      fetch https://get.helm.sh/helm-v3.22.1-linux-amd64.tar.gz \
        896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea helm.tgz
      tar -xzf "$tmp/helm.tgz" -C "$tmp" linux-amd64/helm
      install "$tmp/linux-amd64/helm" "$BIN/helm" ;;
    kind) # v0.33.0
      fetch https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64 \
        aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d kind
      install "$tmp/kind" "$BIN/kind" ;;
    kubeconform) # v0.8.0
      fetch https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz \
        9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 kc.tgz
      tar -xzf "$tmp/kc.tgz" -C "$tmp" kubeconform
      install "$tmp/kubeconform" "$BIN/kubeconform" ;;
    k6) # v2.3.0
      fetch https://github.com/grafana/k6/releases/download/v2.3.0/k6-v2.3.0-linux-amd64.tar.gz \
        39c3117b6af817592dcd0ce4242105c0a7af10948c2a425306f0be8f7a8a8ab1 k6.tgz
      tar -xzf "$tmp/k6.tgz" -C "$tmp"
      install "$tmp"/k6-v2.3.0-linux-amd64/k6 "$BIN/k6" ;;
    promtool) # Prometheus v3.15.1
      fetch https://github.com/prometheus/prometheus/releases/download/v3.15.1/prometheus-3.15.1.linux-amd64.tar.gz \
        6a726885328ad9efebbf36e876ce2fb6225468a71fd1e7ce3ba666d5f4d97a84 prom.tgz
      tar -xzf "$tmp/prom.tgz" -C "$tmp" prometheus-3.15.1.linux-amd64/promtool
      install "$tmp/prometheus-3.15.1.linux-amd64/promtool" "$BIN/promtool" ;;
    *) echo "unknown tool $tool" >&2; exit 1 ;;
  esac
done
echo "$BIN" >> "${GITHUB_PATH:-/dev/null}"
