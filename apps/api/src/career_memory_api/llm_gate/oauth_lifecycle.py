"""In-memory / port-backed Subscription OAuth / PKCE / OIDC / Consent / Refresh / Unlink lifecycle.

Zero PostgreSQL user LLM token tables are introduced per owner-approved OD-014 (OFF).
Zero plaintext tokens ever appear in connection state, job references, logs, or exceptions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from career_memory_api.auth.oauth import derive_pkce_s256_challenge, hash_oauth_state
from career_memory_api.authz import WorkspaceAccessContext

from .models import (
    DEFAULT_LLM_POLICY_VERSION,
    PROHIBITED_PROVIDER_MODES,
    ConnectionJobReference,
    ExternalSendConsentRecord,
    LlmGateReasonCode,
    LlmLifecycleError,
    LlmProviderMode,
    LlmStageCode,
    RemoteRevocationStatus,
    SecretWipeStatus,
    SubscriptionConnectionRecord,
    SubscriptionConnectionStatus,
    redact_llm_text,
    utc_now,
)

logger = logging.getLogger(__name__)

_PKCE_VAULT_NAMESPACE = "llm-sub-pkce"
_TOKEN_VAULT_NAMESPACE = "llm-sub-token"


# =============================================================================
# Injectable Ports & Token Exchange Contracts (Zero Live Network Calls)
# =============================================================================
class SecretVaultPort(Protocol):
    """Port for external secret storage; zero plaintext secrets in domain records."""

    def store_secret(
        self,
        namespace: str,
        secret_value: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        ...

    def get_secret(self, secret_ref: str) -> str | None:
        ...

    def delete_secret(self, secret_ref: str) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class OidcIdTokenClaims:
    """Verified OIDC ID token claims; identity binding uses (iss, sub) ONLY (never email)."""

    iss: str
    aud: str | tuple[str, ...]
    nonce: str
    exp: int
    sub: str
    email: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionTokenGrant:
    """OAuth exchange/refresh token bundle with tokens strictly redacted from repr/str."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in_seconds: int
    granted_scopes: tuple[str, ...]
    plan_tier: str
    plan_inference_eligible: bool
    id_token_claims: OidcIdTokenClaims

    def __repr__(self) -> str:
        return (
            "SubscriptionTokenGrant("
            "access_token=<REDACTED>, "
            "refresh_token=<REDACTED>, "
            f"expires_in_seconds={self.expires_in_seconds}, "
            f"granted_scopes={self.granted_scopes!r}, "
            f"plan_tier={self.plan_tier!r}, "
            f"plan_inference_eligible={self.plan_inference_eligible})"
        )

    def __str__(self) -> str:
        return repr(self)


class SubscriptionOAuthExchangePort(Protocol):
    """Injectable port for OAuth code exchange and refresh token rotation."""

    def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> SubscriptionTokenGrant:
        ...

    def refresh_tokens(
        self, *, refresh_token: str, external_subject: str
    ) -> SubscriptionTokenGrant:
        ...


@dataclass(frozen=True, slots=True)
class RemoteRevokeResult:
    """Outcome of attempting upstream OAuth token revocation."""

    confirmed: bool
    status: RemoteRevocationStatus
    reason_code: str | None = None


class RemoteRevokerPort(Protocol):
    """Injectable port for best-effort upstream OAuth revocation during unlink."""

    def revoke(
        self,
        *,
        provider: LlmProviderMode,
        external_subject: str,
        token_secret_ref: str,
        access_token: str | None,
        refresh_token: str | None,
    ) -> RemoteRevokeResult:
        ...


class InMemorySecretVault:
    """Thread-safe deterministic in-memory SecretVaultPort for offline tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: dict[str, str] = {}
        self._fail_next_delete = False

    def fail_next_delete(self) -> None:
        """Inject a single delete failure to verify SECRET_WIPE_RETRY_PENDING tracking."""
        with self._lock:
            self._fail_next_delete = True

    def store_secret(
        self,
        namespace: str,
        secret_value: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        safe_ns = "".join(ch for ch in namespace if ch.isalnum() or ch in "-_")
        ref = f"secret://{safe_ns}/{uuid.uuid4()}"
        with self._lock:
            self._secrets[ref] = secret_value
        return ref

    def get_secret(self, secret_ref: str) -> str | None:
        with self._lock:
            return self._secrets.get(secret_ref)

    def delete_secret(self, secret_ref: str) -> bool:
        with self._lock:
            if self._fail_next_delete:
                self._fail_next_delete = False
                raise RuntimeError("Injected secret vault delete failure.")
            return self._secrets.pop(secret_ref, None) is not None

    def all_refs(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._secrets.keys())

    def __repr__(self) -> str:
        with self._lock:
            return f"InMemorySecretVault(count={len(self._secrets)})"


# =============================================================================
# Configuration & Lifecycle Result Models
# =============================================================================
@dataclass(frozen=True, slots=True)
class SubscriptionOAuthConfig:
    """Strict allowlist & OIDC claim verification settings for subscription OAuth."""

    allowed_redirect_uris: frozenset[str]
    expected_oidc_issuer: str
    expected_oidc_audience: str
    required_plan_inference_scopes: frozenset[str] = frozenset(
        {"chatgpt.subscription.inference"}
    )
    authorize_endpoint: str = "https://auth.openai.com/oauth/authorize"
    state_ttl_seconds: int = 600
    clock_skew_seconds: int = 30
    policy_version: str = DEFAULT_LLM_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class _PendingOAuthState:
    """Internal single-use state record storing hashes & vault ref ONLY (zero raw state/verifier)."""

    state_hash: str
    nonce_hash: str
    workspace_id: str
    owner_user_id: str
    provider: LlmProviderMode
    redirect_uri: str
    pkce_code_challenge: str
    pkce_verifier_ref: str
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionOAuthStartResult:
    """Result handed back to initiate browser redirect (state & nonce redacted from repr)."""

    provider: LlmProviderMode
    authorization_url: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    pkce_code_challenge: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "SubscriptionOAuthStartResult("
            f"provider={self.provider.value!r}, "
            "state=<REDACTED>, "
            "nonce=<REDACTED>, "
            f"redirect_uri={self.redirect_uri!r}, "
            f"expires_at={self.expires_at!r})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True)
class RefreshConnectionResult:
    """Outcome of a serialized token refresh attempt."""

    connection: SubscriptionConnectionRecord
    refreshed_remotely: bool
    coalesced: bool


@dataclass(frozen=True, slots=True)
class UnlinkConnectionResult:
    """Auditable outcome of strict 3-step unlink (local fence -> remote revoke -> local wipe)."""

    connection: SubscriptionConnectionRecord
    executed_steps: tuple[str, ...]
    remote_revocation_status: RemoteRevocationStatus
    secret_wipe_status: SecretWipeStatus
    remote_revocation_error: str | None = None
    secret_wipe_error: str | None = None


def _hash_nonce(raw_nonce: str) -> str:
    return hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()


def _parse_token_bundle(raw_secret: str | None) -> tuple[str | None, str | None]:
    if not raw_secret:
        return None, None
    try:
        parsed = json.loads(raw_secret)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, Mapping):
        return None, None
    access = parsed.get("access_token")
    refresh = parsed.get("refresh_token")
    return (
        str(access) if isinstance(access, str) and access else None,
        str(refresh) if isinstance(refresh, str) and refresh else None,
    )


def _audience_matches(aud_claim: str | Sequence[str], expected_aud: str) -> bool:
    if isinstance(aud_claim, str):
        return hmac.compare_digest(aud_claim, expected_aud)
    return any(isinstance(item, str) and hmac.compare_digest(item, expected_aud) for item in aud_claim)


# =============================================================================
# SubscriptionConnectionLifecycleManager
# =============================================================================
class SubscriptionConnectionLifecycleManager:
    """Manages OAuth PKCE/OIDC connect, per-repo consent, serialized refresh, and 3-step unlink."""

    def __init__(
        self,
        *,
        config: SubscriptionOAuthConfig,
        secret_vault: SecretVaultPort,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._vault = secret_vault
        self._clock = clock
        self._state_lock = threading.Lock()
        self._pending_states: dict[str, _PendingOAuthState] = {}
        self._connections: dict[str, SubscriptionConnectionRecord] = {}
        self._subject_index: dict[tuple[str, str, str, str, str], str] = {}
        self._connection_locks: dict[str, threading.Lock] = {}
        self._consents: dict[tuple[str, str], ExternalSendConsentRecord] = {}
        self._deletion_fenced_resources: set[tuple[str, str]] = set()
        self._required_scopes = frozenset(
            s.strip().lower() for s in config.required_plan_inference_scopes if s.strip()
        )

    @property
    def secret_vault(self) -> SecretVaultPort:
        return self._vault

    @property
    def config(self) -> SubscriptionOAuthConfig:
        return self._config

    def debug_state_repr(self) -> str:
        """Return safe representation of internal state for leak-audit tests."""
        with self._state_lock:
            return repr(
                {
                    "pending_states": self._pending_states,
                    "connections": self._connections,
                    "consents": self._consents,
                }
            )

    def _require_active_access(self, access: WorkspaceAccessContext | None) -> WorkspaceAccessContext:
        if access is None or not access.workspace_id.strip() or not access.user_id.strip():
            raise LlmLifecycleError(
                code=LlmGateReasonCode.WORKSPACE_MEMBERSHIP_REQUIRED,
                message="Verified workspace membership context is required.",
            )
        return access

    def _get_connection_lock(self, connection_id: str) -> threading.Lock:
        with self._state_lock:
            lock = self._connection_locks.get(connection_id)
            if lock is None:
                lock = threading.Lock()
                self._connection_locks[connection_id] = lock
            return lock

    # -------------------------------------------------------------------------
    # 1. Connect Start & Callback Verification (PKCE S256 + OIDC + Scope Gate)
    # -------------------------------------------------------------------------
    def start_connect(
        self,
        access: WorkspaceAccessContext,
        *,
        redirect_uri: str,
        requested_scopes: tuple[str, ...] | None = None,
        provider: LlmProviderMode = LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH,
    ) -> SubscriptionOAuthStartResult:
        verified_access = self._require_active_access(access)
        if provider in PROHIBITED_PROVIDER_MODES or provider is not LlmProviderMode.CHATGPT_SUBSCRIPTION_OAUTH:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.PROHIBITED_PROVIDER_MODE,
                message=f"Provider mode {provider.value} is not allowed for subscription OAuth connect.",
            )
        if redirect_uri not in self._config.allowed_redirect_uris:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.INVALID_REDIRECT_URI,
                message="redirect_uri is not in the exact allowlist.",
            )

        scopes = (
            tuple(sorted(set(s.strip().lower() for s in requested_scopes if s.strip())))
            if requested_scopes is not None
            else tuple(sorted({"openid", *self._required_scopes}))
        )
        if not self._required_scopes.issubset(frozenset(scopes)):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.LOGIN_ONLY_SCOPE_REJECTED,
                message="Requested OAuth scopes must include plan inference scope (login-only rejected).",
            )

        now = self._clock()
        raw_state = secrets.token_urlsafe(32)
        raw_nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = derive_pkce_s256_challenge(code_verifier)
        state_hash = hash_oauth_state(raw_state)
        nonce_hash = _hash_nonce(raw_nonce)
        expires_at = now + timedelta(seconds=self._config.state_ttl_seconds)

        pkce_verifier_ref = self._vault.store_secret(
            _PKCE_VAULT_NAMESPACE,
            code_verifier,
            metadata={"workspace_id": verified_access.workspace_id},
        )
        pending = _PendingOAuthState(
            state_hash=state_hash,
            nonce_hash=nonce_hash,
            workspace_id=verified_access.workspace_id,
            owner_user_id=verified_access.user_id,
            provider=provider,
            redirect_uri=redirect_uri,
            pkce_code_challenge=code_challenge,
            pkce_verifier_ref=pkce_verifier_ref,
            expires_at=expires_at,
            created_at=now,
            consumed_at=None,
        )
        with self._state_lock:
            self._pending_states[state_hash] = pending

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._config.expected_oidc_audience,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": raw_state,
                "nonce": raw_nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return SubscriptionOAuthStartResult(
            provider=provider,
            authorization_url=f"{self._config.authorize_endpoint}?{query}",
            state=raw_state,
            nonce=raw_nonce,
            pkce_code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            requested_scopes=scopes,
            expires_at=expires_at,
        )

    def complete_callback(
        self,
        access: WorkspaceAccessContext,
        *,
        state: str,
        code: str,
        redirect_uri: str,
        exchanger: SubscriptionOAuthExchangePort,
        provided_code_verifier: str | None = None,
    ) -> SubscriptionConnectionRecord:
        verified_access = self._require_active_access(access)
        if not state or not code:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OAUTH_STATE_INVALID_OR_REPLAYED,
                message="Missing OAuth callback state or authorization code.",
            )
        if redirect_uri not in self._config.allowed_redirect_uris:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.INVALID_REDIRECT_URI,
                message="Callback redirect_uri is not in the exact allowlist.",
            )

        now = self._clock()
        state_hash = hash_oauth_state(state)

        # Atomically consume single-use state under lock.
        with self._state_lock:
            pending = self._pending_states.get(state_hash)
            if (
                pending is None
                or pending.consumed_at is not None
                or pending.expires_at <= now
                or pending.workspace_id != verified_access.workspace_id
                or pending.owner_user_id != verified_access.user_id
                or pending.redirect_uri != redirect_uri
            ):
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.OAUTH_STATE_INVALID_OR_REPLAYED,
                    message="OAuth state is missing, expired, mismatched, or already consumed.",
                )
            consumed = _PendingOAuthState(
                state_hash=pending.state_hash,
                nonce_hash=pending.nonce_hash,
                workspace_id=pending.workspace_id,
                owner_user_id=pending.owner_user_id,
                provider=pending.provider,
                redirect_uri=pending.redirect_uri,
                pkce_code_challenge=pending.pkce_code_challenge,
                pkce_verifier_ref=pending.pkce_verifier_ref,
                expires_at=pending.expires_at,
                created_at=pending.created_at,
                consumed_at=now,
            )
            self._pending_states[state_hash] = consumed

        # Retrieve and always wipe ephemeral PKCE code_verifier.
        try:
            stored_verifier = self._vault.get_secret(pending.pkce_verifier_ref)
        finally:
            try:
                self._vault.delete_secret(pending.pkce_verifier_ref)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete ephemeral PKCE verifier ref %s", pending.pkce_verifier_ref)

        effective_verifier = (
            provided_code_verifier if provided_code_verifier is not None else stored_verifier
        )
        if not effective_verifier or not hmac.compare_digest(
            derive_pkce_s256_challenge(effective_verifier),
            pending.pkce_code_challenge,
        ):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.PKCE_VERIFICATION_FAILED,
                message="PKCE S256 code_verifier does not match stored code_challenge.",
            )

        try:
            grant = exchanger.exchange_code(
                code=code,
                code_verifier=effective_verifier,
                redirect_uri=redirect_uri,
            )
        except LlmLifecycleError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                message=f"Subscription OAuth token exchange failed: {type(exc).__name__}",
            ) from exc

        # Strictly verify OIDC ID token claims (iss, aud, nonce, exp, sub). Never bind by email!
        claims = grant.id_token_claims
        if not claims.sub or not claims.sub.strip():
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OIDC_CLAIMS_INVALID,
                message="OIDC ID token missing required non-empty sub claim.",
            )
        if not hmac.compare_digest(claims.iss, self._config.expected_oidc_issuer):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OIDC_CLAIMS_INVALID,
                message="OIDC ID token issuer mismatch.",
            )
        if not _audience_matches(claims.aud, self._config.expected_oidc_audience):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OIDC_CLAIMS_INVALID,
                message="OIDC ID token audience mismatch.",
            )
        if not hmac.compare_digest(_hash_nonce(claims.nonce), pending.nonce_hash):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OIDC_CLAIMS_INVALID,
                message="OIDC ID token nonce mismatch.",
            )
        if claims.exp <= int((now - timedelta(seconds=self._config.clock_skew_seconds)).timestamp()):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.OIDC_CLAIMS_INVALID,
                message="OIDC ID token is expired.",
            )

        # Separate service login from ChatGPT plan inference permission.
        granted_scopes = tuple(sorted(set(s.strip().lower() for s in grant.granted_scopes if s.strip())))
        if not self._required_scopes.issubset(frozenset(granted_scopes)):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.LOGIN_ONLY_SCOPE_REJECTED,
                message="OAuth grant is login-only/identity-only and lacks required plan inference scope.",
            )
        if not grant.plan_inference_eligible:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.PLAN_ELIGIBILITY_UNVERIFIED,
                message=f"Plan tier {grant.plan_tier!r} is not eligible for subscription inference.",
            )

        # Store tokens ONLY in SecretVaultPort; record gets opaque token_secret_ref ONLY.
        token_bundle = json.dumps(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
            },
            sort_keys=True,
        )
        token_secret_ref = self._vault.store_secret(
            _TOKEN_VAULT_NAMESPACE,
            token_bundle,
            metadata={
                "workspace_id": verified_access.workspace_id,
                "owner_user_id": verified_access.user_id,
            },
        )
        expires_at = now + timedelta(seconds=max(1, int(grant.expires_in_seconds)))

        # Identity key strictly uses (workspace_id, owner_user_id, provider, iss, sub) — NEVER email!
        subject_key = (
            verified_access.workspace_id,
            verified_access.user_id,
            pending.provider.value,
            claims.iss,
            claims.sub.strip(),
        )
        with self._state_lock:
            existing_id = self._subject_index.get(subject_key)
            existing = self._connections.get(existing_id) if existing_id is not None else None
            old_secret_ref = existing.token_secret_ref if existing is not None else None
            conn_id = existing.connection_id if existing is not None else str(uuid.uuid4())
            new_version = (existing.connection_version + 1) if existing is not None else 1
            created_at = existing.created_at if existing is not None else now

            record = SubscriptionConnectionRecord(
                connection_id=conn_id,
                connection_version=new_version,
                workspace_id=verified_access.workspace_id,
                owner_user_id=verified_access.user_id,
                provider=pending.provider,
                oidc_issuer=claims.iss,
                oidc_subject=claims.sub.strip(),
                granted_scopes=granted_scopes,
                plan_tier=grant.plan_tier,
                plan_inference_eligible=grant.plan_inference_eligible,
                token_secret_ref=token_secret_ref,
                token_expires_at=expires_at,
                status=SubscriptionConnectionStatus.ACTIVE,
                remote_revocation_status=RemoteRevocationStatus.NOT_REQUESTED,
                secret_wipe_status=SecretWipeStatus.ACTIVE,
                policy_version=self._config.policy_version,
                created_at=created_at,
                updated_at=now,
                revoked_at=None,
            )
            self._connections[conn_id] = record
            self._subject_index[subject_key] = conn_id
            self._connection_locks.setdefault(conn_id, threading.Lock())

        if old_secret_ref is not None and old_secret_ref != token_secret_ref:
            try:
                self._vault.delete_secret(old_secret_ref)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete superseded secret ref %s", old_secret_ref)

        return record

    # -------------------------------------------------------------------------
    # 2. External Evidence Send Consent & Deletion Fence (OD-002 + ADR-020)
    # -------------------------------------------------------------------------
    def grant_external_send_consent(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        consent_version: str,
        acknowledged_public_unmasked_send: bool,
        acknowledged_third_party_non_recallable: bool,
    ) -> ExternalSendConsentRecord:
        verified_access = self._require_active_access(access)
        if not source_resource_id.strip() or not consent_version.strip():
            raise LlmLifecycleError(
                code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                message="source_resource_id and consent_version are required.",
            )
        if not (acknowledged_public_unmasked_send and acknowledged_third_party_non_recallable):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                message=(
                    "External send consent requires explicit acknowledgment of both "
                    "public unmasked transmission and third-party non-recallability."
                ),
            )
        now = self._clock()
        record = ExternalSendConsentRecord(
            consent_id=str(uuid.uuid4()),
            workspace_id=verified_access.workspace_id,
            source_resource_id=source_resource_id.strip(),
            consented_by_user_id=verified_access.user_id,
            consent_version=consent_version.strip(),
            acknowledged_public_unmasked_send=True,
            acknowledged_third_party_non_recallable=True,
            granted_at=now,
            revoked_at=None,
        )
        with self._state_lock:
            self._consents[(verified_access.workspace_id, source_resource_id.strip())] = record
        return record

    def revoke_external_send_consent(
        self,
        access: WorkspaceAccessContext,
        *,
        source_resource_id: str,
        reason: str = "USER_REVOKED",
    ) -> ExternalSendConsentRecord:
        verified_access = self._require_active_access(access)
        now = self._clock()
        key = (verified_access.workspace_id, source_resource_id.strip())
        with self._state_lock:
            existing = self._consents.get(key)
            if existing is None:
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                    message="No external send consent record found for this repository.",
                )
            revoked = ExternalSendConsentRecord(
                consent_id=existing.consent_id,
                workspace_id=existing.workspace_id,
                source_resource_id=existing.source_resource_id,
                consented_by_user_id=existing.consented_by_user_id,
                consent_version=existing.consent_version,
                acknowledged_public_unmasked_send=existing.acknowledged_public_unmasked_send,
                acknowledged_third_party_non_recallable=existing.acknowledged_third_party_non_recallable,
                granted_at=existing.granted_at,
                revoked_at=now,
                revoked_by_user_id=verified_access.user_id,
                revocation_reason=redact_llm_text(reason),
            )
            self._consents[key] = revoked
            return revoked

    def get_external_send_consent(
        self, *, workspace_id: str, source_resource_id: str
    ) -> ExternalSendConsentRecord | None:
        with self._state_lock:
            return self._consents.get((workspace_id, source_resource_id))

    def mark_source_deletion_fenced(
        self, workspace_id: str, source_resource_id: str, *, fenced: bool = True
    ) -> None:
        key = (workspace_id, source_resource_id)
        with self._state_lock:
            if fenced:
                self._deletion_fenced_resources.add(key)
            else:
                self._deletion_fenced_resources.discard(key)

    def is_source_deletion_fenced(self, workspace_id: str, source_resource_id: str) -> bool:
        with self._state_lock:
            return (workspace_id, source_resource_id) in self._deletion_fenced_resources

    # -------------------------------------------------------------------------
    # 3. Connection Lookup & Strict Ownership / Workspace Enforcement
    # -------------------------------------------------------------------------
    def get_connection(self, connection_id: str) -> SubscriptionConnectionRecord | None:
        with self._state_lock:
            return self._connections.get(connection_id)

    def get_connection_by_subject(
        self, *, oidc_issuer: str, oidc_subject: str
    ) -> SubscriptionConnectionRecord | None:
        with self._state_lock:
            for conn in self._connections.values():
                if conn.oidc_issuer == oidc_issuer and conn.oidc_subject == oidc_subject:
                    return conn
            return None

    def get_known_secret_values_for_connection(self, connection_id: str) -> tuple[str, ...]:
        """Internal helper for operational secret leak detection without logging secrets."""
        conn = self.get_connection(connection_id)
        if conn is None or conn.token_secret_ref is None:
            return ()
        access_tok, refresh_tok = _parse_token_bundle(self._vault.get_secret(conn.token_secret_ref))
        return tuple(tok for tok in (access_tok, refresh_tok) if tok)

    def verify_connection_access(
        self,
        access: WorkspaceAccessContext,
        *,
        connection_id: str,
        expected_connection_version: int | None = None,
    ) -> SubscriptionConnectionRecord:
        verified_access = self._require_active_access(access)
        conn = self.get_connection(connection_id)
        if conn is None:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_NOT_FOUND,
                message="Subscription connection not found.",
            )
        if (
            conn.workspace_id != verified_access.workspace_id
            or conn.owner_user_id != verified_access.user_id
        ):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                message="Connection cannot be accessed across workspaces or by a non-owner.",
            )
        if conn.status is not SubscriptionConnectionStatus.ACTIVE:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                message=f"Subscription connection is not ACTIVE (status={conn.status.value}).",
            )
        if expected_connection_version is not None and conn.connection_version != expected_connection_version:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_VERSION_STALE,
                message="Subscription connection version has advanced or been fenced.",
            )
        return conn

    # -------------------------------------------------------------------------
    # 4. Serialized Token Refresh (Per-Connection Lock + Version Coalescing)
    # -------------------------------------------------------------------------
    def refresh_connection_tokens(
        self,
        access: WorkspaceAccessContext,
        *,
        connection_id: str,
        stale_version: int,
        exchanger: SubscriptionOAuthExchangePort,
    ) -> RefreshConnectionResult:
        verified_access = self._require_active_access(access)
        # Validate existence and ownership before acquiring per-connection lock.
        initial = self.get_connection(connection_id)
        if initial is None:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_NOT_FOUND,
                message="Subscription connection not found.",
            )
        if (
            initial.workspace_id != verified_access.workspace_id
            or initial.owner_user_id != verified_access.user_id
        ):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                message="Connection refresh denied for non-owner or cross-workspace caller.",
            )

        conn_lock = self._get_connection_lock(connection_id)
        with conn_lock:
            current = self.get_connection(connection_id)
            if current is None:
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.CONNECTION_NOT_FOUND,
                    message="Subscription connection not found.",
                )
            if current.status is not SubscriptionConnectionStatus.ACTIVE:
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                    message="Cannot refresh a revoked or inactive subscription connection.",
                )
            # Another concurrent caller already refreshed this stale version -> coalesce!
            if current.connection_version > stale_version:
                return RefreshConnectionResult(
                    connection=current,
                    refreshed_remotely=False,
                    coalesced=True,
                )

            old_secret_ref = current.token_secret_ref
            _, refresh_token = _parse_token_bundle(
                self._vault.get_secret(old_secret_ref) if old_secret_ref else None
            )
            if not refresh_token:
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                    message="Refresh token is unavailable in secret vault; re-authentication required.",
                )

            now = self._clock()
            try:
                grant = exchanger.refresh_tokens(
                    refresh_token=refresh_token,
                    external_subject=current.oidc_subject,
                )
            except Exception as exc:  # noqa: BLE001
                with self._state_lock:
                    degraded = SubscriptionConnectionRecord(
                        connection_id=current.connection_id,
                        connection_version=current.connection_version + 1,
                        workspace_id=current.workspace_id,
                        owner_user_id=current.owner_user_id,
                        provider=current.provider,
                        oidc_issuer=current.oidc_issuer,
                        oidc_subject=current.oidc_subject,
                        granted_scopes=current.granted_scopes,
                        plan_tier=current.plan_tier,
                        plan_inference_eligible=False,
                        token_secret_ref=current.token_secret_ref,
                        token_expires_at=current.token_expires_at,
                        status=SubscriptionConnectionStatus.REAUTH_REQUIRED,
                        remote_revocation_status=current.remote_revocation_status,
                        secret_wipe_status=current.secret_wipe_status,
                        policy_version=current.policy_version,
                        created_at=current.created_at,
                        updated_at=now,
                        revoked_at=current.revoked_at,
                    )
                    self._connections[connection_id] = degraded
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.CONNECTION_REVOKED_OR_INACTIVE,
                    message=f"Subscription token refresh failed: {type(exc).__name__}",
                ) from exc

            granted_scopes = tuple(
                sorted(set(s.strip().lower() for s in grant.granted_scopes if s.strip()))
            )
            if not self._required_scopes.issubset(frozenset(granted_scopes)):
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.LOGIN_ONLY_SCOPE_REJECTED,
                    message="Refreshed grant lacks required plan inference scope.",
                )
            if not grant.plan_inference_eligible:
                raise LlmLifecycleError(
                    code=LlmGateReasonCode.PLAN_ELIGIBILITY_UNVERIFIED,
                    message="Refreshed account plan is no longer eligible for subscription inference.",
                )

            new_bundle = json.dumps(
                {
                    "access_token": grant.access_token,
                    "refresh_token": grant.refresh_token,
                },
                sort_keys=True,
            )
            new_secret_ref = self._vault.store_secret(
                _TOKEN_VAULT_NAMESPACE,
                new_bundle,
                metadata={
                    "workspace_id": current.workspace_id,
                    "owner_user_id": current.owner_user_id,
                },
            )
            updated = SubscriptionConnectionRecord(
                connection_id=current.connection_id,
                connection_version=current.connection_version + 1,
                workspace_id=current.workspace_id,
                owner_user_id=current.owner_user_id,
                provider=current.provider,
                oidc_issuer=current.oidc_issuer,
                oidc_subject=current.oidc_subject,
                granted_scopes=granted_scopes,
                plan_tier=grant.plan_tier,
                plan_inference_eligible=grant.plan_inference_eligible,
                token_secret_ref=new_secret_ref,
                token_expires_at=now + timedelta(seconds=max(1, int(grant.expires_in_seconds))),
                status=SubscriptionConnectionStatus.ACTIVE,
                remote_revocation_status=current.remote_revocation_status,
                secret_wipe_status=SecretWipeStatus.ACTIVE,
                policy_version=current.policy_version,
                created_at=current.created_at,
                updated_at=now,
                revoked_at=None,
            )
            with self._state_lock:
                self._connections[connection_id] = updated

            if old_secret_ref is not None and old_secret_ref != new_secret_ref:
                try:
                    self._vault.delete_secret(old_secret_ref)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to delete previous token secret ref %s", old_secret_ref)

            return RefreshConnectionResult(
                connection=updated,
                refreshed_remotely=True,
                coalesced=False,
            )

    # -------------------------------------------------------------------------
    # 5. Strict 3-Step Unlink: local fence -> remote revoke -> local secret wipe
    # -------------------------------------------------------------------------
    def unlink_connection(
        self,
        access: WorkspaceAccessContext,
        *,
        connection_id: str,
        revoker: RemoteRevokerPort,
        reason: str = "USER_UNLINKED",
    ) -> UnlinkConnectionResult:
        verified_access = self._require_active_access(access)
        initial = self.get_connection(connection_id)
        if initial is None:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.CONNECTION_NOT_FOUND,
                message="Subscription connection not found.",
            )
        if (
            initial.workspace_id != verified_access.workspace_id
            or initial.owner_user_id != verified_access.user_id
        ):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.WORKSPACE_OR_OWNER_MISMATCH,
                message="Connection unlink denied for non-owner or cross-workspace caller.",
            )

        conn_lock = self._get_connection_lock(connection_id)
        executed_steps: list[str] = []

        with conn_lock:
            now = self._clock()
            # STEP 1: LOCAL FENCE FIRST — atomically mark REVOKED and bump connection_version.
            with self._state_lock:
                current = self._connections[connection_id]
                secret_ref_to_wipe = current.token_secret_ref
                fenced_record = SubscriptionConnectionRecord(
                    connection_id=current.connection_id,
                    connection_version=current.connection_version + 1,
                    workspace_id=current.workspace_id,
                    owner_user_id=current.owner_user_id,
                    provider=current.provider,
                    oidc_issuer=current.oidc_issuer,
                    oidc_subject=current.oidc_subject,
                    granted_scopes=current.granted_scopes,
                    plan_tier=current.plan_tier,
                    plan_inference_eligible=False,
                    token_secret_ref=current.token_secret_ref,
                    token_expires_at=current.token_expires_at,
                    status=SubscriptionConnectionStatus.REVOKED,
                    remote_revocation_status=RemoteRevocationStatus.UNCONFIRMED_REMOTE,
                    secret_wipe_status=current.secret_wipe_status,
                    policy_version=current.policy_version,
                    created_at=current.created_at,
                    updated_at=now,
                    revoked_at=current.revoked_at or now,
                )
                self._connections[connection_id] = fenced_record
            executed_steps.append("LOCAL_FENCE")

            # STEP 2: REMOTE REVOKE (best-effort; failures NEVER reopen local fence).
            access_tok, refresh_tok = _parse_token_bundle(
                self._vault.get_secret(secret_ref_to_wipe) if secret_ref_to_wipe else None
            )
            remote_status = RemoteRevocationStatus.UNCONFIRMED_REMOTE
            remote_err: str | None = None
            try:
                if secret_ref_to_wipe is not None:
                    rev_res = revoker.revoke(
                        provider=fenced_record.provider,
                        external_subject=fenced_record.oidc_subject,
                        token_secret_ref=secret_ref_to_wipe,
                        access_token=access_tok,
                        refresh_token=refresh_tok,
                    )
                    remote_status = (
                        RemoteRevocationStatus.CONFIRMED_REMOTE
                        if rev_res.confirmed and rev_res.status is RemoteRevocationStatus.CONFIRMED_REMOTE
                        else RemoteRevocationStatus.UNCONFIRMED_REMOTE
                    )
            except Exception as exc:  # noqa: BLE001
                remote_status = RemoteRevocationStatus.UNCONFIRMED_REMOTE
                remote_err = redact_llm_text(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "Remote subscription revocation unconfirmed for connection=%s reason=%s",
                    connection_id,
                    redact_llm_text(reason),
                )
            finally:
                access_tok = None
                refresh_tok = None
            executed_steps.append("REMOTE_REVOKE")

            # STEP 3: LOCAL SECRET WIPE (runs even if remote revoke failed!).
            wipe_status = SecretWipeStatus.WIPED
            wipe_err: str | None = None
            final_secret_ref: str | None = None
            if secret_ref_to_wipe is not None:
                try:
                    self._vault.delete_secret(secret_ref_to_wipe)
                    wipe_status = SecretWipeStatus.WIPED
                    final_secret_ref = None
                except Exception as exc:  # noqa: BLE001
                    wipe_status = SecretWipeStatus.SECRET_WIPE_RETRY_PENDING
                    wipe_err = redact_llm_text(f"{type(exc).__name__}: {exc}")
                    final_secret_ref = secret_ref_to_wipe
            executed_steps.append("LOCAL_SECRET_WIPE")

            final_record = SubscriptionConnectionRecord(
                connection_id=fenced_record.connection_id,
                connection_version=fenced_record.connection_version,
                workspace_id=fenced_record.workspace_id,
                owner_user_id=fenced_record.owner_user_id,
                provider=fenced_record.provider,
                oidc_issuer=fenced_record.oidc_issuer,
                oidc_subject=fenced_record.oidc_subject,
                granted_scopes=fenced_record.granted_scopes,
                plan_tier=fenced_record.plan_tier,
                plan_inference_eligible=False,
                token_secret_ref=final_secret_ref,
                token_expires_at=fenced_record.token_expires_at,
                status=SubscriptionConnectionStatus.REVOKED,
                remote_revocation_status=remote_status,
                secret_wipe_status=wipe_status,
                policy_version=fenced_record.policy_version,
                created_at=fenced_record.created_at,
                updated_at=self._clock(),
                revoked_at=fenced_record.revoked_at,
            )
            with self._state_lock:
                self._connections[connection_id] = final_record

        return UnlinkConnectionResult(
            connection=final_record,
            executed_steps=tuple(executed_steps),
            remote_revocation_status=remote_status,
            secret_wipe_status=wipe_status,
            remote_revocation_error=remote_err,
            secret_wipe_error=wipe_err,
        )

    # -------------------------------------------------------------------------
    # 6. Non-Raw ConnectionJobReference Issuance
    # -------------------------------------------------------------------------
    def issue_job_reference(
        self,
        access: WorkspaceAccessContext,
        *,
        connection_id: str,
        source_resource_id: str,
        stage_code: LlmStageCode,
        input_hash: str,
    ) -> ConnectionJobReference:
        verified_access = self._require_active_access(access)
        conn = self.verify_connection_access(verified_access, connection_id=connection_id)
        if self.is_source_deletion_fenced(verified_access.workspace_id, source_resource_id):
            raise LlmLifecycleError(
                code=LlmGateReasonCode.SOURCE_POLICY_OR_DELETION_FENCED,
                message="Source resource is deletion-fenced; cannot issue job reference.",
            )
        consent = self.get_external_send_consent(
            workspace_id=verified_access.workspace_id,
            source_resource_id=source_resource_id,
        )
        if consent is None:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                message="External send consent is required before issuing a job reference.",
            )
        if consent.revoked_at is not None:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REVOKED,
                message="External send consent has been revoked.",
            )
        if not consent.is_active:
            raise LlmLifecycleError(
                code=LlmGateReasonCode.EXTERNAL_SEND_CONSENT_REQUIRED,
                message="External send consent is incomplete.",
            )
        return ConnectionJobReference(
            connection_id=conn.connection_id,
            connection_version=conn.connection_version,
            owner_user_id=conn.owner_user_id,
            workspace_id=conn.workspace_id,
            consent_version=consent.consent_version,
            policy_version=conn.policy_version,
            stage_code=stage_code,
            input_hash=input_hash,
        )
