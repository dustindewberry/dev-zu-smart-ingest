"""Shared constants for zubot-ingestion.

NOTE: This is a minimal stub providing only the symbols needed by the
database/repository module. The canonical, full-featured constants module
is produced by task-26 (shared-constants-and-config-keys) and is a strict
superset of this stub. On merge, the canonical version must overwrite
this file.
"""

from __future__ import annotations

# Service identifiers
SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"

# Database connection pool defaults (CAP-004)
DB_POOL_SIZE: int = 10
DB_MAX_OVERFLOW: int = 20
DB_POOL_TIMEOUT: int = 30
DB_POOL_RECYCLE: int = 3600

# Confidence tier thresholds (used by repository queries)
CONFIDENCE_TIER_AUTO: str = "auto"
CONFIDENCE_TIER_SPOT: str = "spot"
CONFIDENCE_TIER_REVIEW: str = "review"
