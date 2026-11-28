"""M5 Permission-Safe PostgreSQL FTS Search & Projection Indexer (T-501 / T-501A / OD-008)."""

from career_memory_api.search.indexer import PostgresSearchIndexer
from career_memory_api.search.models import (
    DEFAULT_SEARCH_CANDIDATE_LIMIT,
    MAX_SEARCH_TOKENS_LEN,
    MAX_TITLE_OR_SUMMARY_LEN,
    SearchIndexerStats,
    SearchRunRecord,
    SearchServiceConfig,
)
from career_memory_api.search.postgres_service import PostgresSearchService

__all__ = [
    "DEFAULT_SEARCH_CANDIDATE_LIMIT",
    "MAX_SEARCH_TOKENS_LEN",
    "MAX_TITLE_OR_SUMMARY_LEN",
    "PostgresSearchIndexer",
    "PostgresSearchService",
    "SearchIndexerStats",
    "SearchRunRecord",
    "SearchServiceConfig",
]
