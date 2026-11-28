"""Live 127.0.0.1 loopback server mounting the REAL career_memory_api WorkEvents FastAPI app + PG18.

Runs strictly against an ephemeral local PostgreSQL 18 cluster (migrations 0010..0060,
cm_api_app NOBYPASSRLS + FORCE RLS) and mounts the production `create_work_events_router`
backed by `PostgresWorkEventsReadService` and `PostgresWorkEventMutationService`.
Guarantees full PG18 cluster teardown on exit so zero postgres processes leak.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Request

REPO_ROOT = Path(__file__).resolve().parents[4]
API_ROOT = REPO_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT / "src"))
sys.path.insert(0, str(API_ROOT))

from career_memory_api.app import create_app  # noqa: E402
from career_memory_api.auth.postgres_guard import workspace_scoped_transaction  # noqa: E402
from career_memory_api.authz import (  # noqa: E402
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.schema_contracts.enums import (  # noqa: E402
    EventMembershipOrigin,
    EventMembershipRelationClass,
    PrAssociationState,
    SourceObjectType,
    WorkEventAnchorMode,
    WorkEventStatus,
)
from career_memory_api.work_events import (  # noqa: E402
    PostgresWorkEventMutationService,
    PostgresWorkEventsReadService,
    append_event_version_snapshot,
    create_work_events_router,
    record_source_relation,
)
from tests.backfill_engine.pg_harness import (  # noqa: E402
    MIGRATIONS_0010_TO_0050,
    EphemeralBackfillPg18Cluster,
    seed_workspace_repo_and_sync_run,
)

MIGRATION_0060 = REPO_ROOT / "db" / "migrations" / "0060_m3_event_versions_and_constraints.sql"
MIGRATIONS_0010_TO_0060 = (*MIGRATIONS_0010_TO_0050, MIGRATION_0060)


def _sha256(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


class LiveBackendState:
    def __init__(self, cluster: EphemeralBackfillPg18Cluster) -> None:
        self.cluster = cluster
        self.access: WorkspaceAccessContext | None = None
        self.seeded: dict[str, str] = {}

    def reset_and_seed(self) -> dict[str, str]:
        self.cluster.truncate_all()
        seed = seed_workspace_repo_and_sync_run(
            self.cluster, repo_name="acme-syn-lab/live-wire-repo"
        )
        ws_id = seed["workspace_id"]
        res_id = seed["source_resource_id"]
        user_id = seed["user_id"]
        access = WorkspaceAccessContext(
            user=AuthenticatedUser(
                user_id=user_id,
                auth_subject=AuthSubject(
                    issuer="https://auth.local/v1", subject=f"sub-{user_id}"
                ),
            ),
            membership=WorkspaceMembership(
                workspace_id=ws_id, user_id=user_id, role="OWNER"
            ),
        )
        self.access = access

        now = datetime.now(UTC)
        pr_obj_id = str(uuid.uuid4())
        dc_obj_id = str(uuid.uuid4())
        issue_obj_id = str(uuid.uuid4())
        ev_pr_id = str(uuid.uuid4())
        ev_dc_id = str(uuid.uuid4())

        with self.cluster.admin_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    for obj_id, obj_type, prov_id, assoc in (
                        (pr_obj_id, SourceObjectType.PR.value, "PR#104", None),
                        (
                            dc_obj_id,
                            SourceObjectType.COMMIT.value,
                            "a8f3c1e99900",
                            PrAssociationState.NO_MATCH_OBSERVED.value,
                        ),
                        (issue_obj_id, SourceObjectType.ISSUE.value, "ISSUE#41", None),
                    ):
                        cur.execute(
                            """
                            INSERT INTO public.source_objects (
                                id, workspace_id, source_resource_id, object_type,
                                provider_object_id, lifecycle_state, pr_association_state
                            )
                            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, 'ACTIVE', %s)
                            """,
                            (obj_id, ws_id, res_id, obj_type, prov_id, assoc),
                        )
                        cur.execute(
                            """
                            INSERT INTO public.source_object_revisions (
                                id, workspace_id, source_object_id, content_hash,
                                hash_policy_version, observed_at, provider_updated_at,
                                normalized_metadata, connector_version, schema_version
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, %s,
                                'v1', %s, %s, '{}'::jsonb, 'gh-v1', 'v1'
                            )
                            """,
                            (
                                str(uuid.uuid4()),
                                ws_id,
                                obj_id,
                                _sha256(f"{ws_id}:{prov_id}"),
                                now,
                                now,
                            ),
                        )

                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                            version, title, problem, action, outcome, summary_metadata,
                            status, created_by, occurred_start_at, occurred_end_at,
                            created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'PR',
                            1, 'PR #104: Live Backend Ledger Guard',
                            'Duplicate webhook hazard', 'Added transactional lock',
                            'Zero duplicate rows', %s::jsonb,
                            'CANDIDATE', 'RULE', %s, %s, %s, %s
                        )
                        """,
                        (
                            ev_pr_id,
                            ws_id,
                            pr_obj_id,
                            json.dumps(
                                {
                                    "source_resource_id": res_id,
                                    "review_flags": ["MIXED_PURPOSE_SIGNALS"],
                                },
                                sort_keys=True,
                            ),
                            now - timedelta(hours=3),
                            now - timedelta(hours=2),
                            now,
                            now,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.work_events (
                            id, workspace_id, primary_anchor_source_object_id, anchor_mode,
                            version, title, problem, action, outcome, summary_metadata,
                            status, created_by, occurred_start_at, occurred_end_at,
                            created_at, updated_at
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, 'DIRECT_COMMIT',
                            1, 'hotfix(ledger): rounding guard',
                            'Boundary drift', 'Direct default branch patch',
                            'All checks green', %s::jsonb,
                            'CANDIDATE', 'RULE', %s, %s, %s, %s
                        )
                        """,
                        (
                            ev_dc_id,
                            ws_id,
                            dc_obj_id,
                            json.dumps(
                                {
                                    "source_resource_id": res_id,
                                    "default_branch": "trunk",
                                    "abbreviated_sha": "a8f3c1e",
                                    "default_branch_reachable": True,
                                    "pr_association_state": "NO_MATCH_OBSERVED",
                                    "requires_user_confirmation": True,
                                    "review_flags": [
                                        "DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED"
                                    ],
                                },
                                sort_keys=True,
                            ),
                            now - timedelta(hours=1),
                            now - timedelta(hours=1),
                            now,
                            now,
                        ),
                    )

        with self.cluster.api_connection() as conn:
            with workspace_scoped_transaction(conn, access) as cur:
                record_source_relation(
                    cur,
                    workspace_id=ws_id,
                    event_id=ev_pr_id,
                    source_object_id=pr_obj_id,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                    relation_type="PRIMARY_ANCHOR",
                    origin=EventMembershipOrigin.SOURCE,
                )
                record_source_relation(
                    cur,
                    workspace_id=ws_id,
                    event_id=ev_pr_id,
                    source_object_id=issue_obj_id,
                    relation_class=EventMembershipRelationClass.CONTEXT_ONLY,
                    relation_type="ISSUE_REFERENCE",
                    origin=EventMembershipOrigin.SOURCE,
                )
                append_event_version_snapshot(
                    cur, workspace_id=ws_id, event_id=ev_pr_id, version=1
                )

                record_source_relation(
                    cur,
                    workspace_id=ws_id,
                    event_id=ev_dc_id,
                    source_object_id=dc_obj_id,
                    relation_class=EventMembershipRelationClass.MUST_LINK,
                    relation_type="PRIMARY_ANCHOR",
                    origin=EventMembershipOrigin.SOURCE,
                )
                append_event_version_snapshot(
                    cur, workspace_id=ws_id, event_id=ev_dc_id, version=1
                )

        self.seeded = {
            "workspace_id": ws_id,
            "source_resource_id": res_id,
            "pr_event_id": ev_pr_id,
            "dc_event_id": ev_dc_id,
        }
        return dict(self.seeded)


def bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


async def main() -> None:
    cluster = EphemeralBackfillPg18Cluster(prefix="cm-m3-live-fe-pg18-")
    cluster.start()
    try:
        for mig in MIGRATIONS_0010_TO_0060:
            cluster.apply_sql_file(mig)
        cluster.provision_runtime_logins()

        state = LiveBackendState(cluster)
        seeded = state.reset_and_seed()

        read_service = PostgresWorkEventsReadService(
            api_connection_factory=cluster.api_connection
        )
        mutation_service = PostgresWorkEventMutationService(
            api_connection_factory=cluster.api_connection
        )
        work_events_router = create_work_events_router(
            service=read_service,
            mutation_service=mutation_service,
            resolve_access=lambda _req: state.access,  # type: ignore[arg-type,return-value]
        )
        app = create_app(
            work_events_router=work_events_router,
            install_work_events_handlers=True,
        )

        @app.post("/__control/reset")
        async def control_reset() -> dict[str, Any]:
            return {"ok": True, **state.reset_and_seed()}

        @app.post("/__control/bump-version")
        async def control_bump_version(request: Request) -> dict[str, Any]:
            body = await request.json()
            ev_id = str(body["event_id"])
            new_status = str(body.get("status") or WorkEventStatus.CONFIRMED.value)
            with cluster.admin_connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE public.work_events
                            SET version = version + 1, status = %s, updated_at = now()
                            WHERE id = %s::uuid
                            RETURNING version, status
                            """,
                            (new_status, ev_id),
                        )
                        row = cur.fetchone()
                        assert row is not None
                        return {"ok": True, "version": int(row[0]), "status": str(row[1])}

        sock = bind_loopback_socket()
        port = sock.getsockname()[1]

        server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                access_log=False,
            )
        )
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        loop = asyncio.get_running_loop()

        def request_shutdown() -> None:
            server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                pass

        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)

        print(json.dumps({"baseUrl": f"http://127.0.0.1:{port}", **seeded}), flush=True)

        try:
            await task
        finally:
            sock.close()
    finally:
        cluster.stop()


if __name__ == "__main__":
    asyncio.run(main())
