"""Bounded external wake + recording its outcome on the outbox row.

The wake is a *hint* sent after the DB commit. It is never inside a DB
transaction, it is attempted once with a hard timeout, and its failure never
fails the request: the outbox row stays PENDING and recovery picks it up.
"""

import logging
from typing import Callable
from uuid import UUID

import httpx

from .db import connect

log = logging.getLogger("t007a.wake")

Wake = Callable[[UUID], None]


class WakeError(Exception):
    """The wake hint could not be confirmed. Safe to ignore: outbox is durable."""


class HttpWake:
    """POST {job_id} to the worker. One attempt, hard timeout, no redirects."""

    def __init__(self, url: str, timeout_s: float):
        self.url = url
        self.timeout = httpx.Timeout(timeout_s)

    def __call__(self, job_id: UUID) -> None:
        try:
            r = httpx.post(self.url, json={"job_id": str(job_id)}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise WakeError(type(exc).__name__) from exc
        if r.status_code >= 300:
            raise WakeError(f"http_{r.status_code}")


def dispatch(dsn: str, app_name: str, wake: Wake | None, job_id: UUID) -> str:
    """Call ``wake`` outside any transaction and record the outcome.

    Returns ``acknowledged`` / ``failed`` / ``no_wake_configured``.
    """
    if wake is None:
        outcome, error = "no_wake_configured", "no_wake_configured"
    else:
        try:
            wake(job_id)
            outcome, error = "acknowledged", None
        except WakeError as exc:
            outcome, error = "failed", f"wake_failed:{exc}"
        except Exception as exc:  # noqa: BLE001 - a wake must never fail the caller
            outcome, error = "failed", f"wake_failed:{type(exc).__name__}"
    try:
        with connect(dsn, app_name) as conn:
            conn.execute(
                """
                UPDATE outbox
                   SET dispatch_attempts = dispatch_attempts + 1,
                       status = CASE WHEN %(ok)s AND status = 'PENDING'
                                          AND (last_error IS NULL OR last_error NOT LIKE 'retryable:%%')
                                     THEN 'DISPATCHED' ELSE status END,
                       dispatched_at = CASE WHEN %(ok)s THEN now() ELSE dispatched_at END,
                       last_error = CASE
                           WHEN %(ok)s AND (last_error IS NULL OR last_error NOT LIKE 'retryable:%%')
                               THEN NULL
                           WHEN NOT %(ok)s THEN %(err)s
                           ELSE last_error
                       END
                 WHERE job_id = %(job)s
                """,
                {"ok": error is None, "err": error, "job": job_id},
            )
    except Exception:  # noqa: BLE001 - bookkeeping only; recovery still works
        log.warning("could not record wake outcome for %s", job_id, exc_info=True)
    return outcome
