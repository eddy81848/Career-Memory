"""Real local PostgreSQL 18 integration tests for M1-T103B (RLS, immutability, TOCTOU, REF-04)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.auth.jwt_verifier import (
    JwkKey,
    JwksSnapshot,
    JwtAccessTokenVerifier,
    JwtVerificationConfig,
)
from career_memory_api.auth.postgres_guard import (
    PostgresWorkspaceAccessProvider,
    workspace_scoped_transaction,
)
from career_memory_api.authz import AuthenticatedUser, AuthSubject, WorkspaceAccessContext, WorkspaceMembership
from career_memory_api.canonicalization import (
    CANONICALIZATION_VERSION,
    PARSER_VERSION,
    SourceObjectKind,
    canonicalize_source_bytes,
    parse_content_units,
)
from career_memory_api.resolver import (
    GateOperationKind,
    InMemoryProviderContentFetcher,
    PostgresProvenanceMetadataStore,
    RefetchRequestInput,
    RefetchResolverService,
    ResolutionStatus,
    mount_resolver_routes,
    verify_required_supports_for_gate,
)
from tests.auth.crypto_helpers import Es256TestKeyPair
from tests.resolver.pg_fixtures import EphemeralResolverPg18Cluster, run_ephemeral_pg18_cluster

pytestmark = pytest.mark.postgres

_ISSUER = "https://auth.career-memory.local/auth/v1"
_AUDIENCE = "authenticated"
_CANARY_PG = "CANARY_PG18_RAW_TEXT_ZERO_PERSISTENCE_7B9C1E"
_FIXED_NOW = datetime(2026, 11, 27, 15, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralResolverPg18Cluster]:
    yield from run_ephemeral_pg18_cluster()


def _seed_workspace_and_units(
    admin_dsn: str,
    *,
    user_id: str,
    auth_subject: str,
    workspace_id: str,
    raw_bytes: bytes,
    visibility: str = "PUBLIC",
    resource_status: str = "ACTIVE",
) -> dict[str, str]:
    canon = canonicalize_source_bytes(raw_bytes)
    assert canon.status == "CANONICALIZED" and canon.document is not None
    parsed = parse_content_units(canon.document, SourceObjectKind.PR_BODY)
    assert len(parsed.persisted_metadata) >= 2

    account_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
    object_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    unit_ids: list[str] = []

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.users (id, auth_issuer, auth_subject, email, name)
                VALUES (%s::uuid, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, _ISSUER, auth_subject, f"{auth_subject}@local.test", auth_subject),
            )
            cur.execute(
                """
                INSERT INTO public.workspaces (id, owner_user_id, name)
                VALUES (%s::uuid, %s::uuid, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (workspace_id, user_id, f"ws-{workspace_id[:8]}"),
            )
            cur.execute(
                """
                INSERT INTO public.workspace_memberships (workspace_id, user_id, role)
                VALUES (%s::uuid, %s::uuid, 'OWNER')
                ON CONFLICT (workspace_id, user_id) DO NOTHING
                """,
                (workspace_id, user_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_accounts
                    (id, workspace_id, provider, external_account_id, token_secret_ref, scopes, status)
                VALUES (%s::uuid, %s::uuid, 'GITHUB', %s, %s, ARRAY['public_repo'], 'ACTIVE')
                """,
                (
                    account_id,
                    workspace_id,
                    f"gh-{workspace_id[:8]}",
                    f"secret://github/{workspace_id[:8]}/tok",
                ),
            )
            cur.execute(
                """
                INSERT INTO public.source_resources
                    (id, workspace_id, source_account_id, provider_resource_id, resource_type,
                     name, canonical_url, visibility, processing_mode, raw_storage_mode,
                     raw_retention_seconds, status, policy_version)
                VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s, 'GITHUB_REPOSITORY',
                     'acme/pg-service', 'https://github.com/acme/pg-service', %s, 'PUBLIC_CLOUD',
                     'NONE', NULL, %s, 'policy-v1')
                """,
                (
                    resource_id,
                    workspace_id,
                    account_id,
                    f"repo-{workspace_id[:8]}",
                    visibility,
                    resource_status,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.source_objects
                    (id, workspace_id, source_resource_id, object_type, provider_object_id, lifecycle_state)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'PR', '101', 'ACTIVE')
                """,
                (object_id, workspace_id, resource_id),
            )
            cur.execute(
                """
                INSERT INTO public.source_object_revisions
                    (id, workspace_id, source_object_id, provider_revision_id, content_hash,
                     hash_policy_version, normalized_metadata, connector_version, schema_version)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'etag-pg-1', %s, %s, %s::jsonb, 'conn-v1', 'schema-v1')
                """,
                (
                    revision_id,
                    workspace_id,
                    object_id,
                    canon.document.metadata.content_sha256,
                    CANONICALIZATION_VERSION,
                    json.dumps({"source_kind": "PR_BODY"}),
                ),
            )
            for idx, meta in enumerate(parsed.persisted_metadata):
                cu_id = str(uuid.uuid4())
                unit_ids.append(cu_id)
                anchor = meta.to_persisted_anchor_dict()
                anchor.pop("text_content", None)
                anchor.pop("content_ref", None)
                anchor.pop("retention_class", None)
                cur.execute(
                    """
                    INSERT INTO public.content_units
                        (id, workspace_id, source_object_revision_id, unit_type, anchor,
                         text_content, content_ref, content_hash, canonicalization_version,
                         retention_class, expires_at, ordinal, policy_state)
                    VALUES
                        (%s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb,
                         NULL, NULL, %s, %s, 'NONE', NULL, %s, 'ACTIVE')
                    """,
                    (
                        cu_id,
                        workspace_id,
                        revision_id,
                        meta.unit_type.value,
                        json.dumps(anchor),
                        meta.locator.span_sha256,
                        CANONICALIZATION_VERSION,
                        idx,
                    ),
                )

    return {
        "account_id": account_id,
        "resource_id": resource_id,
        "object_id": object_id,
        "revision_id": revision_id,
        "unit_0_id": unit_ids[0],
        "unit_1_id": unit_ids[1],
        "expected_revision_hash": canon.document.metadata.content_sha256,
    }


def _dump_entire_public_schema(admin_dsn: str) -> str:
    chunks: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                cur.execute(f"SELECT row_to_json(t)::text FROM public.{table} AS t")
                for (row_json,) in cur.fetchall():
                    chunks.append(f"{table}:{row_json}")
    return "\n".join(chunks)


def test_pg18_end_to_end_rls_concealment_canary_audit_and_immutability(
    pg18_cluster: EphemeralResolverPg18Cluster,
    caplog: Any,
) -> None:
    raw = (
        f"# Summary\nContext header.\n\n"
        f"## Implementation\nPG18 verified {_CANARY_PG} body.\n\n"
        f"## Caveats\nOnly staging verified.\n"
    ).encode("utf-8")

    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    ws_alpha = str(uuid.uuid4())
    ws_beta = str(uuid.uuid4())

    seeded_alpha = _seed_workspace_and_units(
        pg18_cluster.admin_dsn,
        user_id=alice_id,
        auth_subject="sub-pg-alice",
        workspace_id=ws_alpha,
        raw_bytes=raw,
    )
    _seed_workspace_and_units(
        pg18_cluster.admin_dsn,
        user_id=bob_id,
        auth_subject="sub-pg-bob",
        workspace_id=ws_beta,
        raw_bytes=raw,
    )

    keypair = Es256TestKeyPair.generate(kid="pg-kid-1")

    class _StaticJwksProvider:
        def get_jwks(self, *, force_refresh: bool = False) -> JwksSnapshot:
            return JwksSnapshot(
                keys=(JwkKey.from_dict(keypair.to_jwk()),),
                fetched_at=_FIXED_NOW,
                expires_at=_FIXED_NOW + timedelta(minutes=30),
            )

    verifier = JwtAccessTokenVerifier(
        config=JwtVerificationConfig(
            expected_issuer=_ISSUER,
            expected_audience=_AUDIENCE,
        ),
        jwks_provider=_StaticJwksProvider(),
        clock=lambda: _FIXED_NOW,
    )
    ts = int(_FIXED_NOW.timestamp())
    alice_jwt = keypair.sign_jwt(
        {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": "sub-pg-alice",
            "exp": ts + 600,
            "iat": ts - 5,
        }
    )
    bob_jwt = keypair.sign_jwt(
        {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": "sub-pg-bob",
            "exp": ts + 600,
            "iat": ts - 5,
        }
    )

    assert not hasattr(PostgresProvenanceMetadataStore, "get_by_id")

    with psycopg.connect(pg18_cluster.api_dsn) as api_conn:
        auth_provider = PostgresWorkspaceAccessProvider(api_conn, verifier)
        pg_store = PostgresProvenanceMetadataStore(api_conn)
        fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
        resolver = RefetchResolverService(
            metadata_store=pg_store,
            fetcher=fetcher,
            clock=lambda: _FIXED_NOW,
        )
        app = create_app(access_provider=auth_provider)
        mount_resolver_routes(app, resolver=resolver)

        with caplog.at_level(logging.DEBUG):
            with TestClient(app) as client:
                # 1) Authorized Alice resolves her unit -> 200 OK + no-store
                resp_ok = client.get(
                    f"/v1/content-units/{seeded_alpha['unit_1_id']}/content",
                    params={"workspace_id": ws_alpha},
                    headers={"Authorization": f"Bearer {alice_jwt}"},
                )
                assert resp_ok.status_code == 200
                assert resp_ok.headers["cache-control"] == "private, no-store"
                assert _CANARY_PG in resp_ok.json()["content"]
                assert resp_ok.json()["source_resolution"]["status"] == "MATCHED"

                # 2) Bob in ws_beta requests Alice's unit -> 404 concealed identically under FORCE RLS
                resp_xws = client.get(
                    f"/v1/content-units/{seeded_alpha['unit_1_id']}/content",
                    params={"workspace_id": ws_beta},
                    headers={"Authorization": f"Bearer {bob_jwt}", "X-Request-ID": "req-pg-xws"},
                )
                resp_missing = client.get(
                    f"/v1/content-units/{uuid.uuid4()}/content",
                    params={"workspace_id": ws_beta},
                    headers={"Authorization": f"Bearer {bob_jwt}", "X-Request-ID": "req-pg-xws"},
                )
                assert resp_xws.status_code == 404
                assert resp_missing.status_code == 404
                assert resp_xws.json() == resp_missing.json()
                assert resp_xws.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 3) REF-04 Full PostgreSQL table dump + log scan: ZERO canary occurrences
    db_dump = _dump_entire_public_schema(pg18_cluster.admin_dsn)
    assert _CANARY_PG not in db_dump
    assert _CANARY_PG not in "\n".join(rec.getMessage() for rec in caplog.records)

    # 4) Verify last_visibility_checked_at was updated (non-raw metadata) and content_units.text_content IS NULL
    with psycopg.connect(pg18_cluster.admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_visibility_checked_at FROM public.source_resources WHERE id = %s::uuid",
                (seeded_alpha["resource_id"],),
            )
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT text_content, content_ref, retention_class FROM public.content_units WHERE id = %s::uuid",
                (seeded_alpha["unit_1_id"],),
            )
            assert cur.fetchone() == (None, None, "NONE")

            # 5) DB CHECK constraint actively rejects forbidden raw key smuggling
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "UPDATE public.content_units SET anchor = anchor || '{\"raw_text\": \"leak\"}'::jsonb WHERE id = %s::uuid",
                    (seeded_alpha["unit_1_id"],),
                )

            # 6) DATA-01 immutability trigger rejects revision UPDATE
            with pytest.raises(psycopg.errors.IntegrityError):
                cur.execute(
                    "UPDATE public.source_object_revisions SET content_hash = %s WHERE id = %s::uuid",
                    ("sha256:" + "0" * 64, seeded_alpha["revision_id"]),
                )


def test_pg18_toctou_blocks_when_resource_becomes_private_between_refetch_and_commit(
    pg18_cluster: EphemeralResolverPg18Cluster,
) -> None:
    raw = b"# Title\nIntro\n\n## Action\nValid public unit body.\n"
    user_id = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    seeded = _seed_workspace_and_units(
        pg18_cluster.admin_dsn,
        user_id=user_id,
        auth_subject="sub-pg-toctou",
        workspace_id=ws_id,
        raw_bytes=raw,
    )
    access = WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer=_ISSUER, subject="sub-pg-toctou"),
        ),
        membership=WorkspaceMembership(workspace_id=ws_id, user_id=user_id, role="OWNER"),
    )

    with psycopg.connect(pg18_cluster.api_dsn) as api_conn:
        pg_store = PostgresProvenanceMetadataStore(api_conn)
        fetcher = InMemoryProviderContentFetcher.from_single_bytes(raw)
        resolver = RefetchResolverService(
            metadata_store=pg_store,
            fetcher=fetcher,
            clock=lambda: _FIXED_NOW,
        )

        with resolver.open_operation_session(access, operation_id="op-pg-toctou") as session:
            # Refetch succeeds while resource is PUBLIC
            with resolver.resolve_unit(
                session,
                RefetchRequestInput(
                    access=access,
                    content_unit_id=seeded["unit_1_id"],
                    operation_id="op-pg-toctou",
                ),
            ) as res:
                assert res.metadata.status is ResolutionStatus.MATCHED

            # Concurrent change: resource transitions to PRIVATE in Postgres BEFORE commit
            with psycopg.connect(pg18_cluster.admin_dsn, autocommit=True) as admin_conn:
                with admin_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.source_resources SET visibility = 'PRIVATE' WHERE id = %s::uuid",
                        (seeded["resource_id"],),
                    )

            # Gate re-checks live DB policy fence before commit -> BLOCKED!
            gate = verify_required_supports_for_gate(
                session,
                [seeded["unit_1_id"]],
                operation_kind=GateOperationKind.APPROVAL,
                resolver=resolver,
            )
            assert gate.allowed is False
            assert gate.error is not None
            assert gate.error.http_status == 403
            assert gate.error.code == "SOURCE_POLICY_BLOCKED"
            assert gate.reason_code == "TOCTOU_POLICY_CHANGED_BEFORE_COMMIT"
