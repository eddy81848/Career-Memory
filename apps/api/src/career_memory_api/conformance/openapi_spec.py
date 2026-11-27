"""Deterministic M1 Integrated OpenAPI 3.1.0 generator and invariant verifier.

Generates ``contracts/openapi/career-memory-v1.openapi.json`` while preserving
``contracts/import-v1/openapi.yaml`` (blob ``07594d75425744d6149250a9f7ff95ac24659f80``)
completely untouched and 100% compatible on the 4 initial-import routes.
"""

from __future__ import annotations

import copy
from typing import Any

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
from career_memory_api.resolver.models import ResolutionStatus
from career_memory_api.schema_contracts.enums import (
    ClaimStatus,
    ClaimVerification,
    ContentUnitType,
)
from career_memory_api.sync_runs.models import ProcessingStatus, SyncRunMode, SyncRunStatus

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
        SearchResponse,
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
    # Add M0-aligned 4 initial-import routes & Plan C §7 content-unit viewing route
    paths.update(_sync_runs_openapi_paths())
    paths.update(_content_unit_ephemeral_openapi_path())

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
