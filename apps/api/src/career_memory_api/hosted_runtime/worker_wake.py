"""Post-commit Cloud Run worker wake notifier and Google OIDC ID token verifier (OD-012)."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from career_memory_api.auth.jwt_verifier import (
    JwksProviderPort,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
)
from career_memory_api.auth.redaction import redact_text
from career_memory_api.edge_guard.middleware import HmacWorkerTokenVerifier
from career_memory_api.edge_guard.models import WorkerIdentity
from career_memory_api.hosted_runtime.jwks_client import (
    HttpsJwksProvider,
    _NoRedirectHandler,
    validate_pinned_auth_url,
)
from career_memory_api.jobs.models import WakeSignal

logger = logging.getLogger(__name__)

_GOOGLE_METADATA_IDENTITY_BASE = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)


class GoogleMetadataOidcTokenMinter:
    """Mints a Google Cloud Run OIDC ID token from the GCE/Cloud Run metadata server."""

    def __init__(self, *, timeout_seconds: float = 1.5) -> None:
        self._timeout_seconds = max(0.2, float(timeout_seconds))

    def mint_id_token(self, audience: str) -> str:
        encoded_aud = urllib.parse.quote(audience.strip(), safe="")
        url = f"{_GOOGLE_METADATA_IDENTITY_BASE}?audience={encoded_aud}&format=full"
        opener = urllib.request.build_opener(_NoRedirectHandler())
        req = urllib.request.Request(
            url,
            headers={"Metadata-Flavor": "Google"},
            method="GET",
        )
        with opener.open(req, timeout=self._timeout_seconds) as resp:
            if int(resp.status) != 200:
                raise OSError(f"Metadata server returned HTTP {resp.status}.")
            token = resp.read(16384).decode("utf-8").strip()
            if not token:
                raise OSError("Metadata server returned empty OIDC token.")
            return token


HttpPostFn = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _default_no_redirect_http_post(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            return int(resp.status), resp.read(16384)
    except urllib.error.HTTPError as exc:
        err_body = b""
        try:
            if exc.fp is not None:
                err_body = exc.fp.read(4096)
        except Exception:  # noqa: BLE001
            err_body = b""
        return int(exc.code), err_body


class HttpCloudRunWakeNotifier:
    """Bounded post-commit HTTP wake notifier for ``cm-worker`` (``PostCommitWakeNotifierPort``).

    Invariants (OD-012 / T-007A..F):
    - Called ONLY after PostgreSQL commit (``open_tx == 0``).
    - Uses short bounded timeout (default ``2.0s``) with ZERO retries on timeout.
    - Catches and logs (redacted) all failures; NEVER fails an already-committed API 202.
    """

    def __init__(
        self,
        *,
        worker_url: str,
        audience: str | None = None,
        timeout_seconds: float = 2.0,
        token_minter: Callable[[str], str] | None = None,
        hmac_verifier: HmacWorkerTokenVerifier | None = None,
        hmac_subject: str = "cm-api-sa@local.invalid",
        hmac_issuer: str = "https://accounts.google.com",
        allow_http_loopback_for_tests: bool = False,
        http_post: HttpPostFn | None = None,
        github_public_token_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._worker_url = validate_pinned_auth_url(
            worker_url.rstrip("/"),
            allow_http_loopback_for_tests=allow_http_loopback_for_tests,
        )
        self._wake_endpoint = f"{self._worker_url}/internal/worker/wake"
        self._audience = (audience or self._worker_url).strip()
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._token_minter = token_minter
        self._hmac_verifier = hmac_verifier
        self._hmac_subject = hmac_subject
        self._hmac_issuer = hmac_issuer
        self._http_post: HttpPostFn = http_post or _default_no_redirect_http_post
        self._github_public_token_getter = github_public_token_getter
        self._metadata_minter = GoogleMetadataOidcTokenMinter(
            timeout_seconds=min(1.5, self._timeout_seconds)
        )

        # Redacted counters for observability & tests
        self.attempts: int = 0
        self.successes: int = 0
        self.failures: int = 0
        self.last_error_code: str | None = None

    def _mint_auth_token(self) -> str:
        if self._token_minter is not None:
            return self._token_minter(self._audience)
        if self._hmac_verifier is not None:
            return self._hmac_verifier.mint_token(
                subject=self._hmac_subject,
                issuer=self._hmac_issuer,
                audience=self._audience,
                expires_at=time.time() + 120.0,
            )
        return self._metadata_minter.mint_id_token(self._audience)

    def _send_wake_payload(self, payload: dict[str, Any]) -> bool:
        self.attempts += 1
        try:
            token = self._mint_auth_token()
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {token}",
                "X-CM-Worker-Auth": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self._github_public_token_getter is not None:
                gh_tok = (self._github_public_token_getter() or "").strip()
                if gh_tok and not any(ch.isspace() for ch in gh_tok):
                    headers["X-CM-GitHub-Public-Token"] = gh_tok
            status, _ = self._http_post(
                self._wake_endpoint,
                headers,
                body,
                self._timeout_seconds,
            )
            if 200 <= status < 300:
                self.successes += 1
                self.last_error_code = None
                return True
            self.failures += 1
            self.last_error_code = f"HTTP_{status}"
            logger.warning(
                "Post-commit worker wake returned non-2xx status=%s workspace_id=%s",
                status,
                payload.get("workspace_id"),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.last_error_code = type(exc).__name__
            logger.warning(
                "Post-commit worker wake failed (non-fatal; job remains durable in PG): %s",
                redact_text(str(exc)),
            )
            return False

    def notify_job_available(self, signal: WakeSignal) -> None:
        self._send_wake_payload(
            {
                "workspace_id": signal.workspace_id,
                "job_id": signal.job_id,
                "job_type": signal.job_type,
                "target_sync_run_id": signal.target_sync_run_id,
                "outbox_event_id": signal.outbox_event_id,
            }
        )

    def notify_deletion_job(
        self,
        *,
        workspace_id: str,
        deletion_job_id: str | None = None,
    ) -> bool:
        return self._send_wake_payload(
            {
                "workspace_id": workspace_id,
                "job_type": "SOURCE_DELETION",
                "deletion_job_id": deletion_job_id,
            }
        )


def _peek_unverified_jwt_iss_aud(raw_token: str) -> tuple[str | None, tuple[str, ...]]:
    """Inspect unverified iss/aud solely to select a pre-pinned cryptographic verifier."""
    parts = raw_token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        return None, ()
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((parts[1] + padding).encode("ascii")).decode("utf-8")
        )
    except Exception:  # noqa: BLE001
        return None, ()
    if not isinstance(payload, dict):
        return None, ()
    iss = payload.get("iss") if isinstance(payload.get("iss"), str) else None
    raw_aud = payload.get("aud")
    if isinstance(raw_aud, str):
        auds = (raw_aud,)
    elif isinstance(raw_aud, list) and all(isinstance(a, str) for a in raw_aud):
        auds = tuple(raw_aud)
    else:
        auds = ()
    return iss, auds


class GoogleOidcWorkerTokenVerifier:
    """Fail-closed Google OIDC RS256/ES256 ID token verifier for ``EdgeGuardConfig.worker_auth``."""

    def __init__(
        self,
        *,
        allowed_worker_audiences: frozenset[str],
        allowed_worker_issuers: frozenset[str] = frozenset(
            {"https://accounts.google.com", "accounts.google.com"}
        ),
        allowed_worker_service_accounts: frozenset[str] = frozenset(),
        jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs",
        jwks_provider: JwksProviderPort | None = None,
        allow_http_loopback_for_tests: bool = False,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._allowed_audiences = frozenset(
            a.strip() for a in allowed_worker_audiences if a.strip()
        )
        self._allowed_issuers = frozenset(i.strip() for i in allowed_worker_issuers if i.strip())
        self._allowed_service_accounts = frozenset(
            sa.strip().lower() for sa in allowed_worker_service_accounts if sa.strip()
        )
        self._jwks_provider: JwksProviderPort = jwks_provider or HttpsJwksProvider(
            jwks_url=jwks_url,
            allow_http_loopback_for_tests=allow_http_loopback_for_tests,
            clock=clock,
        )
        self._verifiers: dict[tuple[str, str], JwtAccessTokenVerifier] = {
            (iss, aud): JwtAccessTokenVerifier(
                config=JwtVerificationConfig(
                    expected_issuer=iss,
                    expected_audience=aud,
                ),
                jwks_provider=self._jwks_provider,
                clock=clock,
            )
            for iss in self._allowed_issuers
            for aud in self._allowed_audiences
        }

    def __call__(self, token: str) -> WorkerIdentity | None:
        iss, auds = _peek_unverified_jwt_iss_aud(token)
        if iss is None or iss not in self._allowed_issuers:
            return None
        matched_aud = next((a for a in auds if a in self._allowed_audiences), None)
        if matched_aud is None:
            return None

        verifier = self._verifiers.get((iss, matched_aud))
        if verifier is None:
            return None

        try:
            claims = verifier.verify(token)
        except Exception:  # noqa: BLE001
            return None

        # Prefer verified service account email when present; fall back to subject.
        email_verified = claims.raw_claims_redacted.get("email_verified")
        candidate_sa = (
            claims.email.strip().lower()
            if claims.email and (email_verified is True or email_verified is None)
            else claims.subject.strip().lower()
        )
        if self._allowed_service_accounts:
            sub_lower = claims.subject.strip().lower()
            email_lower = claims.email.strip().lower() if claims.email else ""
            if (
                candidate_sa not in self._allowed_service_accounts
                and sub_lower not in self._allowed_service_accounts
                and email_lower not in self._allowed_service_accounts
            ):
                return None

        return WorkerIdentity(
            subject=candidate_sa,
            issuer=claims.issuer,
            audience=matched_aud,
            expires_at=claims.expires_at.timestamp(),
        )


__all__ = [
    "GoogleMetadataOidcTokenMinter",
    "GoogleOidcWorkerTokenVerifier",
    "HttpCloudRunWakeNotifier",
    "HttpPostFn",
]
