import time
from collections.abc import Callable

from prometheus_client import Gauge

STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}
BREAKER_STATE = Gauge("circuit_breaker_state", "0=closed, 1=half_open, 2=open", ["name"])


class CircuitBreaker:
    """Opens after `threshold` consecutive failures inside `window_s`.

    While open every call is rejected. After `cooldown_s` one trial call is let through
    (half-open): success closes the breaker, failure re-opens it.
    """

    def __init__(
        self,
        name: str,
        *,
        threshold: int,
        window_s: float,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name, self.threshold = name, threshold
        self.window_s, self.cooldown_s, self.clock = window_s, cooldown_s, clock
        self._state = "closed"
        self._failures = 0
        self._first_failure_at = 0.0
        self._opened_at = 0.0
        self._trial_started_at: float | None = None
        self._publish()

    @property
    def state(self) -> str:
        return self._state

    def _set(self, state: str) -> None:
        self._state = state
        self._publish()

    def _publish(self) -> None:
        BREAKER_STATE.labels(self.name).set(STATE_VALUES[self._state])

    def allow(self) -> bool:
        now = self.clock()
        if self._state == "closed":
            return True
        if self._state == "open":
            if now - self._opened_at < self.cooldown_s:
                return False
            self._set("half_open")
            self._trial_started_at = now
            return True
        # half_open: a single trial at a time; a lost trial is replaced after another cooldown.
        if self._trial_started_at is not None and now - self._trial_started_at < self.cooldown_s:
            return False
        self._trial_started_at = now
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._trial_started_at = None
        self._set("closed")

    def record_failure(self) -> None:
        now = self.clock()
        if self._state == "half_open":
            self._open(now)
            return
        if self._failures == 0 or now - self._first_failure_at > self.window_s:
            self._failures, self._first_failure_at = 0, now
        self._failures += 1
        if self._failures >= self.threshold:
            self._open(now)

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._failures = 0
        self._trial_started_at = None
        self._set("open")
