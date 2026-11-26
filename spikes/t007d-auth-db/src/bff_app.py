"""Synthetic Web BFF (Browser -> Web Server -> Backend API) boundary for OD-010 verification.

What IS verified locally here:
- Browser holds ONLY an opaque `HttpOnly; SameSite=Lax` session cookie (`cm_session`),
  never the raw backend Bearer JWT in JSON responses or JS-accessible storage.
- Web BFF validates the cookie, resolves the server-side session record, and attaches
  `Authorization: Bearer <jwt>` only on the server-to-API hop.
- State-changing requests (`POST`) enforce BOTH:
  1. Strict `Origin` header allowlist match (`allowed_origin`), AND
  2. Synchronizer CSRF token (`X-CSRF-Token`) bound to the server session.
- Synthetic logout clears the cookie, removes the BFF session, and optionally notifies
  a synthetic revocation store (clearly separated from stateless JWT validity).

What is NOT verified locally (Design Candidate / Hosting Required):
- Real browser HTTPS + `Secure` / `__Host-` cookie enforcement across TLS termination.
- Supabase Auth PKCE OAuth/Magic-link callback redirect chain and SameSite behavior.
- Multi-tab refresh-token rotation races and Next.js middleware/RSC cookie propagation.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import httpx
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from .auth_tokens import SyntheticJwtAuthority, SyntheticRevocationStore

SESSION_COOKIE_NAME = "cm_session"


@dataclass
class BffSessionRecord:
    cookie_id: str
    subject_id: UUID
    csrf_token: str
    backend_jwt: str
    auth_session_id: str
    jti: str


class SyntheticLoginRequest(BaseModel):
    subject_id: UUID


class BffCreateProjectRequest(BaseModel):
    name: str
    workspace_id: UUID | None = None


def create_bff_app(
    *,
    backend_client_factory: Callable[[], httpx.Client],
    jwt_authority: SyntheticJwtAuthority,
    revocation_store: SyntheticRevocationStore | None = None,
    allowed_origin: str = "http://localhost:3000",
    cookie_secure: bool = False,
) -> FastAPI:
    """Create the synthetic Web BFF proxy app."""
    app = FastAPI(title="T-007D Synthetic Web BFF Proxy")
    sessions: dict[str, BffSessionRecord] = {}

    def _require_session(session_cookie: str | None) -> BffSessionRecord:
        if not session_cookie or session_cookie not in sessions:
            raise HTTPException(
                status_code=401,
                detail={"code": "UNAUTHENTICATED", "message": "Valid BFF session cookie required"},
            )
        return sessions[session_cookie]

    def _verify_csrf(request: Request, session: BffSessionRecord, csrf_header: str | None) -> None:
        origin = request.headers.get("origin")
        if origin != allowed_origin:
            raise HTTPException(
                status_code=403,
                detail={"code": "CSRF_ORIGIN_MISMATCH", "message": "Missing or disallowed Origin header"},
            )
        if not csrf_header or not secrets.compare_digest(csrf_header, session.csrf_token):
            raise HTTPException(
                status_code=403,
                detail={"code": "CSRF_TOKEN_INVALID", "message": "Missing or invalid X-CSRF-Token"},
            )

    @app.post("/bff/auth/synthetic-login")
    def synthetic_login(body: SyntheticLoginRequest, response: Response) -> dict:
        """Establish a synthetic BFF cookie session. Never returns raw Bearer JWT to browser."""
        cookie_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        backend_jwt, auth_session_id, jti = jwt_authority.issue_token(subject_id=body.subject_id)

        sessions[cookie_id] = BffSessionRecord(
            cookie_id=cookie_id,
            subject_id=body.subject_id,
            csrf_token=csrf_token,
            backend_jwt=backend_jwt,
            auth_session_id=auth_session_id,
            jti=jti,
        )
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_id,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            path="/",
        )
        return {
            "authenticated": True,
            "subject_id": str(body.subject_id),
            "csrf_token": csrf_token,
            # Deliberately NO access_token / refresh_token returned to browser.
        }

    @app.post("/bff/auth/logout")
    def synthetic_logout(
        request: Request,
        response: Response,
        cm_session: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
        propagate_revocation: bool = True,
    ) -> dict:
        session = _require_session(cm_session)
        _verify_csrf(request, session, x_csrf_token)

        sessions.pop(session.cookie_id, None)
        if propagate_revocation and revocation_store is not None:
            revocation_store.revoke_session(session.auth_session_id)

        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        return {
            "logged_out": True,
            "bff_session_deleted": True,
            "synthetic_revocation_propagated": bool(propagate_revocation and revocation_store is not None),
        }

    @app.get("/bff/workspaces/{workspace_id}/projects")
    def bff_list_projects(
        workspace_id: UUID,
        response: Response,
        cm_session: str | None = Cookie(default=None),
    ) -> dict:
        session = _require_session(cm_session)
        with backend_client_factory() as client:
            upstream = client.get(
                f"/api/v1/workspaces/{workspace_id}/projects",
                headers={"Authorization": f"Bearer {session.backend_jwt}"},
            )
        if "X-T007D-Backend-PID" in upstream.headers:
            response.headers["X-T007D-Backend-PID"] = upstream.headers["X-T007D-Backend-PID"]
        if upstream.status_code >= 400:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.json().get("detail"))
        return upstream.json()

    @app.post("/bff/workspaces/{workspace_id}/projects", status_code=201)
    def bff_create_project(
        workspace_id: UUID,
        body: BffCreateProjectRequest,
        request: Request,
        response: Response,
        cm_session: str | None = Cookie(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> dict:
        session = _require_session(cm_session)
        _verify_csrf(request, session, x_csrf_token)

        payload = {"name": body.name}
        if body.workspace_id is not None:
            payload["workspace_id"] = str(body.workspace_id)

        with backend_client_factory() as client:
            upstream = client.post(
                f"/api/v1/workspaces/{workspace_id}/projects",
                json=payload,
                headers={"Authorization": f"Bearer {session.backend_jwt}"},
            )
        if "X-T007D-Backend-PID" in upstream.headers:
            response.headers["X-T007D-Backend-PID"] = upstream.headers["X-T007D-Backend-PID"]
        if upstream.status_code >= 400:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.json().get("detail"))
        return upstream.json()

    # Test-only inspection helper to retrieve the server-stored JWT for proving
    # stateless JWT caveat after logout when revocation store is disabled.
    app.state._debug_sessions = sessions
    return app
