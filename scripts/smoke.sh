#!/usr/bin/env bash
# End-to-end check against a running stack: browse, cart, checkout, stock, metrics.
# Defaults to the docker compose ports on 127.0.0.1 (make up). Override with HOST and/or the full
# base URLs CATALOG_URL, INVENTORY_URL, CHECKOUT_URL, PAYMENT_MOCK_URL, ORDER_WORKER_URL.
set -euo pipefail

HOST=${HOST:-127.0.0.1}
declare -A BASES=(
  [catalog]=${CATALOG_URL:-http://$HOST:8001}
  [inventory]=${INVENTORY_URL:-http://$HOST:8002}
  [checkout]=${CHECKOUT_URL:-http://$HOST:8003}
  [payment-mock]=${PAYMENT_MOCK_URL:-http://$HOST:8004}
  [order-worker]=${ORDER_WORKER_URL:-http://$HOST:8005}
)
fail() { echo "SMOKE FAILED: $*" >&2; exit 1; }

echo "== waiting for /readyz"
for name in "${!BASES[@]}"; do
  url="${BASES[$name]}/readyz"
  for i in $(seq 1 60); do
    curl -fsS -o /dev/null "$url" 2>/dev/null && break
    [ "$i" = 60 ] && fail "$name not ready at $url"
    sleep 2
  done
  echo "ready: $name"
done

CATALOG=${BASES[catalog]} INVENTORY=${BASES[inventory]} CHECKOUT=${BASES[checkout]}

echo "== list products"
products=$(curl -fsS "$CATALOG/products?category=sneakers&limit=100")
echo "$products" | jq -e '.items | length > 0' >/dev/null || fail "no products"
sku=$(echo "$products" | jq -r '[.items[] | select(.limited_drop)][0].sku')
[ -n "$sku" ] && [ "$sku" != null ] || fail "no hot item found"
echo "hot item: $sku"

before=$(curl -fsS "$INVENTORY/stock/$sku" | jq -r '.available')
echo "stock before: $before"
qty=2

echo "== cart + checkout"
cart="smoke-$(date +%s)"
curl -fsS -X PUT "$CHECKOUT/carts/$cart/items" -H 'content-type: application/json' \
  -d "{\"sku\":\"$sku\",\"qty\":$qty}" | jq -e ".items[0].qty == $qty" >/dev/null || fail "cart"
order=$(curl -fsS -X POST "$CHECKOUT/checkout" -H 'content-type: application/json' \
  -d "{\"cart_id\":\"$cart\"}")
echo "$order"
echo "$order" | jq -e '.status == "paid"' >/dev/null || fail "order not paid"
order_id=$(echo "$order" | jq -r '.order_id')

echo "== fetch order"
curl -fsS "$CHECKOUT/orders/$order_id" | jq -e '.status == "paid"' >/dev/null || fail "fetched order not paid"

echo "== stock decreased"
after=$(curl -fsS "$INVENTORY/stock/$sku" | jq -r '.available')
echo "stock after: $after"
[ "$after" -eq $((before - qty)) ] || fail "expected stock $((before - qty)), got $after"

echo "== metrics"
for name in "${!BASES[@]}"; do
  # capture first: `curl | grep -q` under pipefail fails with SIGPIPE when grep exits early
  body=$(curl -fsS "${BASES[$name]}/metrics") || fail "no metrics from $name"
  grep -q '^# HELP' <<<"$body" || fail "no metrics from $name"
  echo "metrics ok: $name"
done
echo "SMOKE OK"
