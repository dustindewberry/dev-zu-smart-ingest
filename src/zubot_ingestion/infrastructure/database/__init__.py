"""PostgreSQL persistence layer (CAP-004).

This package provides:
- SQLAlchemy 2.0 ORM models (models.py)
- Async engine / session management (session.py)
- IJobRepository implementation (repository.py)
"""

from zubot_ingestion.infrastructure.database.models import (
    Base,
    BatchORM,
    JobORM,
    ReviewActionORM,
)
from zubot_ingestion.infrastructure.database.repository import JobRepository
from zubot_ingestion.infrastructure.database.session import (
    create_engine,
    get_session,
    get_sessionmaker,
)

__all__ = [
    "Base",
    "BatchORM",
    "JobORM",
    "ReviewActionORM",
    "JobRepository",
    "create_engine",
    "get_session",
    "get_sessionmaker",
]
