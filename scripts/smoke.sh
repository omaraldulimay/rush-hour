#!/usr/bin/env bash
# End-to-end check against a running compose stack (make up): browse, cart, checkout, stock, metrics.
set -euo pipefail

declare -A PORTS=([catalog]=8001 [inventory]=8002 [checkout]=8003 [payment-mock]=8004 [order-worker]=8005)
HOST=${HOST:-127.0.0.1}
fail() { echo "SMOKE FAILED: $*" >&2; exit 1; }

echo "== waiting for /readyz"
for name in "${!PORTS[@]}"; do
  url="http://$HOST:${PORTS[$name]}/readyz"
  for i in $(seq 1 60); do
    curl -fsS -o /dev/null "$url" 2>/dev/null && break
    [ "$i" = 60 ] && fail "$name not ready at $url"
    sleep 2
  done
  echo "ready: $name"
done

CATALOG=http://$HOST:8001 INVENTORY=http://$HOST:8002 CHECKOUT=http://$HOST:8003

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
for name in "${!PORTS[@]}"; do
  curl -fsS "http://$HOST:${PORTS[$name]}/metrics" | grep -q '^# HELP' || fail "no metrics from $name"
  echo "metrics ok: $name"
done
echo "SMOKE OK"
