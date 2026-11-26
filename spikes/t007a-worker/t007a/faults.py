"""Test-only fault injection.

A ``FaultPlan`` can only be passed as a Python object to ``create_api(...)``.
There is no HTTP endpoint, env var, header or query flag that enables it, so
the uvicorn entrypoints always run with ``NO_FAULTS``.
"""

from threading import Lock

POINTS = frozenset(
    {
        "api.between_job_and_outbox",  # job inserted, outbox not yet -> rollback
        "api.before_commit",  # job+outbox inserted, not committed -> rollback
        "api.wake",  # committed; the wake call fails
        "api.response_lost",  # committed (+wake attempted); client never gets a response
        "worker.response_lost_after_save",  # result saved + committed; wake HTTP response lost
        "import.between_items_and_checkpoint",  # page items inserted, checkpoint not yet -> rollback
        "import.before_page_commit",  # page items + checkpoint updated, before commit -> rollback
        "import.after_page_commit",  # page items + checkpoint committed; ack/caller lost
    }
)


class InjectedFault(RuntimeError):
    pass


class FaultPlan:
    """``FaultPlan({"api.wake": 1})`` fails the named point that many times."""

    def __init__(self, counts: dict[str, int] | None = None):
        counts = dict(counts or {})
        unknown = set(counts) - POINTS
        if unknown:
            raise ValueError(f"unknown fault points: {sorted(unknown)}")
        self._remaining = counts
        self._lock = Lock()
        self.fired: list[str] = []

    def should_fail(self, point: str) -> bool:
        assert point in POINTS, point
        with self._lock:
            if self._remaining.get(point, 0) > 0:
                self._remaining[point] -= 1
                self.fired.append(point)
                return True
            return False

    def maybe_raise(self, point: str) -> None:
        if self.should_fail(point):
            raise InjectedFault(point)


NO_FAULTS = FaultPlan()
