// Small k6 load burst against catalog (browse) and checkout (cart + order) for the Kubernetes CI job.
// Env: CATALOG_URL, CHECKOUT_URL, PHASE ("ramp" or "steady"), SUMMARY (output json path).
import http from "k6/http";
import { Rate, Trend } from "k6/metrics";

const CATALOG = __ENV.CATALOG_URL || "http://127.0.0.1:8001";
const CHECKOUT = __ENV.CHECKOUT_URL || "http://127.0.0.1:8003";
const PHASE = __ENV.PHASE || "ramp";

const catalogFail = new Rate("catalog_failed");
const checkoutFail = new Rate("checkout_failed");
const catalogMs = new Trend("catalog_ms", true);
const checkoutMs = new Trend("checkout_ms", true);

// ramp: quiet start, then a Black Friday jump. steady: constant load while a pod is killed.
const catalogStages =
  PHASE === "ramp"
    ? [
        { target: 15, duration: "10s" },
        { target: 220, duration: "20s" },
        { target: 220, duration: "70s" },
      ]
    : [{ target: 220, duration: "50s" }];
const checkoutStages =
  PHASE === "ramp"
    ? [
        { target: 2, duration: "10s" },
        { target: 12, duration: "20s" },
        { target: 12, duration: "70s" },
      ]
    : [{ target: 12, duration: "50s" }];

export const options = {
  discardResponseBodies: false,
  scenarios: {
    catalog: {
      executor: "ramping-arrival-rate",
      exec: "browse",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 60,
      maxVUs: 300,
      stages: catalogStages,
    },
    checkout: {
      executor: "ramping-arrival-rate",
      exec: "buy",
      startRate: 1,
      timeUnit: "1s",
      preAllocatedVUs: 30,
      maxVUs: 150,
      stages: checkoutStages,
    },
  },
};

const ok = http.expectedStatuses({ min: 200, max: 299 });
const headers = { "content-type": "application/json" };

export function browse() {
  const category = ["sneakers", "jackets", "knitwear", "accessories"][Math.floor(Math.random() * 4)];
  const res = http.get(`${CATALOG}/products?category=${category}&limit=20&offset=${Math.floor(Math.random() * 400)}`, {
    responseCallback: ok,
    timeout: "10s",
  });
  catalogFail.add(res.status < 200 || res.status > 299);
  catalogMs.add(res.timings.duration);
}

export function buy() {
  // Accessories have 50-600 pieces each and are not limited drops, so stock never decides the result.
  const sku = `ACC-${String(1 + Math.floor(Math.random() * 500)).padStart(4, "0")}`;
  const cart = `k6-${__VU}-${__ITER}-${Date.now()}`;
  const put = http.put(`${CHECKOUT}/carts/${cart}/items`, JSON.stringify({ sku, qty: 1 }), {
    headers,
    responseCallback: ok,
    timeout: "10s",
  });
  const res =
    put.status === 200
      ? http.post(`${CHECKOUT}/checkout`, JSON.stringify({ cart_id: cart }), {
          headers,
          responseCallback: ok,
          timeout: "10s",
        })
      : put;
  checkoutFail.add(res.status < 200 || res.status > 299);
  checkoutMs.add(res.timings.duration);
}

export function handleSummary(data) {
  const m = (name) => (data.metrics[name] && (data.metrics[name].values || data.metrics[name])) || {};
  const out = {
    phase: PHASE,
    requests: m("http_reqs").count || 0,
    rps: m("http_reqs").rate || 0,
    catalog_error_rate: m("catalog_failed").rate || 0,
    checkout_error_rate: m("checkout_failed").rate || 0,
    catalog_p95_ms: m("catalog_ms")["p(95)"] || 0,
    checkout_p95_ms: m("checkout_ms")["p(95)"] || 0,
    dropped_iterations: m("dropped_iterations").count || 0,
  };
  return { [__ENV.SUMMARY || "k6-summary.json"]: JSON.stringify(out, null, 2) };
}
