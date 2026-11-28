"""Deterministic M1 Integrated OpenAPI 3.1.0 generator and invariant verifier.

Generates ``contracts/openapi/career-memory-v1.openapi.json`` while preserving
``contracts/import-v1/openapi.yaml`` (blob ``07594d75425744d6149250a9f7ff95ac24659f80``)
completely untouched and 100% compatible on the 4 initial-import routes.
"""

from __future__ import annotations

import copy
from typing import Any

from career_memory_api.budgets.models import (
    CircuitBreakerState,
    QuotaAdmissionState,
    QuotaMeterItem,
    QuotaMeterStatus,
    QuotaStatusResponse,
)
from career_memory_api.claims.models import (
    ApproveClaimActionRequest,
    ClaimDetailResponse,
    ClaimGenerationStatus,
    ClaimListResponse,
    ClaimSupportsResponse,
    GenerateEventClaimsRequest,
    GenerateEventClaimsResponse,
    RejectClaimActionRequest,
    RevalidateClaimRequest,
)
from career_memory_api.conformance.ai_schemas import render_canonical_json
from career_memory_api.contracts_api.models import (
    ClaimApproveRequest,
    ClaimBoundaryResponse,
    ClaimDraftCreateRequest,
    ClaimEditRequest,
    DeletionConfirmAcceptedResponse,
    DeletionConfirmRequest,
    DeletionPreviewResponse,
    ErrorEnvelope,
    SearchEntityType,
    SearchResponse,
)
from career_memory_api.contracts_api.router import create_m1_contract_app
from career_memory_api.deletion.models import (
    DeletionComponentReceiptItem,
    DeletionJobStatusResponse,
)
from career_memory_api.exports.models import (
    CreateExportRequest,
    DeleteExportResponse,
    ExportDetailResponse,
    ExportListResponse,
)
from career_memory_api.facts.models import (
    ApproveFactRequest,
    ApproveFactResponse,
    EditFactRequest,
    EditFactResponse,
    FactAttributionScope,
    FactCausalBasis,
    FactCompletionState,
    FactEnvironmentScope,
    FactListResponse,
    FactSupportsResponse,
    RejectFactRequest,
    RejectFactResponse,
)
from career_memory_api.github_connect.models import (
    GitHubCredentialMode,
    PolicyDecision,
    PolicyGuardedOperation,
    PolicyGuidanceCode,
    PolicyTransitionKind,
)
from career_memory_api.identities.models import (
    ConfirmIdentityMappingRequest,
    ConfirmIdentityMappingResponse,
    IdentityAttributionBlockReason,
    IdentityListResponse,
    RevokeIdentityMappingRequest,
    RevokeIdentityMappingResponse,
)
from career_memory_api.jobs.models import DeletionJobStatus
from career_memory_api.resolver.models import ResolutionStatus
from career_memory_api.schema_contracts.enums import (
    ClaimAtomStatus,
    ClaimAtomType,
    ClaimStatus,
    ClaimVerification,
    ContentUnitType,
    DeletionComponent,
    DeletionComponentStatus,
    EventConstraintOrigin,
    EventConstraintStatus,
    EventConstraintType,
    ExportFormat,
    ExportPolicy,
    ExportStatus,
    ExportVerificationClass,
    ExternalIdentityMappingStatus,
    FactStatus,
    FactType,
    IdentityStatus,
    IdentityType,
    IdentityVerificationMethod,
    LinkStatus,
    QuotaMetricKind,
    RecordOrigin,
    RuntimePauseState,
    SourceProvider,
    SupportType,
    SupportValidator,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunMode, SyncRunStatus
from career_memory_api.work_events.models import (
    ConfirmWorkEventRequest,
    DissolveWorkEventRequest,
    DissolveWorkEventResponse,
    EditWorkEventRequest,
    MergeWorkEventsRequest,
    MergeWorkEventsResponse,
    RejectWorkEventRequest,
    SplitWorkEventRequest,
    SplitWorkEventResponse,
    WorkEventDetailResponse,
    WorkEventListResponse,
    WorkEventPolicyState,
)

INTEGRATED_OPENAPI_VERSION = "1.0.0-m1"
INTEGRATED_OPENAPI_TITLE = "Career Memory M1 Integrated API"

# Route prefixes/keywords that MUST NOT exist in M1 OpenAPI (T-106: no BYOK, no subscription LLM callbacks).
FORBIDDEN_OPENAPI_PATH_FRAGMENTS: tuple[str, ...] = (
    "byok",
    "api-keys",
    "llm-keys",
    "llm/callback",
    "subscription",
    "openai/callback",
    "anthropic/callback",
    "gemini/callback",
)


def _sync_runs_openapi_paths() -> dict[str, Any]:
    """M0-approved 4 initial-import paths aligned 1:1 with contracts/import-v1/openapi.yaml."""
    return {
        "/v1/sync-runs": {
            "get": {
                "operationId": "recoverInitialImportRun",
                "tags": ["sync-runs"],
                "summary": "Recover one readable import run for a repository",
                "parameters": [
                    {
                        "name": "source_resource_id",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "const": 1, "default": 1},
                    },
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "responses": {
                    "200": {
                        "description": "Zero or one readable run with its current status.",
                        "headers": {
                            "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RecoveredSyncRuns"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "422": {"description": "Invalid query parameter (missing source_resource_id or limit != 1)."},
                    "503": {"$ref": "#/components/responses/ImportAuthUnavailable"},
                },
            },
            "post": {
                "operationId": "createInitialImport",
                "tags": ["sync-runs"],
                "summary": "Request a one-time initial import plan",
                "parameters": [
                    {"$ref": "#/components/parameters/IdempotencyKey"},
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/InitialImportRequest"}
                        }
                    },
                },
                "responses": {
                    "202": {"$ref": "#/components/responses/SyncRunAccepted"},
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "409": {"$ref": "#/components/responses/InitialImportConflict"},
                    "422": {"description": "Missing or invalid Idempotency-Key or request body."},
                    "503": {"$ref": "#/components/responses/ImportAuthUnavailable"},
                },
            },
        },
        "/v1/sync-runs/{id}": {
            "parameters": [{"$ref": "#/components/parameters/SyncRunID"}],
            "get": {
                "operationId": "getInitialImportStatus",
                "tags": ["sync-runs"],
                "summary": "Read run status, checkpoint progress, limits, and retry eligibility",
                "parameters": [{"$ref": "#/components/parameters/RequestID"}],
                "responses": {
                    "200": {
                        "description": "Import and derived processing status, including PARTIAL and PARTIAL_WITH_LIMIT.",
                        "headers": {
                            "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ImportStatus"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "503": {"$ref": "#/components/responses/ImportAuthUnavailable"},
                },
            },
        },
        "/v1/sync-runs/{id}/retry": {
            "parameters": [{"$ref": "#/components/parameters/SyncRunID"}],
            "post": {
                "operationId": "retryInitialImport",
                "tags": ["sync-runs"],
                "summary": "Resume the same run from its safe checkpoint",
                "parameters": [
                    {"$ref": "#/components/parameters/IdempotencyKey"},
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RetryImportRequest"}
                        }
                    },
                },
                "responses": {
                    "202": {"$ref": "#/components/responses/SyncRunAccepted"},
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "409": {"$ref": "#/components/responses/RetryConflict"},
                    "422": {"description": "Missing or invalid Idempotency-Key or request body."},
                    "503": {"$ref": "#/components/responses/ImportAuthUnavailable"},
                },
            },
        },
    }


def _content_unit_ephemeral_openapi_path() -> dict[str, Any]:
    """Plan C §7 ephemeral raw content viewing contract (GET /v1/content-units/{content_unit_id}/content)."""
    return {
        "/v1/content-units/{content_unit_id}/content": {
            "get": {
                "operationId": "getEphemeralContentUnitContent",
                "tags": ["content-units"],
                "summary": "Ephemerally refetch and verify a persisted Content Unit (Plan C §7)",
                "description": (
                    "Refetches raw source content strictly on-demand from persisted provider IDs "
                    "(never accepting arbitrary URLs), verifies revision/unit SHA-256 hashes and "
                    "locators, and returns Cache-Control: private, no-store."
                ),
                "parameters": [
                    {
                        "name": "content_unit_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "name": "workspace_id",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "include_context",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean", "default": True},
                    },
                    {
                        "name": "X-Workspace-ID",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "responses": {
                    "200": {
                        "description": "Ephemeral verified Content Unit text and resolution metadata.",
                        "headers": {
                            "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/EphemeralContentUnitResponse"
                                }
                            }
                        },
                    },
                    "400": {
                        "description": "INVALID_REQUEST (e.g. arbitrary URL parameter supplied).",
                        "headers": {
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {
                        "description": "WORKSPACE_SCOPE_VIOLATION or SOURCE_POLICY_BLOCKED.",
                        "headers": {
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceResolutionErrorEnvelope"
                                }
                            }
                        },
                    },
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "409": {
                        "description": "SOURCE_REVISION_MISMATCH or SOURCE_LOCATOR_INVALID.",
                        "headers": {
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceResolutionErrorEnvelope"
                                }
                            }
                        },
                    },
                    "422": {
                        "description": "SOURCE_FETCH_LIMIT (operation limits exceeded or truncated response).",
                        "headers": {
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceResolutionErrorEnvelope"
                                }
                            }
                        },
                    },
                    "503": {
                        "description": "SOURCE_UNAVAILABLE or AUTH_VERIFICATION_UNAVAILABLE.",
                        "headers": {
                            "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                        },
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceResolutionErrorEnvelope"
                                }
                            }
                        },
                    },
                },
            }
        }
    }


def _github_connect_openapi_paths() -> dict[str, Any]:
    """M2-T201 (T-201 + T-201A) GitHub OAuth Connect, Allowlist & Policy Transition routes."""
    no_store_headers = {
        "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
        "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
    }
    return {
        "/v1/sources/github/oauth/start": {
            "post": {
                "operationId": "startGitHubOAuth",
                "tags": ["github-connect"],
                "summary": "Start minimum-scope (read:user) GitHub OAuth App connect flow",
                "parameters": [{"$ref": "#/components/parameters/RequestID"}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/GitHubOAuthStartRequest"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "One-time state and PKCE S256 authorize URL issued.",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/GitHubOAuthStartResponse"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "422": {"description": "Invalid request body."},
                },
            }
        },
        "/v1/sources/github/oauth/callback": {
            "post": {
                "operationId": "completeGitHubOAuthCallback",
                "tags": ["github-connect"],
                "summary": "Complete GitHub OAuth callback with scope enforcement and atomic persistence",
                "parameters": [
                    {"$ref": "#/components/parameters/IdempotencyKey"},
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/GitHubOAuthCallbackRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Connected SourceAccount (and optional allowlisted public SourceResource).",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/GitHubOAuthCallbackResponse"}
                            }
                        },
                    },
                    "400": {"description": "Invalid OAuth state, exchange error, or repository identifier."},
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"description": "EXCESSIVE_OAUTH_SCOPE, PRIVATE_REPOSITORY_NOT_ALLOWED, or workspace denial."},
                    "409": {"description": "IDEMPOTENCY_CONFLICT or state already consumed."},
                    "422": {"description": "Missing or invalid Idempotency-Key or request body."},
                },
            }
        },
        "/v1/source-resources": {
            "get": {
                "operationId": "listSourceResources",
                "tags": ["github-connect"],
                "summary": "List workspace-scoped non-deleted allowlisted source resources",
                "parameters": [
                    {
                        "name": "source_account_id",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "responses": {
                    "200": {
                        "description": "Workspace-scoped list of source resources with computed guidance codes.",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SourceResourceListResponse"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                },
            },
            "post": {
                "operationId": "registerSourceResource",
                "tags": ["github-connect"],
                "summary": "Register a single explicit public GitHub repository on the workspace allowlist",
                "parameters": [{"$ref": "#/components/parameters/RequestID"}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/SourceResourceRegisterRequest"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Verified PUBLIC repository registered under Plan C (raw_storage_mode=NONE).",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SourceResourceRecord"}
                            }
                        },
                    },
                    "400": {"description": "INVALID_REPOSITORY_NAME."},
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"description": "PRIVATE_REPOSITORY_NOT_ALLOWED or REAUTH_REQUIRED."},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "422": {"description": "Invalid request body."},
                },
            },
        },
        "/v1/source-accounts": {
            "get": {
                "operationId": "listSourceAccounts",
                "tags": ["github-connect"],
                "summary": "List workspace-scoped connected source accounts without secret references",
                "parameters": [{"$ref": "#/components/parameters/RequestID"}],
                "responses": {
                    "200": {
                        "description": "Connected source accounts (token_secret_ref strictly omitted).",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SourceAccountListResponse"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                },
            }
        },
        "/v1/source-resources/{source_resource_id}/verify-policy": {
            "post": {
                "operationId": "verifySourceResourcePolicy",
                "tags": ["github-connect"],
                "summary": "Re-verify repository visibility and token health and apply POLICY-01 transitions",
                "parameters": [
                    {
                        "name": "source_resource_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "responses": {
                    "200": {
                        "description": "Policy verification outcome (0 tombstones on visibility/permission transitions).",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/PolicyVerificationResponse"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                    "429": {"description": "GITHUB_RATE_LIMITED (retryable=true)."},
                    "503": {"description": "GITHUB_PROVIDER_UNAVAILABLE (retryable=true)."},
                },
            }
        },
        "/v1/source-accounts/{source_account_id}/disconnect": {
            "post": {
                "operationId": "disconnectSourceAccount",
                "tags": ["github-connect"],
                "summary": "Idempotently revoke GitHub grant, delete secret, and lock linked resources",
                "parameters": [
                    {
                        "name": "source_account_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {"$ref": "#/components/parameters/RequestID"},
                ],
                "responses": {
                    "200": {
                        "description": "Disconnected source account and canceled active jobs/runs.",
                        "headers": no_store_headers,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SourceAccountDisconnectResponse"}
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/ImportSessionRequired"},
                    "403": {"$ref": "#/components/responses/ImportWorkspaceAccessRevoked"},
                    "404": {"$ref": "#/components/responses/ImportResourceConcealed"},
                },
            }
        },
    }


def _shared_components() -> dict[str, Any]:
    """Authoritative OpenAPI components combining M0 import contract, Plan C §7, and M1 schemas."""
    # Extract Pydantic JSON schemas for contract models so FastAPI and OpenAPI stay in lockstep.
    pydantic_models = [
        ClaimDraftCreateRequest,
        ClaimEditRequest,
        ClaimApproveRequest,
        ClaimBoundaryResponse,
        DeletionPreviewResponse,
        DeletionConfirmRequest,
        DeletionConfirmAcceptedResponse,
        DeletionComponentReceiptItem,
        DeletionJobStatusResponse,
        QuotaMeterItem,
        QuotaStatusResponse,
        SearchResponse,
        WorkEventListResponse,
        WorkEventDetailResponse,
        ConfirmWorkEventRequest,
        EditWorkEventRequest,
        RejectWorkEventRequest,
        SplitWorkEventRequest,
        SplitWorkEventResponse,
        MergeWorkEventsRequest,
        MergeWorkEventsResponse,
        DissolveWorkEventRequest,
        DissolveWorkEventResponse,
        IdentityListResponse,
        ConfirmIdentityMappingRequest,
        ConfirmIdentityMappingResponse,
        RevokeIdentityMappingRequest,
        RevokeIdentityMappingResponse,
        FactListResponse,
        FactSupportsResponse,
        ApproveFactRequest,
        ApproveFactResponse,
        RejectFactRequest,
        RejectFactResponse,
        EditFactRequest,
        EditFactResponse,
        ClaimListResponse,
        ClaimDetailResponse,
        ClaimSupportsResponse,
        GenerateEventClaimsRequest,
        GenerateEventClaimsResponse,
        RevalidateClaimRequest,
        ApproveClaimActionRequest,
        RejectClaimActionRequest,
        ExportListResponse,
        CreateExportRequest,
        ExportDetailResponse,
        DeleteExportResponse,
        ErrorEnvelope,
    ]
    extracted_schemas: dict[str, Any] = {}
    for model_cls in pydantic_models:
        schema = model_cls.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = schema.pop("$defs", {})
        for def_name, def_schema in defs.items():
            extracted_schemas[def_name] = def_schema
        extracted_schemas[model_cls.__name__] = schema

    schemas: dict[str, Any] = {
        **extracted_schemas,
        "ClaimStatus": {
            "type": "string",
            "enum": [e.value for e in ClaimStatus],
        },
        "ClaimAtomType": {
            "type": "string",
            "enum": [e.value for e in ClaimAtomType],
        },
        "ClaimAtomStatus": {
            "type": "string",
            "enum": [e.value for e in ClaimAtomStatus],
        },
        "ClaimGenerationStatus": {
            "type": "string",
            "enum": [e.value for e in ClaimGenerationStatus],
        },
        "ExportFormat": {
            "type": "string",
            "enum": [e.value for e in ExportFormat],
        },
        "ExportPolicy": {
            "type": "string",
            "enum": [e.value for e in ExportPolicy],
        },
        "ExportStatus": {
            "type": "string",
            "enum": [e.value for e in ExportStatus],
        },
        "ExportVerificationClass": {
            "type": "string",
            "enum": [e.value for e in ExportVerificationClass],
        },
        "ClaimVerification": {
            "type": "string",
            "readOnly": True,
            "enum": [e.value for e in ClaimVerification],
        },
        "ContentUnitType": {
            "type": "string",
            "enum": [e.value for e in ContentUnitType],
        },
        "SearchEntityType": {
            "type": "string",
            "enum": [e.value for e in SearchEntityType],
        },
        "DeletionJobStatus": {
            "type": "string",
            "enum": [e.value for e in DeletionJobStatus],
        },
        "DeletionComponent": {
            "type": "string",
            "enum": [e.value for e in DeletionComponent],
        },
        "DeletionComponentStatus": {
            "type": "string",
            "enum": [e.value for e in DeletionComponentStatus],
        },
        "QuotaMetricKind": {
            "type": "string",
            "enum": [e.value for e in QuotaMetricKind],
        },
        "RuntimePauseState": {
            "type": "string",
            "enum": [e.value for e in RuntimePauseState],
        },
        "QuotaAdmissionState": {
            "type": "string",
            "enum": [e.value for e in QuotaAdmissionState],
        },
        "QuotaMeterStatus": {
            "type": "string",
            "enum": [e.value for e in QuotaMeterStatus],
        },
        "CircuitBreakerState": {
            "type": "string",
            "enum": [e.value for e in CircuitBreakerState],
        },
        "SyncRunMode": {
            "type": "string",
            "enum": [e.value for e in SyncRunMode],
        },
        "ImportState": {
            "type": "string",
            "enum": [e.value for e in SyncRunStatus],
        },
        "ProcessingStatus": {
            "type": "string",
            "enum": [e.value for e in ProcessingStatus],
        },
        "ResolutionStatus": {
            "type": "string",
            "enum": [e.value for e in ResolutionStatus],
        },
        "GitHubCredentialMode": {
            "type": "string",
            "enum": [e.value for e in GitHubCredentialMode],
        },
        "PolicyDecision": {
            "type": "string",
            "enum": [e.value for e in PolicyDecision],
        },
        "PolicyGuidanceCode": {
            "type": "string",
            "enum": [e.value for e in PolicyGuidanceCode],
        },
        "PolicyTransitionKind": {
            "type": "string",
            "enum": [e.value for e in PolicyTransitionKind],
        },
        "PolicyGuardedOperation": {
            "type": "string",
            "enum": [e.value for e in PolicyGuardedOperation],
        },
        "WorkEventAnchorMode": {
            "type": "string",
            "enum": [e.value for e in WorkEventAnchorMode],
        },
        "WorkEventStatus": {
            "type": "string",
            "enum": [e.value for e in WorkEventStatus],
        },
        "RecordOrigin": {
            "type": "string",
            "enum": [e.value for e in RecordOrigin],
        },
        "EventConstraintType": {
            "type": "string",
            "enum": [e.value for e in EventConstraintType],
        },
        "EventConstraintOrigin": {
            "type": "string",
            "enum": [e.value for e in EventConstraintOrigin],
        },
        "EventConstraintStatus": {
            "type": "string",
            "enum": [e.value for e in EventConstraintStatus],
        },
        "WorkEventPolicyState": {
            "type": "string",
            "enum": [e.value for e in WorkEventPolicyState],
        },
        "SourceProvider": {
            "type": "string",
            "enum": [e.value for e in SourceProvider],
        },
        "IdentityType": {
            "type": "string",
            "enum": [e.value for e in IdentityType],
        },
        "IdentityStatus": {
            "type": "string",
            "enum": [e.value for e in IdentityStatus],
        },
        "IdentityVerificationMethod": {
            "type": "string",
            "enum": [e.value for e in IdentityVerificationMethod],
        },
        "ExternalIdentityMappingStatus": {
            "type": "string",
            "enum": [e.value for e in ExternalIdentityMappingStatus],
        },
        "IdentityAttributionBlockReason": {
            "type": "string",
            "enum": [e.value for e in IdentityAttributionBlockReason],
        },
        "FactType": {
            "type": "string",
            "enum": [e.value for e in FactType],
        },
        "FactStatus": {
            "type": "string",
            "enum": [e.value for e in FactStatus],
        },
        "SupportType": {
            "type": "string",
            "enum": [e.value for e in SupportType],
        },
        "SupportValidator": {
            "type": "string",
            "enum": [e.value for e in SupportValidator],
        },
        "LinkStatus": {
            "type": "string",
            "enum": [e.value for e in LinkStatus],
        },
        "FactCompletionState": {
            "type": "string",
            "enum": [e.value for e in FactCompletionState],
        },
        "FactAttributionScope": {
            "type": "string",
            "enum": [e.value for e in FactAttributionScope],
        },
        "FactEnvironmentScope": {
            "type": "string",
            "enum": [e.value for e in FactEnvironmentScope],
        },
        "FactCausalBasis": {
            "type": "string",
            "enum": [e.value for e in FactCausalBasis],
        },
        "GitHubOAuthStartRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["return_path", "redirect_uri"],
            "properties": {
                "return_path": {"type": "string", "minLength": 1},
                "redirect_uri": {"type": "string", "minLength": 1},
                "ttl_seconds": {"type": "integer", "minimum": 60, "default": 600},
            },
        },
        "GitHubOAuthStartResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "provider",
                "credential_mode",
                "authorization_url",
                "requested_scopes",
                "state",
                "pkce_code_challenge",
                "return_path",
                "expires_at",
            ],
            "properties": {
                "provider": {"const": "GITHUB"},
                "credential_mode": {"$ref": "#/components/schemas/GitHubCredentialMode"},
                "authorization_url": {"type": "string", "format": "uri"},
                "requested_scopes": {"type": "array", "items": {"type": "string"}},
                "state": {"type": "string", "minLength": 1},
                "pkce_code_challenge": {"type": "string", "minLength": 1},
                "return_path": {"type": "string"},
                "expires_at": {"type": "string", "format": "date-time"},
            },
        },
        "GitHubOAuthCallbackRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["state", "redirect_uri"],
            "properties": {
                "state": {"type": "string", "minLength": 1},
                "code": {"type": ["string", "null"]},
                "redirect_uri": {"type": "string", "minLength": 1},
                "repository_full_name": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
                "policy_version": {"type": ["string", "null"]},
                "consent_version": {"type": ["string", "null"]},
            },
        },
        "SourceAccountView": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "id",
                "workspace_id",
                "provider",
                "external_account_id",
                "scopes",
                "status",
                "created_at",
                "revoked_at",
            ],
            "properties": {
                "id": {"type": "string"},
                "workspace_id": {"type": "string"},
                "provider": {"const": "GITHUB"},
                "external_account_id": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": ["ACTIVE", "REVOKED"]},
                "created_at": {"type": "string", "format": "date-time"},
                "revoked_at": {"type": ["string", "null"], "format": "date-time"},
            },
        },
        "SourceAccountListResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SourceAccountView"},
                }
            },
        },
        "SourceResourceRecord": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "id",
                "workspace_id",
                "source_account_id",
                "provider_resource_id",
                "resource_type",
                "name",
                "canonical_url",
                "visibility",
                "processing_mode",
                "raw_storage_mode",
                "raw_retention_seconds",
                "status",
                "policy_version",
                "consent_version",
                "last_visibility_checked_at",
                "created_at",
                "guidance_code",
            ],
            "properties": {
                "id": {"type": "string"},
                "workspace_id": {"type": "string"},
                "source_account_id": {"type": "string"},
                "provider_resource_id": {"type": "string"},
                "resource_type": {"const": "GITHUB_REPOSITORY"},
                "name": {"type": "string"},
                "canonical_url": {"type": "string", "format": "uri"},
                "visibility": {"type": "string", "enum": ["PUBLIC", "PRIVATE", "INTERNAL"]},
                "processing_mode": {
                    "type": "string",
                    "enum": ["PUBLIC_CLOUD", "PRIVATE_LOCAL", "PRIVATE_CLOUD_OPT_IN"],
                },
                "raw_storage_mode": {"const": "NONE"},
                "raw_retention_seconds": {"type": "null"},
                "status": {
                    "type": "string",
                    "enum": ["ACTIVE", "PAUSED", "POLICY_REVIEW_REQUIRED", "DELETED"],
                },
                "policy_version": {"type": "string"},
                "consent_version": {"type": ["string", "null"]},
                "last_visibility_checked_at": {"type": ["string", "null"], "format": "date-time"},
                "created_at": {"type": "string", "format": "date-time"},
                "guidance_code": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/PolicyGuidanceCode"},
                        {"type": "null"},
                    ]
                },
            },
        },
        "SourceResourceListResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SourceResourceRecord"},
                }
            },
        },
        "SourceResourceRegisterRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_account_id", "repository_full_name"],
            "properties": {
                "source_account_id": {"type": "string", "minLength": 1},
                "repository_full_name": {"type": "string", "minLength": 3},
                "policy_version": {"type": ["string", "null"]},
                "consent_version": {"type": ["string", "null"]},
            },
        },
        "GitHubOAuthCallbackResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_account", "source_resource", "return_path", "idempotent_replay"],
            "properties": {
                "source_account": {"$ref": "#/components/schemas/SourceAccountView"},
                "source_resource": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/SourceResourceRecord"},
                        {"type": "null"},
                    ]
                },
                "return_path": {"type": "string"},
                "idempotent_replay": {"type": "boolean"},
            },
        },
        "PolicyVerificationResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "resource",
                "account_status",
                "policy_decision",
                "guidance_code",
                "transition",
                "tombstone_created",
                "canceled_job_ids",
                "canceled_sync_run_ids",
                "blocked_operations",
                "checked_at",
            ],
            "properties": {
                "resource": {"$ref": "#/components/schemas/SourceResourceRecord"},
                "account_status": {"type": "string", "enum": ["ACTIVE", "REVOKED"]},
                "policy_decision": {"$ref": "#/components/schemas/PolicyDecision"},
                "guidance_code": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/PolicyGuidanceCode"},
                        {"type": "null"},
                    ]
                },
                "transition": {"$ref": "#/components/schemas/PolicyTransitionKind"},
                "tombstone_created": {"type": "boolean"},
                "canceled_job_ids": {"type": "array", "items": {"type": "string"}},
                "canceled_sync_run_ids": {"type": "array", "items": {"type": "string"}},
                "blocked_operations": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/PolicyGuardedOperation"},
                },
                "checked_at": {"type": "string", "format": "date-time"},
            },
        },
        "SourceAccountDisconnectResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "source_account",
                "provider_grant_revoked",
                "secret_deleted",
                "already_revoked",
                "affected_resource_ids",
                "canceled_job_ids",
                "canceled_sync_run_ids",
                "tombstone_created",
            ],
            "properties": {
                "source_account": {"$ref": "#/components/schemas/SourceAccountView"},
                "provider_grant_revoked": {"type": "boolean"},
                "secret_deleted": {"type": "boolean"},
                "already_revoked": {"type": "boolean"},
                "affected_resource_ids": {"type": "array", "items": {"type": "string"}},
                "canceled_job_ids": {"type": "array", "items": {"type": "string"}},
                "canceled_sync_run_ids": {"type": "array", "items": {"type": "string"}},
                "tombstone_created": {"type": "boolean"},
            },
        },
        "SourceResolutionErrorCode": {
            "type": "string",
            "enum": [
                "SOURCE_REVISION_MISMATCH",
                "SOURCE_LOCATOR_INVALID",
                "SOURCE_POLICY_BLOCKED",
                "SOURCE_UNAVAILABLE",
                "SOURCE_FETCH_LIMIT",
            ],
        },
        "InitialImportRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source_resource_id", "mode"],
            "properties": {
                "source_resource_id": {"type": "string", "minLength": 1},
                "mode": {"const": SyncRunMode.INITIAL_BACKFILL.value},
                "backfill_from": {"type": "string", "format": "date"},
                "limits": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"max_objects": {"type": "integer", "minimum": 1}},
                },
            },
        },
        "RetryImportRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["expected_run_version"],
            "properties": {
                "expected_run_version": {"type": "integer", "minimum": 1},
            },
        },
        "SyncRunAccepted": {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_id", "status_url"],
            "properties": {
                "job_id": {"type": "string"},
                "status": {"const": "PENDING"},
                "target_type": {"const": "SYNC_RUN"},
                "target_id": {"type": "string", "minLength": 1},
                "status_url": {"type": "string", "pattern": r"^/v1/sync-runs/[^/?#]+$"},
            },
        },
        "StreamProgress": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "status"],
            "properties": {
                "type": {"type": "string"},
                "status": {"type": "string"},
                "last_committed_page": {"type": "integer", "minimum": 0},
                "unresolved_failures": {"type": "integer", "minimum": 0},
            },
        },
        "ImportStatus": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "plan_id",
                "import_ready",
                "processing_status",
                "source_drift",
                "retry",
                "run_version",
            ],
            "properties": {
                "status": {"$ref": "#/components/schemas/ImportState"},
                "run_version": {"type": "integer", "minimum": 1},
                "plan_id": {"type": "string", "minLength": 1},
                "failure_reason": {"type": "string"},
                "streams": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/StreamProgress"},
                },
                "import_ready": {"type": "boolean"},
                "processing_status": {"$ref": "#/components/schemas/ProcessingStatus"},
                "observed_at": {"type": "string", "format": "date-time"},
                "source_drift": {"type": "boolean"},
                "processed_range": {"type": "string"},
                "remaining_range": {"type": "string"},
                "retry": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["allowed"],
                    "properties": {
                        "allowed": {"type": "boolean"},
                        "from_safe_checkpoint": {"const": True},
                    },
                },
            },
        },
        "RecoveredSyncRuns": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target_id", "status_url", "import_status"],
                        "properties": {
                            "target_id": {"type": "string", "minLength": 1},
                            "status_url": {
                                "type": "string",
                                "pattern": r"^/v1/sync-runs/[^/?#]+$",
                            },
                            "import_status": {"$ref": "#/components/schemas/ImportStatus"},
                        },
                    },
                }
            },
        },
        "ImportConflict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "retryable", "request_id"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": [
                                "IDEMPOTENCY_CONFLICT",
                                "SYNC_ALREADY_RUNNING",
                                "IDEMPOTENCY_KEY_EXPIRED",
                            ],
                        },
                        "message": {"type": "string"},
                        "retryable": {"const": False},
                        "request_id": {"type": "string"},
                        "details": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["existing_run_id"],
                            "properties": {"existing_run_id": {"type": "string"}},
                        },
                    },
                }
            },
        },
        "RetryVersionConflict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message", "retryable", "request_id"],
                    "properties": {
                        "code": {"const": "RESOURCE_VERSION_CONFLICT"},
                        "message": {"type": "string"},
                        "retryable": {"const": False},
                        "request_id": {"type": "string"},
                    },
                }
            },
        },
        "SourceResolutionSummary": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "checked_at",
                "expected_revision_hash",
                "observed_revision_hash",
                "retryable",
                "reason_code",
            ],
            "properties": {
                "status": {"$ref": "#/components/schemas/ResolutionStatus"},
                "checked_at": {"type": "string", "format": "date-time"},
                "expected_revision_hash": {"type": "string"},
                "observed_revision_hash": {"type": ["string", "null"]},
                "retryable": {"type": "boolean"},
                "reason_code": {"type": ["string", "null"]},
            },
        },
        "EphemeralContentUnitResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "content_unit_id",
                "unit_type",
                "content",
                "context",
                "source_resolution",
            ],
            "properties": {
                "content_unit_id": {"type": "string", "minLength": 1},
                "unit_type": {"$ref": "#/components/schemas/ContentUnitType"},
                "content": {"type": "string"},
                "context": {"type": ["object", "null"]},
                "source_resolution": {
                    "$ref": "#/components/schemas/SourceResolutionSummary"
                },
            },
        },
        "SourceResolutionErrorEnvelope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error", "source_resolution"],
            "properties": {
                "error": {"$ref": "#/components/schemas/ErrorBody"},
                "source_resolution": {
                    "$ref": "#/components/schemas/SourceResolutionSummary"
                },
            },
        },
    }

    return {
        "securitySchemes": {
            "SupabaseAccessJWT": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Supabase access JWT verified server-side with live session and "
                    "Workspace membership checks."
                ),
            }
        },
        "parameters": {
            "IdempotencyKey": {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "format": "uuid"},
            },
            "RequestID": {
                "name": "X-Request-ID",
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
            },
            "SyncRunID": {
                "name": "id",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "minLength": 1},
            },
        },
        "headers": {
            "RequestID": {
                "description": "Echo of the optional X-Request-ID.",
                "schema": {"type": "string"},
            },
            "PrivateNoStore": {
                "required": True,
                "description": "Enforce non-cached reads for current access and Plan C ephemeral content.",
                "schema": {"const": "private, no-store"},
            },
        },
        "responses": {
            "SyncRunAccepted": {
                "description": "Accepted for processing.",
                "headers": {"X-Request-ID": {"$ref": "#/components/headers/RequestID"}},
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/SyncRunAccepted"}
                    }
                },
            },
            "ImportSessionRequired": {
                "description": "SESSION_TERMINATED or unauthenticated request.",
                "headers": {
                    "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                    "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                    }
                },
            },
            "ImportWorkspaceAccessRevoked": {
                "description": "WORKSPACE_SCOPE_VIOLATION.",
                "headers": {
                    "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                    "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                    }
                },
            },
            "ImportResourceConcealed": {
                "description": "RESOURCE_NOT_FOUND (missing, out-of-workspace, or concealed).",
                "headers": {
                    "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                    "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                    }
                },
            },
            "ImportAuthUnavailable": {
                "description": "AUTH_VERIFICATION_UNAVAILABLE (retryable=true).",
                "headers": {
                    "X-Request-ID": {"$ref": "#/components/headers/RequestID"},
                    "Cache-Control": {"$ref": "#/components/headers/PrivateNoStore"},
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                    }
                },
            },
            "InitialImportConflict": {
                "description": "IDEMPOTENCY_CONFLICT, SYNC_ALREADY_RUNNING, or IDEMPOTENCY_KEY_EXPIRED.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ImportConflict"}
                    }
                },
            },
            "RetryConflict": {
                "description": "RESOURCE_VERSION_CONFLICT or ImportConflict.",
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                {"$ref": "#/components/schemas/ImportConflict"},
                                {"$ref": "#/components/schemas/RetryVersionConflict"},
                            ]
                        }
                    }
                },
            },
        },
        "schemas": schemas,
    }


def generate_integrated_openapi_spec() -> dict[str, Any]:
    """Build the deterministic M1 integrated OpenAPI 3.1.0 specification."""
    app = create_m1_contract_app()
    raw_spec = copy.deepcopy(app.openapi())

    paths: dict[str, Any] = {}
    # Keep FastAPI-generated /healthz, /v1/claims/*, /v1/search, and /v1/source-resources/*/deletion-*
    for path_key, path_item in raw_spec.get("paths", {}).items():
        paths[path_key] = path_item
    # Add M0-aligned 4 initial-import routes, Plan C §7 content-unit viewing route & M2-T201 GitHub connect routes
    paths.update(_sync_runs_openapi_paths())
    paths.update(_content_unit_ephemeral_openapi_path())
    paths.update(_github_connect_openapi_paths())

    components = _shared_components()
    # Merge any FastAPI-generated schemas (e.g. HTTPValidationError) without losing explicit ones
    for name, schema_def in raw_spec.get("components", {}).get("schemas", {}).items():
        if name not in components["schemas"]:
            components["schemas"][name] = schema_def

    # Normalize sorted path & schema keys for byte-level determinism
    sorted_paths = {k: paths[k] for k in sorted(paths.keys())}
    components["schemas"] = {
        k: components["schemas"][k] for k in sorted(components["schemas"].keys())
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": INTEGRATED_OPENAPI_TITLE,
            "version": INTEGRATED_OPENAPI_VERSION,
            "description": (
                "Integrated M1 Foundation API contract covering healthz, CONTRACT-06 initial import "
                "(4 routes aligned with contracts/import-v1/openapi.yaml), Plan C §7 ephemeral content "
                "viewing, read-only Claim verification write guard, OD-008 workspace search (zero "
                "project_id), and OD-003 source deletion preview/confirm. BYOK and external LLM "
                "subscription callback routes are intentionally absent."
            ),
        },
        "externalDocs": {
            "description": "Notion MVP API Specification & T-106",
            "url": "https://app.notion.com/p/3e3f86591b5f81c5b25ce48dd61263fa",
        },
        "security": [{"SupabaseAccessJWT": []}],
        "paths": sorted_paths,
        "components": components,
    }


def render_integrated_openapi_json() -> str:
    """Render the integrated M1 OpenAPI specification as canonical sorted JSON."""
    return render_canonical_json(generate_integrated_openapi_spec())


__all__ = [
    "FORBIDDEN_OPENAPI_PATH_FRAGMENTS",
    "INTEGRATED_OPENAPI_TITLE",
    "INTEGRATED_OPENAPI_VERSION",
    "generate_integrated_openapi_spec",
    "render_integrated_openapi_json",
]
