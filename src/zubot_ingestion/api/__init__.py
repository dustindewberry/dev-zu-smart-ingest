"""Presentation layer (FastAPI) for the Zubot Ingestion Service.

FastAPI application, routes, and HTTP-level middleware. This layer
depends on services (application layer) for business logic and is
the only layer that speaks HTTP. It must not import from infrastructure
or domain.pipeline directly.
"""
