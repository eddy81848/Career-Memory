-- T-007D Synthetic Fixture Data (Workspace A & B, Users, Memberships, Parent/Child Rows)
-- Executed as postgres superuser or with explicit migrator bootstrap; never run by API/Worker.

INSERT INTO app_t007d.workspaces (id, slug, display_name) VALUES
    ('11111111-1111-4111-8111-111111111111', 'workspace-a', 'Synthetic Workspace A'),
    ('22222222-2222-4222-8222-222222222222', 'workspace-b', 'Synthetic Workspace B')
ON CONFLICT (id) DO NOTHING;

INSERT INTO app_t007d.users (subject_id, email) VALUES
    ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alice.a@synthetic.local'),
    ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'bob.b@synthetic.local'),
    ('cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'charlie.revoked@synthetic.local'),
    ('dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'dana.dual@synthetic.local')
ON CONFLICT (subject_id) DO NOTHING;

INSERT INTO app_t007d.workspace_memberships (workspace_id, subject_id, member_role, revoked_at) VALUES
    -- Alice: active owner in Workspace A only
    ('11111111-1111-4111-8111-111111111111', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'owner', NULL),
    -- Bob: active owner in Workspace B only
    ('22222222-2222-4222-8222-222222222222', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'owner', NULL),
    -- Charlie: revoked membership in Workspace A
    ('11111111-1111-4111-8111-111111111111', 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'member', now() - interval '1 hour'),
    -- Dana: active member in BOTH Workspace A and Workspace B (for dual-workspace switching tests)
    ('11111111-1111-4111-8111-111111111111', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'member', NULL),
    ('22222222-2222-4222-8222-222222222222', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'member', NULL)
ON CONFLICT (workspace_id, subject_id) DO NOTHING;

INSERT INTO app_t007d.projects (id, workspace_id, name, created_by) VALUES
    ('a1000000-0000-4000-8000-000000000001', '11111111-1111-4111-8111-111111111111', 'Alpha Repo A1', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),
    ('b2000000-0000-4000-8000-000000000001', '22222222-2222-4222-8222-222222222222', 'Beta Repo B1',  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
ON CONFLICT (id) DO NOTHING;

INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title, payload_note) VALUES
    ('a1000000-0000-4000-8000-000000000101', '11111111-1111-4111-8111-111111111111', 'a1000000-0000-4000-8000-000000000001', 'Artifact A1-1', 'synthetic-a-secret'),
    ('b2000000-0000-4000-8000-000000000201', '22222222-2222-4222-8222-222222222222', 'b2000000-0000-4000-8000-000000000001', 'Artifact B1-1', 'synthetic-b-secret')
ON CONFLICT (id) DO NOTHING;
