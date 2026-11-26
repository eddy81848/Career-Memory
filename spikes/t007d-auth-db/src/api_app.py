"""Synthetic Backend API (Resource Server) for T-007D verification.

Enforces:
1. Strict Bearer JWT verification (signature, iss, aud, exp, alg, synthetic revocation).
2. Authoritative server-side membership lookup via restricted `t007d_api_app` role.
   Never grants access from client-provided `workspace_id` or body fields alone.
3. Transaction-local `set_config('app.current_workspace_id', ..., true)` RLS scoping.
4. Consistent concealment (`404`) when querying out-of-scope vs nonexistent resource IDs.
5. Composite FK rejection (`404`/`422`) when attempting cross-workspace child creation.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from .auth_tokens import (
    SessionCheckUnavailableError,
    StrictJwtVerifier,
    SyntheticSessionLifecycleStore,
    TokenVerificationError,
    VerifiedPrincipal,
)
from .db import scoped_api_workspace_tx


class CreateProjectRequest(BaseModel):
    name: str
    # Optional spoofable field intentionally accepted to prove server NEVER trusts body workspace_id.
    workspace_id: UUID | None = None


class CreateArtifactRequest(BaseModel):
    project_id: UUID
    title: str
    payload_note: str = ""
    workspace_id: UUID | None = None


class SensitiveActionRequest(BaseModel):
    target_id: str = "synthetic-target"


def create_api_app(
    *,
    api_pool: ConnectionPool,
    verifier: StrictJwtVerifier,
    session_lifecycle: SyntheticSessionLifecycleStore | None = None,
) -> FastAPI:
    app = FastAPI(title="T-007D Synthetic Backend API")

    def _extract_bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={"code": "MISSING_BEARER", "message": "Authorization Bearer token required"},
            )
        return authorization.removeprefix("Bearer ").strip()

    def require_principal(authorization: str | None = Header(default=None)) -> VerifiedPrincipal:
        token = _extract_bearer(authorization)
        try:
            return verifier.verify(token)
        except TokenVerificationError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    def require_active_session_principal(
        authorization: str | None = Header(default=None),
    ) -> VerifiedPrincipal:
        """Fail-closed live session check for sensitive paths (approval/export/delete/source).

        Even if cryptographic JWT verification passes before `exp`, sensitive operations
        MUST be blocked when the current session is terminated OR cannot be confirmed.
        NOTE: HTTP status/detail codes here are spike-only observations, not finalized API contracts.
        """
        token = _extract_bearer(authorization)
        try:
            principal = verifier.verify_stateless(token)
        except TokenVerificationError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

        if session_lifecycle is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SESSION_STATUS_UNAVAILABLE",
                    "message": "Current session verifier is not configured (fail closed)",
                },
            )

        try:
            status = session_lifecycle.check_session_status(principal.session_id)
        except SessionCheckUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SESSION_STATUS_UNAVAILABLE",
                    "message": str(exc),
                },
            ) from exc

        if status == "terminated":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "SESSION_TERMINATED",
                    "message": "Session has been terminated; sensitive action blocked",
                },
            )
        if status != "active":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "SESSION_STATUS_UNKNOWN",
                    "message": "Current session could not be confirmed active (fail closed)",
                },
            )
        return principal

    @app.get("/api/v1/health")
    def health_check() -> dict:
        with api_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
                assert row is not None and row["ok"] == 1
        return {"status": "ok", "db": "reachable"}

    @app.get("/api/v1/workspaces/{workspace_id}/projects")
    def list_projects(
        workspace_id: UUID,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, workspace_id, name, created_by FROM app_t007d.projects ORDER BY id"
                    )
                    rows = cur.fetchall()
                return {
                    "workspace_id": str(membership["workspace_id"]),
                    "member_role": membership["member_role"],
                    "items": [
                        {
                            "id": str(r["id"]),
                            "workspace_id": str(r["workspace_id"]),
                            "name": r["name"],
                            "created_by": str(r["created_by"]),
                        }
                        for r in rows
                    ],
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    @app.post("/api/v1/workspaces/{workspace_id}/projects", status_code=201)
    def create_project(
        workspace_id: UUID,
        body: CreateProjectRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        if body.workspace_id is not None and body.workspace_id != workspace_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "WORKSPACE_MISMATCH",
                    "message": "Body workspace_id must not diverge from authorized workspace",
                },
            )
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                project_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app_t007d.projects (id, workspace_id, name, created_by)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id, workspace_id, name, created_by
                        """,
                        (
                            str(project_id),
                            str(membership["workspace_id"]),
                            body.name,
                            str(principal.subject_id),
                        ),
                    )
                    row = cur.fetchone()
                    assert row is not None
                return {
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "name": row["name"],
                    "created_by": str(row["created_by"]),
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    @app.get("/api/v1/workspaces/{workspace_id}/projects/{project_id}")
    def get_project_by_id(
        workspace_id: UUID,
        project_id: UUID,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, _membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, workspace_id, name
                          FROM app_t007d.projects
                         WHERE id = %s
                        """,
                        (str(project_id),),
                    )
                    row = cur.fetchone()
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

        # Consistent concealment: out-of-workspace ID and nonexistent ID both return 404.
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RESOURCE_NOT_FOUND", "message": "Project not found"},
            )
        return {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "name": row["name"],
        }

    @app.get("/api/v1/workspaces/{workspace_id}/projects/{project_id}/artifacts")
    def list_artifacts_via_parent(
        workspace_id: UUID,
        project_id: UUID,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, _membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT a.id, a.workspace_id, a.project_id, a.title
                          FROM app_t007d.artifacts a
                          JOIN app_t007d.projects p
                            ON p.workspace_id = a.workspace_id AND p.id = a.project_id
                         WHERE p.id = %s
                         ORDER BY a.id
                        """,
                        (str(project_id),),
                    )
                    rows = cur.fetchall()
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

        return {
            "items": [
                {
                    "id": str(r["id"]),
                    "workspace_id": str(r["workspace_id"]),
                    "project_id": str(r["project_id"]),
                    "title": r["title"],
                }
                for r in rows
            ]
        }

    @app.post("/api/v1/workspaces/{workspace_id}/artifacts", status_code=201)
    def create_artifact(
        workspace_id: UUID,
        body: CreateArtifactRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        if body.workspace_id is not None and body.workspace_id != workspace_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "WORKSPACE_MISMATCH",
                    "message": "Body workspace_id must not diverge from authorized workspace",
                },
            )
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                artifact_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title, payload_note)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, workspace_id, project_id, title
                        """,
                        (
                            str(artifact_id),
                            str(membership["workspace_id"]),
                            str(body.project_id),
                            body.title,
                            body.payload_note,
                        ),
                    )
                    row = cur.fetchone()
                    assert row is not None
                return {
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "project_id": str(row["project_id"]),
                    "title": row["title"],
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            # Composite FK rejects both cross-workspace parent IDs and nonexistent parent IDs identically.
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "PARENT_PROJECT_NOT_FOUND",
                    "message": "Referenced project does not exist in authorized workspace",
                },
            ) from exc

    @app.get("/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}")
    def get_artifact_by_id(
        workspace_id: UUID,
        artifact_id: UUID,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, _membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, workspace_id, project_id, title
                          FROM app_t007d.artifacts
                         WHERE id = %s
                        """,
                        (str(artifact_id),),
                    )
                    row = cur.fetchone()
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RESOURCE_NOT_FOUND", "message": "Artifact not found"},
            )
        return {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "project_id": str(row["project_id"]),
            "title": row["title"],
        }

    # =========================================================================
    # Sensitive Verification Paths (OD010-5 / OD010-6):
    #   Approval, Export, Delete, Source Connect / Revoke
    # Every path requires:
    #   1) Valid JWT signature/claims
    #   2) Confirmed ACTIVE live session (fail closed if terminated or unknown)
    #   3) Current active DB workspace membership + transaction-local RLS
    # =========================================================================

    @app.post("/api/v1/workspaces/{workspace_id}/approvals")
    def sensitive_approve(
        workspace_id: UUID,
        body: SensitiveActionRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_active_session_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (_conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                return {
                    "action": "approval",
                    "workspace_id": str(membership["workspace_id"]),
                    "target_id": body.target_id,
                    "status": "approved",
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    @app.post("/api/v1/workspaces/{workspace_id}/exports")
    def sensitive_export(
        workspace_id: UUID,
        body: SensitiveActionRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_active_session_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (_conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                return {
                    "action": "export",
                    "workspace_id": str(membership["workspace_id"]),
                    "target_id": body.target_id,
                    "status": "exported",
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    @app.delete("/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}")
    def sensitive_delete_artifact(
        workspace_id: UUID,
        artifact_id: UUID,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_active_session_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app_t007d.artifacts WHERE id = %s RETURNING id",
                        (str(artifact_id),),
                    )
                    deleted = cur.fetchone()
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

        if deleted is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RESOURCE_NOT_FOUND", "message": "Artifact not found"},
            )
        return {
            "action": "delete",
            "workspace_id": str(membership["workspace_id"]),
            "deleted_id": str(deleted["id"]),
        }

    @app.post("/api/v1/workspaces/{workspace_id}/sources/connect")
    def sensitive_source_connect(
        workspace_id: UUID,
        body: SensitiveActionRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_active_session_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (_conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                return {
                    "action": "source_connect",
                    "workspace_id": str(membership["workspace_id"]),
                    "target_id": body.target_id,
                    "status": "connected",
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    @app.post("/api/v1/workspaces/{workspace_id}/sources/revoke")
    def sensitive_source_revoke(
        workspace_id: UUID,
        body: SensitiveActionRequest,
        response: Response,
        principal: VerifiedPrincipal = Depends(require_active_session_principal),
    ) -> dict:
        try:
            with scoped_api_workspace_tx(
                api_pool,
                subject_id=principal.subject_id,
                requested_workspace_id=workspace_id,
            ) as (_conn, membership, pid):
                response.headers["X-T007D-Backend-PID"] = str(pid)
                return {
                    "action": "source_revoke",
                    "workspace_id": str(membership["workspace_id"]),
                    "target_id": body.target_id,
                    "status": "revoked",
                }
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "WORKSPACE_FORBIDDEN", "message": str(exc)},
            ) from exc

    return app
