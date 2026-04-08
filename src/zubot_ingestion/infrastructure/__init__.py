"""Infrastructure layer.

Concrete adapters for external systems: PDF processing, Ollama,
ChromaDB, Elasticsearch, webhooks, OTEL, and logging. This layer
implements protocol interfaces defined in the domain layer. It may
only import from domain.protocols, domain.entities, and shared.
"""
