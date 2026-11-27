"""Fixed in-memory synthetic external-provider fixtures for T-007C.

NEVER performs network, GitHub, or LLM I/O. Models the three failure classes
required by T-007C:
1. Known transient upstream failure (safe to retry while attempts < max_attempts).
2. Known permanent upstream rejection (fail immediately, do not retry).
3. Ambiguous upstream timeout where side-effect completion is unknown (quarantine
   into REVIEW_REQUIRED; NEVER auto-re-invoke).
"""

from collections import deque


class TransientExternalError(RuntimeError):
    """Upstream did not apply the side effect (e.g. 503/connect refused before write)."""


class PermanentExternalError(RuntimeError):
    """Upstream deterministically rejected input (e.g. 422 schema/validation error)."""


class AmbiguousExternalTimeoutError(TimeoutError):
    """Timed out waiting for upstream response; side effect may or may not have happened."""


class FixtureExternalProcessor:
    """Deterministic scripted processor backed strictly by local canned fixtures."""

    def __init__(self, script: list[dict | BaseException]):
        self._steps = deque(script)
        self.calls: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, payload: dict) -> dict:
        self.calls.append(dict(payload))
        if not self._steps:
            return {
                "processor": "t007c-fixture-v1",
                "fixture": "default_ok",
                "call_no": len(self.calls),
                "echo_chars": len(payload["text"]),
            }
        step = self._steps.popleft()
        if isinstance(step, BaseException):
            raise step
        return {
            "processor": "t007c-fixture-v1",
            **step,
            "call_no": len(self.calls),
        }
