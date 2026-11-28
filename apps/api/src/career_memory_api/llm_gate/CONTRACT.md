# Subscription LLM OAuth, Consent, Budget & Execution Fence Gate Contract (`M4-T408` / `INF-08`)

## 1. Scope & Hard-OFF Governance (`OD-002` / `OD-014` / `ADR-020`)
`career_memory_api.llm_gate` is the single domain and execution boundary for all 5 bounded AI pipeline stages (`STAGE_2A`..`STAGE_3C`).

- **Default Hard-OFF (`LlmRuntimeMode.OFF_MANUAL_ONLY`)**: `HostedLlmApprovalConfig()` defaults all 4 gates to `False`:
  - `hosted_deployment_approved = False`
  - `zero_overage_guaranteed = False`
  - `od002_data_policy_verified = False`
  - `od014_token_store_verified = False`
  If ANY flag is `False`, live subscription calls fail closed with `LlmGateOutcome.MANUAL_ONLY_BLOCKED`, `external_call_count == 0`, `retryable == False`, and actionable `free_alternatives` (`REDUCE_WORK_SCOPE`, `MANUAL_RESUME_AFTER_QUOTA_RESET`, `NON_AI_IMPORT_AND_EVENT_CURATION`, `LOCAL_SYNTHETIC_FIXTURE_DEV_ONLY`).
- **Zero Prohibited Modes & Zero Paid API Fallback**: `BYOK_API_KEY`, `SHARED_OPERATOR_KEY`, `BROWSER_COOKIE_IMPORT`, `CLI_CREDENTIAL_IMPORT`, `GEMINI_SUBSCRIPTION_UNSUPPORTED`, `CLAUDE_SUBSCRIPTION_UNSUPPORTED`, and `PAID_API_CREDITS_FALLBACK` are rejected unconditionally with `0` provider calls.
- **Zero DB User Token Tables & Zero Public OpenAPI Routes (`OD-014` OFF)**: No PostgreSQL user LLM token table and no public OpenAPI subscription/callback/BYOK route is added while `OD-014` remains `OFF`.
- **Public Source Unmasked Pass-Through (`ADR-020` / `PM-SEC-01` Post-MVP)**: Consented `PUBLIC` GitHub content units within budget are passed in-memory **as-is** to the stage adapter without sensitivity/PII scanning or masking. Server/API operational credentials (`Authorization`, `Cookie`, `Set-Cookie`, OAuth tokens, DB DSNs) are strictly stripped/blocked from adapter payloads, metadata, logs, and exceptions, and `0` source text is persisted after execution returns.

## 2. Exact Imports for Wave 2 (`T-402`, `T-403`) and Wave 3 (`T-404`, `T-405`)

```python
from career_memory_api.llm_gate import (
    # Policy & Approval Configuration
    HostedLlmApprovalConfig,
    LlmGateDecision,
    LlmGateOutcome,
    LlmGateReasonCode,
    LlmPolicyGate,
    LlmProviderMode,
    LlmRuntimeMode,
    LlmStageCode,
    PolicyGateRequest,
    StageBudgetPolicy,
    # OAuth / Consent / Unlink Lifecycle
    ConnectionJobReference,
    ExternalSendConsentRecord,
    InMemorySecretVault,
    RemoteRevokerPort,
    SecretVaultPort,
    SubscriptionConnectionLifecycleManager,
    SubscriptionOAuthConfig,
    SubscriptionOAuthExchangePort,
    # Bounded 5-Stage Runner & Adapters
    BoundedStageRunner,
    PolicyGatedProviderStageAdapter,
    ProviderTransportErrorKind,
    RawProviderStageResponse,
    StageAdapterInput,
    StageExecutionAdapterPort,
    StageExecutionRequest,
    StagePreCommitContext,
    StageRunEnvelope,
    StageRunStatus,
    SyntheticFixtureStageAdapter,
    # Provenance & Non-Raw Guards
    LivePilotGateEligibilityError,
    assert_no_raw_text_or_secret_keys,
    assert_not_synthetic_for_live_gate,
)
```

## 3. Stage-to-Schema Mapping (`career_memory_api.conformance.ai_schemas.validate_stage_payload`)
| `LlmStageCode` | T-106 Schema File | Downstream Consumer |
|---|---|---|
| `STAGE_2A_FACT_CANDIDATE_EXTRACTION` | `fact_candidate.v1.schema.json` | Wave 2 (`T-402` FactCandidateExtractor) |
| `STAGE_2B_FACT_SUPPORT_VALIDATION` | `fact_support_validation.v1.schema.json` | Wave 2 (`T-403` FactSupportValidator) |
| `STAGE_3A_CLAIM_GENERATION` | `claim_draft.v1.schema.json` | Wave 3 (`T-404` ClaimGenerator) |
| `STAGE_3B_CLAIM_ATOMIZATION` | `claim_atomization.v1.schema.json` | Wave 3 (`T-405` ClaimAtomizer) |
| `STAGE_3C_CLAIM_SUPPORT_VALIDATION` | `claim_support_validation.v1.schema.json` | Wave 3 (`T-405` ClaimSupportValidator) |

## 4. Mandatory Usage Pattern for Wave 2 / Wave 3 Services

### 4.1 Offline Synthetic Tests & Evaluation (`SYNTHETIC_FIXTURE_OFFLINE`)
1. Resolve the verified Plan C `OperationMemoryResolution` from `career_memory_api.resolver.RefetchResolverService` (`status == MATCHED`, exact `expected_revision_hash` and `span_sha256`).
2. Construct `LlmPolicyGate(approval_config=HostedLlmApprovalConfig())` (hard-OFF default stays intact).
3. Construct `SyntheticFixtureStageAdapter(fixtures_by_stage=...)` and `BoundedStageRunner(policy_gate=gate)`.
4. Invoke `runner.run_stage(StageExecutionRequest(..., provider_mode=LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE, allow_synthetic_fixture_mode=True, ...))`.
5. Check `envelope.status is StageRunStatus.SUCCEEDED` and `envelope.committed_allowed is True` before persisting validated non-raw outputs.
6. **Never treat synthetic runs as live pilot gate evidence**: every synthetic envelope sets `execution_origin == "SYNTHETIC_FIXTURE"` and `live_pilot_gate_eligible == False`, and `assert_not_synthetic_for_live_gate(envelope)` raises `LivePilotGateEligibilityError`.

### 4.2 Approved Subscription Execution (`CHATGPT_SUBSCRIPTION_OAUTH`)
1. Complete OAuth PKCE S256 + OIDC (`iss`, `aud`, `nonce`, `exp`, `sub`) via `SubscriptionConnectionLifecycleManager.start_connect(...)` and `complete_callback(...)` (rejects `login-only` / `identity-only` grants and never binds by email).
2. Capture separate per-repository consent via `grant_external_send_consent(...)` requiring both `acknowledged_public_unmasked_send=True` and `acknowledged_third_party_non_recallable=True`.
3. Persist **ONLY** `ConnectionJobReference.to_dict()` (`{connection_id, connection_version, owner_user_id, workspace_id, consent_version, policy_version, stage_code, input_hash}`) inside `jobs` or `generation_runs` — never tokens, headers, or raw source text.
4. Execute through `BoundedStageRunner.run_stage(...)` with `StageBudgetPolicy(overage_credit_disabled=True, quota_remaining_known_and_sufficient=True, ...)`. Note: `max_output_tokens_hint` is advisory only and never satisfies zero-overage guarantees alone.
5. Respect `envelope.committed_allowed` and `envelope.can_promote_to_supported`:
   - Any blocked, stream-incomplete, schema-invalid (`DRAFT_INVALID`), ambiguous-timeout (`FAILED_AMBIGUOUS_TIMEOUT`, `retryable=False`, `auto_retry_performed=False`), or pre-commit-fenced (`CANCELED_PRE_COMMIT_FENCE`) run MUST NEVER persist partial output or promote any Fact/Claim to `SUPPORTED` or `SOURCE_VERIFIED`.
6. Unlink executes in strict order `LOCAL_FENCE -> REMOTE_REVOKE -> LOCAL_SECRET_WIPE`: remote revocation failure keeps local status `REVOKED` and reports `RemoteRevocationStatus.UNCONFIRMED_REMOTE`.
