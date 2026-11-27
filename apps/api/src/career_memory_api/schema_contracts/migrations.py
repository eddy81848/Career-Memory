"""SQL migration loader and sequential applier for M1-T102 & M1-T104."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from career_memory_api.schema_contracts.enums import SCHEMA_MIGRATION_FILES


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """Descriptor for an ordered SQL migration file."""

    version: str
    filename: str
    path: Path
    sha256: str


def default_migrations_dir() -> Path:
    """Resolve ``<repo_root>/db/migrations`` from this package location."""
    # .../apps/api/src/career_memory_api/schema_contracts/migrations.py -> parents[5] is repo root
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / "db" / "migrations"


def list_schema_migrations(
    migrations_dir: Path | None = None,
) -> tuple[MigrationFile, ...]:
    """Return ordered descriptors for the T-102 and T-104 SQL migrations."""
    base_dir = migrations_dir if migrations_dir is not None else default_migrations_dir()
    items: list[MigrationFile] = []
    for filename in SCHEMA_MIGRATION_FILES:
        file_path = base_dir / filename
        if not file_path.is_file():
            raise FileNotFoundError(f"Expected SQL migration file not found: {file_path}")
        raw_bytes = file_path.read_bytes()
        version = filename.split("_", 1)[0]
        digest = hashlib.sha256(raw_bytes).hexdigest()
        items.append(
            MigrationFile(
                version=version,
                filename=filename,
                path=file_path,
                sha256=digest,
            )
        )
    return tuple(items)


def load_migration_sql(
    version_or_filename: str, *, migrations_dir: Path | None = None
) -> str:
    """Load the UTF-8 SQL text of a migration by version prefix (e.g. '0020') or filename."""
    for item in list_schema_migrations(migrations_dir=migrations_dir):
        if version_or_filename in {item.version, item.filename}:
            return item.path.read_text(encoding="utf-8")
    raise KeyError(f"Unknown schema migration: {version_or_filename!r}")


def apply_schema_migrations(
    conn: Any,
    *,
    versions: Sequence[str] | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply ordered schema migrations onto a psycopg-compatible connection.

    Supports both fresh installs (``0020 -> 0030``) and staged upgrades
    (e.g. apply ``['0020']``, populate data, then apply ``['0030']`` or re-apply both).
    """
    available = list_schema_migrations(migrations_dir=migrations_dir)
    if versions is None:
        selected = list(available)
    else:
        wanted = set(versions)
        selected = [
            item
            for item in available
            if item.version in wanted or item.filename in wanted
        ]
        found_keys = {item.version for item in selected} | {item.filename for item in selected}
        missing = [v for v in versions if v not in found_keys]
        if missing:
            raise KeyError(f"Unknown migration version(s): {', '.join(missing)}")

    applied: list[str] = []
    for item in selected:
        sql_text = item.path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql_text)
        if not getattr(conn, "autocommit", False):
            conn.commit()
        applied.append(item.filename)
    return applied
