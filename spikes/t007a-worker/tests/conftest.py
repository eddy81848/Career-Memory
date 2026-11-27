"""Real-PostgreSQL fixtures. There is NO SQLite/stub fallback by design.

PG source, in order:
1. ``T007A_TEST_DSN`` — must point at a throwaway database (schema t007a_spike is dropped).
2. An ephemeral cluster created with ``initdb`` (found on PATH, ``$PG_BIN`` or Homebrew),
   listening on a private unix socket only (``listen_addresses=''``), deleted afterwards.
If neither is available every test is SKIPPED and reported as "real PG not run".
"""

import json
import os
import platform
import random
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from t007a.db import apply_schema, connect

SPIKE_DIR = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = Path(os.environ.get("T007A_EVIDENCE_PATH", SPIKE_DIR / "evidence" / "latest.json"))
_EVIDENCE: dict = {"cases": []}


def _find_pg_bin() -> Path | None:
    candidates = [os.environ.get("PG_BIN")]
    if shutil.which("initdb"):
        candidates.append(str(Path(shutil.which("initdb")).parent))
    candidates += [f"/opt/homebrew/opt/postgresql@{v}/bin" for v in (18, 17, 16)]
    candidates += ["/opt/homebrew/opt/postgresql/bin", "/usr/lib/postgresql/16/bin"]
    for c in candidates:
        if c and (Path(c) / "initdb").exists() and (Path(c) / "pg_ctl").exists():
            return Path(c)
    return None


@pytest.fixture(scope="session")
def pg_dsn():
    external = os.environ.get("T007A_TEST_DSN")
    if external:
        _EVIDENCE["pg_source"] = "external T007A_TEST_DSN (throwaway DB)"
        yield external
        return
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("REAL PostgreSQL NOT AVAILABLE: no initdb and no T007A_TEST_DSN; not substituted")
    root = Path(tempfile.mkdtemp(prefix="t7a-pg-", dir="/tmp"))  # short path for unix socket
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    port = random.randint(56000, 56999)
    subprocess.run(
        [pg_bin / "initdb", "-D", data, "-U", "t007a", "--auth=trust", "-E", "UTF8",
         "--locale=C", "--no-sync", "--no-instructions"],
        check=True, capture_output=True,
    )
    opts = f"-p {port} -k {sock} -c listen_addresses='' -c fsync=off -c max_connections=60"
    subprocess.run(
        [pg_bin / "pg_ctl", "-D", data, "-o", opts, "-l", root / "pg.log", "-w", "start"],
        check=True, capture_output=True,
    )
    dsn = f"host={sock} port={port} user=t007a dbname=postgres"
    _EVIDENCE["pg_source"] = f"ephemeral initdb cluster ({pg_bin}), unix socket only, deleted after run"
    try:
        yield dsn
    finally:
        subprocess.run([pg_bin / "pg_ctl", "-D", data, "-m", "fast", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _evidence_file(request):
    yield
    if not _EVIDENCE["cases"]:
        return
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _EVIDENCE["python"] = platform.python_version()
    _EVIDENCE["psycopg"] = psycopg.__version__
    EVIDENCE_PATH.write_text(json.dumps(_EVIDENCE, indent=2, default=str) + "\n")


@pytest.fixture
def db(pg_dsn):
    """Fresh t007a_spike schema per test."""
    apply_schema(pg_dsn)
    if "server_version" not in _EVIDENCE:
        with connect(pg_dsn, "t007a-observer") as conn:
            _EVIDENCE["server_version"] = conn.execute("SHOW server_version").fetchone()[
                "server_version"
            ]
    return pg_dsn


@pytest.fixture
def evidence(request):
    case = {"case": request.node.name, "phases": {}}
    _EVIDENCE["cases"].append(case)

    def record(phase: str, data):
        case["phases"][phase] = data
        return data

    return record
