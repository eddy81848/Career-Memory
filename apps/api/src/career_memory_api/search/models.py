"""Domain models and configuration for M5 permission-safe PostgreSQL FTS search (T-501/T-501A)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from career_memory_api.contracts_api.models import (
    DEFAULT_SEARCH_INDEX_VERSION,
    DEFAULT_SEARCH_RANKING_VERSION,
)

DEFAULT_SEARCH_CANDIDATE_LIMIT = 200
MAX_TITLE_OR_SUMMARY_LEN = 2000
MAX_SEARCH_TOKENS_LEN = 8000


@dataclass(frozen=True, slots=True)
class SearchIndexerStats:
    """Counts produced by a workspace or resource FTS index rebuild."""

    workspace_id: str
    index_version: str
    work_events_indexed: int
    facts_indexed: int
    claims_indexed: int
    deleted_entries: int


@dataclass(frozen=True, slots=True)
class SearchRunRecord:
    """Metadata-only persisted row from ``public.search_runs`` (SEARCH-04: zero raw query text)."""

    id: str
    workspace_id: str
    source_resource_id: str | None
    query_hash: str
    entity_types: tuple[str, ...]
    index_version: str
    ranking_version: str
    candidate_limit: int
    page_limit: int
    cursor_offset: int
    raw_candidate_count: int
    lag_filtered_count: int
    returned_count: int
    degraded_mode: bool
    run_metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SearchServiceConfig:
    """Version and capacity configuration for ``PostgresSearchService``."""

    index_version: str = DEFAULT_SEARCH_INDEX_VERSION
    ranking_version: str = DEFAULT_SEARCH_RANKING_VERSION
    candidate_limit: int = DEFAULT_SEARCH_CANDIDATE_LIMIT
    force_degraded_mode: bool = False


__all__ = [
    "DEFAULT_SEARCH_CANDIDATE_LIMIT",
    "MAX_SEARCH_TOKENS_LEN",
    "MAX_TITLE_OR_SUMMARY_LEN",
    "SearchIndexerStats",
    "SearchRunRecord",
    "SearchServiceConfig",
]
