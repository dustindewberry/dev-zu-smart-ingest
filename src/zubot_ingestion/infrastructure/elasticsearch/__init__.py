"""Elasticsearch adapter for full-text companion document indexing."""

from zubot_ingestion.infrastructure.elasticsearch.indexer import (
    ElasticsearchSearchIndexer,
    NoOpSearchIndexer,
    build_search_indexer,
)

__all__ = [
    "ElasticsearchSearchIndexer",
    "NoOpSearchIndexer",
    "build_search_indexer",
]
