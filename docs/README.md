# zubot_ingestion Developer Documentation

`zubot_ingestion` is a FastAPI + Celery microservice for automated metadata extraction from construction PDF drawings. It uses vision and text LLMs (via Ollama) to extract drawing numbers, titles, document types, and other metadata through a multi-stage pipeline.

## Documentation Index

### Getting Started

- [Quickstart Guide](guides/QUICKSTART.md) — Set up the development environment with Docker Compose or native Python
- [API Usage Guide](guides/API.md) — Happy-path workflows with curl examples, webhook callbacks, and error handling

### Architecture

- [Architecture Overview](architecture/ARCHITECTURE.md) — Hexagonal architecture, 3-stage extraction pipeline, mermaid diagrams, and how to extend the system

### Reference

- [Contracts & Protocols](reference/CONTRACTS.md) — Domain protocol interfaces, HTTP request/response schemas, and webhook callback format
- [Configuration Reference](reference/CONFIGURATION.md) — Comprehensive environment variable table with defaults and descriptions

### Operations

- [Deployment Guide](guides/DEPLOYMENT.md) — Docker Compose setup, T4 appliance overlay, service dependencies
- [Observability Guide](guides/OBSERVABILITY.md) — OpenTelemetry tracing, Prometheus metrics, and structured logging
- [Performance Tuning](PERFORMANCE.md) — Benchmark harness, regression checking, and tuning knobs

### Project

- [Contributing Guide](CONTRIBUTING.md) — Code style, architecture rules, testing conventions, and common patterns
- [MVP Boundary](MVP.md) — MVP status tracking and step definitions

## Quick Links

- API Docs (Swagger): `http://localhost:4243/docs`
- API Docs (ReDoc): `http://localhost:4243/redoc`
- Health Check: `http://localhost:4243/health`
- Metrics: `http://localhost:4243/metrics`
