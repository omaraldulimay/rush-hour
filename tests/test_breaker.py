from prometheus_client import REGISTRY

from services.checkout.app.breaker import CircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(clock: Clock, name: str = "t") -> CircuitBreaker:
    return CircuitBreaker(name, threshold=3, window_s=10, cooldown_s=5, clock=clock)


def gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value("circuit_breaker_state", {"name": name})


def test_opens_after_consecutive_failures() -> None:
    clock, breaker = Clock(), None
    breaker = make(clock, "opens")
    for _ in range(2):
        breaker.record_failure()
    assert breaker.allow() and gauge("opens") == 0
    breaker.record_failure()
    assert not breaker.allow()
    assert gauge("opens") == 2


def test_success_resets_the_count() -> None:
    breaker = make(Clock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allow() and breaker.state == "closed"


def test_failures_outside_the_window_do_not_add_up() -> None:
    clock = Clock()
    breaker = make(clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 11
    breaker.record_failure()
    assert breaker.state == "closed"


def test_half_open_trial_success_closes() -> None:
    clock = Clock()
    breaker = make(clock, "half")
    for _ in range(3):
        breaker.record_failure()
    clock.now = 4.9
    assert not breaker.allow()
    clock.now = 5.1
    assert breaker.allow() and gauge("half") == 1
    assert not breaker.allow()  # only one trial at a time
    breaker.record_success()
    assert breaker.state == "closed" and breaker.allow()


def test_half_open_trial_failure_reopens() -> None:
    clock = Clock()
    breaker = make(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.now = 6
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.now = 11.5
    assert breaker.allow()
