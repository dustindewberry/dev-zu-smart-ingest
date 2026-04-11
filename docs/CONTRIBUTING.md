# Contributing Guide

This document describes the conventions, architecture rules, and development
workflow for the `zubot_ingestion` service. Read this before submitting your
first change.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Code Style & Linting](#code-style--linting)
3. [Architecture Rules](#architecture-rules)
4. [Testing Conventions](#testing-conventions)
5. [Protocol Implementation Pattern](#protocol-implementation-pattern)
6. [Common Patterns](#common-patterns)

---

## Project Structure

```
src/zubot_ingestion/
  __init__.py
  __main__.py              # python -m zubot_ingestion entry point
  config.py                # Pydantic Settings (all env vars)
  api/
    app.py                 # FastAPI application factory
    middleware/             # Auth, rate limiting, request logging
    routes/                # HTTP endpoint handlers
  domain/
    entities.py            # Frozen dataclasses
    enums.py               # Business vocabulary enums
    protocols.py           # Interface definitions (Protocols)
    pipeline/
      extractors/          # Stage 1 extractors
      companion.py         # Stage 2 companion generator
      sidecar.py           # Stage 3 sidecar builder
      confidence.py        # Confidence calculator
      validation.py        # Companion validator
  infrastructure/
    database/              # PostgreSQL (SQLAlchemy ORM + repository)
    ollama/                # Ollama HTTP client
    pdf/                   # PyMuPDF processor
    chromadb/              # ChromaDB metadata writer
    elasticsearch/         # ES indexer
    callback/              # Webhook HTTP client
    metrics/               # Prometheus metrics
    logging/               # Structured logging (structlog)
    otel/                  # OpenTelemetry instrumentation
  services/
    __init__.py            # Composition root (DI factories)
    orchestrator.py        # ExtractionOrchestrator (pipeline)
    celery_app.py          # Celery app + tasks
  shared/
    constants.py           # All constants
    types.py               # Cross-layer DTOs and branded types
```

### Layer Responsibilities

| Layer | Purpose |
|---|---|
| `api/` | FastAPI routes, middleware (auth, rate limiting, logging). Thin HTTP translation layer. |
| `services/` | Application orchestration, Celery task definitions, composition root (DI factories). |
| `domain/` | Pure business logic: entities, enums, protocols, extraction pipeline stages. No I/O. |
| `infrastructure/` | Concrete adapters for external systems (Postgres, Ollama, PDF, ChromaDB, ES, OTEL, etc.). |
| `shared/` | Cross-layer constants, branded types (`JobId`, `BatchId`, `FileHash`), and DTOs. |

---

## Code Style & Linting

All tooling is configured in `pyproject.toml`. The project targets **Python 3.12+**.

### Formatter & Linter: Ruff

Ruff handles both formatting and linting in a single tool.

| Setting | Value |
|---|---|
| Line length | 100 |
| Target version | `py312` |
| Quote style | Double (`"`) |
| Indent style | Spaces |
| Line endings | LF |
| Docstring convention | Google |

**Enabled rule sets:**

| Code | Plugin |
|---|---|
| `E`, `W` | pycodestyle |
| `F` | pyflakes |
| `I` | isort (import sorting) |
| `B` | flake8-bugbear |
| `C4` | flake8-comprehensions |
| `UP` | pyupgrade |
| `N` | pep8-naming |
| `SIM` | flake8-simplify |
| `RUF` | ruff-specific rules |
| `PL` | pylint |
| `PT` | flake8-pytest-style |
| `TID` | flake8-tidy-imports |
| `ARG` | flake8-unused-arguments |
| `PTH` | flake8-use-pathlib |
| `ERA` | eradicate (commented-out code) |
| `PIE` | flake8-pie |
| `ASYNC` | flake8-async |
| `S` | flake8-bandit (security) |
| `TCH` | flake8-type-checking |

Import sorting treats `zubot_ingestion` as first-party and uses
`combine-as-imports = true`.

**Suppressed rules:**

- `E501` -- line length is handled by the formatter
- `PLR0913` -- many constructors legitimately take many arguments (DI)
- `PLR2004` -- magic values in comparisons are acceptable
- `S101` -- `assert` is fine in tests

Tests (`tests/**/*.py`) additionally suppress `ARG001` (unused fixture args),
`S105`/`S106` (hardcoded passwords in test fixtures).

### Type Checker: mypy (strict)

mypy runs in **strict mode** against `src/zubot_ingestion/`:

```
strict = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
no_implicit_optional = true
warn_return_any = true
warn_unreachable = true
```

Tests (`tests.*`) have relaxed rules: `disallow_untyped_defs = false`.

Third-party libraries without stubs (`celery`, `chromadb`, `fitz`, `pymupdf`,
`slowapi`, `json_repair`, `kombu`, `prometheus_client`) use
`ignore_missing_imports = true`.

### Commands

```bash
# Format all files
ruff format .

# Lint (check only)
ruff check .

# Lint (auto-fix safe violations)
ruff check --fix .

# Type check
mypy

# Run all checks together
ruff format . && ruff check . && mypy
```

---

## Architecture Rules

The codebase enforces a **layered hexagonal architecture** via
[import-linter](https://github.com/seddonym/import-linter). The contracts are
defined in `.importlinter` at the repository root.

### Contract 1: Layered Architecture

```
api > services > infrastructure > domain
```

Higher layers may import lower layers, **never** the reverse. `domain` is the
innermost layer and has zero outward dependencies (except `shared/`).

### Contract 2: Domain Purity

`domain` **cannot** import from `infrastructure`, `api`, or `services`.

Domain code depends only on:
- Python stdlib
- `zubot_ingestion.shared.types`
- `zubot_ingestion.domain.enums`

This keeps the domain layer free of I/O and framework dependencies.

### Contract 3: API Isolation

`api` **cannot** import `domain.pipeline` internals or `infrastructure`
directly.

The only legitimate cross-layer imports from `api` into `infrastructure` are
explicitly whitelisted in `.importlinter` via `ignore_imports` rules:

- `api.app` -> `infrastructure.logging.config` (structured logging setup)
- `api.app` -> `infrastructure.otel.instrumentation` (OTEL setup in lifespan)
- `api.routes.extract` -> `infrastructure.database.*` (composition root DI)
- `api.routes.health` -> `infrastructure.metrics.prometheus` (health probe)
- `api.routes.metrics` -> `infrastructure.metrics.prometheus` (Prometheus endpoint)

### Running the check

```bash
lint-imports
```

> **Violating these rules will fail CI.** import-linter also treats unused
> `ignore_imports` rules as a configuration error (non-zero exit). If you
> remove an import that was previously whitelisted, also remove its
> `ignore_imports` entry.

---

## Testing Conventions

### Framework

- **pytest** (>= 8.0) with **pytest-asyncio** in `auto` mode
- Async tests need **no marker** -- any `async def test_*` function runs
  automatically under asyncio.

### Test Location

```
tests/
  conftest.py             # Hermetic env vars (set BEFORE collection)
  unit/                   # Fast, isolated unit tests
  integration/            # Tests requiring real infrastructure
```

### Markers

| Marker | Meaning |
|---|---|
| `unit` | Fast, isolated -- no network, no disk I/O, no real databases |
| `integration` | Requires real infrastructure (Postgres, Redis, Ollama, etc.) |
| `e2e` | End-to-end: spins up the full service |
| `slow` | Takes longer than 1 second |

Markers are enforced with `--strict-markers` -- using an unregistered marker is
a hard error.

### Naming

- Test files: `test_<module>_<scenario>.py` or `test_<feature>.py`
- Test functions: `test_<unit>_<behavior>` (e.g. `test_confidence_calculator_satisfies_protocol`)
- Helper functions: `_make_<thing>()` or `_build_<thing>()` (prefixed with `_`)
- Test doubles: `Stub<Protocol>` or `Fake<Protocol>` classes defined at the top
  of each test file

### Hermetic conftest

`tests/conftest.py` sets environment variables **before** any test module is
collected. This prevents module-level singletons (slowapi `Limiter`,
`setup_logging`, OTEL `TracerProvider`) from hitting real infrastructure during
import:

```python
os.environ.setdefault("LOG_DIR", _LOG_TMP)          # writable tmpdir
os.environ.setdefault("REDIS_URL", "memory://")      # in-memory rate limiter
os.environ.setdefault("ZUBOT_INGESTION_API_KEY", "test-api-key")
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "")  # noop OTEL
```

If you add a new module-level singleton that reads from `Settings`, add the
corresponding `setdefault` to `tests/conftest.py`.

### Coverage

Branch coverage is enabled:

```toml
[tool.coverage.run]
branch = true
source = ["src/zubot_ingestion"]
```

Lines excluded from coverage:
- `pragma: no cover`
- `raise NotImplementedError`
- `if TYPE_CHECKING:`
- `if __name__ == "__main__":`

### Warnings

Warnings are treated as errors, except:
- `DeprecationWarning` (ignored)
- `PendingDeprecationWarning` (ignored)

### Running Tests

```bash
# Unit tests only
python -m pytest tests/unit/

# Integration tests only
python -m pytest tests/ -m integration

# All tests with coverage
python -m pytest tests/ --cov --cov-report=term-missing

# Single test file
python -m pytest tests/unit/test_confidence_calculator.py -v
```

---

## Protocol Implementation Pattern

All cross-layer interfaces are defined as `@runtime_checkable` Protocols in
`domain/protocols.py`. To add a new infrastructure adapter:

### Step 1: Define the protocol

Add a new `@runtime_checkable` Protocol class in `domain/protocols.py`:

```python
@runtime_checkable
class IMyAdapter(Protocol):
    """Describe what this adapter does."""

    async def do_thing(self, input: SomeType) -> ResultType:
        """Full docstring with Args/Returns/Raises."""
        ...
```

Add it to the `__all__` list in the same file.

### Step 2: Create the concrete implementation

Create the adapter in `infrastructure/<adapter_name>/`:

```
infrastructure/
  my_adapter/
    __init__.py         # Re-exports the concrete class
    client.py           # Concrete implementation
```

Implement **ALL** protocol methods with **MATCHING signatures** (parameter
names, types, and return types must match exactly).

### Step 3: Add a factory in the composition root

Add a factory function in `services/__init__.py`:

```python
def build_my_adapter(settings: Settings) -> IMyAdapter:
    from zubot_ingestion.infrastructure.my_adapter.client import MyAdapter
    return MyAdapter(base_url=settings.MY_ADAPTER_URL)
```

Use lazy imports to keep infrastructure off the import graph.

### Step 4: Wire into the orchestrator

Add the adapter as an **optional** keyword argument to
`ExtractionOrchestrator.__init__`:

```python
def __init__(
    self,
    ...,
    my_adapter: IMyAdapter | None = None,
):
    self._my_adapter = my_adapter
```

Use it inside `run_pipeline` with a try/except degrade guard:

```python
if self._my_adapter is not None:
    try:
        await self._my_adapter.do_thing(input)
    except Exception:
        logger.warning("my_adapter failed, degrading gracefully")
```

### Step 5: Add tests

Three tests are required:

1. **Unit test** for the adapter itself (`tests/unit/test_my_adapter.py`)
2. **Factory sanity test** in `tests/unit/test_composition_root_sanity.py` --
   call the factory with default settings and assert it returns without error
   and satisfies the protocol:
   ```python
   def test_build_my_adapter_returns_protocol_instance() -> None:
       settings = _make_settings()
       adapter = build_my_adapter(settings)
       assert isinstance(adapter, IMyAdapter)
   ```
3. **Pipeline wiring smoke test** -- verify the adapter is actually **invoked**
   during `run_pipeline`, not just constructed. Add assertions to
   `tests/unit/test_pipeline_wiring_smoke.py`.

> **Warning:** `@runtime_checkable` only checks method **names**, not
> signatures. A class that defines `do_thing(self, x: int)` will pass
> `isinstance(..., IMyAdapter)` even if the protocol declares
> `do_thing(self, input: SomeType)`. **Always manually verify signatures
> match.** mypy strict mode will catch mismatches at type-check time.

---

## Common Patterns

### Composition Root

All dependency wiring happens in `services/__init__.py` factory functions.
The two main factories are:

- `build_orchestrator()` -- constructs the full `ExtractionOrchestrator` with
  all stage extractors, pipeline components, and cross-cutting adapters.
- `get_job_repository()` -- async context manager that yields a `JobRepository`
  bound to a fresh database session.

Factories use **lazy imports** so that `import zubot_ingestion.services` does
not drag in every infrastructure adapter at module-load time.

### Degrade Gracefully

Cross-cutting adapters (metadata writer, search indexer, callback client,
companion validator) are **optional** constructor kwargs defaulting to `None`.
Inside `run_pipeline`, each is guarded by a try/except:

```python
if self._search_indexer is not None:
    try:
        await self._search_indexer.index_companion(...)
    except Exception:
        pipeline_trace["search_index"] = {"error": str(e)}
```

This means a missing or failing adapter never blocks the core extraction
pipeline.

### Two-Phase REVIEW Persist

When a job's confidence tier is `REVIEW`, the Celery task uses a two-phase
persist:

1. `update_job_result(...)` -- writes the JSONB blob **and** all indexed
   columns (`confidence_score`, `confidence_tier`, `processing_time_ms`,
   `otel_trace_id`, `pipeline_trace`), setting status to `COMPLETED`.
2. `update_job_status(status=REVIEW, result=None)` -- flips status to `REVIEW`
   **without** clobbering the indexed data, because `update_job_status` uses an
   `if result is not None` guard.

This pattern is load-bearing. Do not refactor `update_job_status` to
unconditionally write `None` to the result column.

### Branded Types

Use `NewType` branded types from `shared/types.py` for type safety:

```python
from zubot_ingestion.shared.types import JobId, BatchId, FileHash

job_id: JobId = JobId(uuid4())
file_hash: FileHash = FileHash(sha256_hex)
```

These are zero-cost at runtime but let mypy catch accidental mixing of
`UUID` values that represent different domain concepts.

### Frozen Dataclasses

All domain entities in `domain/entities.py` are `@dataclass(frozen=True)`.
To "update" an entity, use `dataclasses.replace()`:

```python
from dataclasses import replace

updated_job = replace(job, status=JobStatus.COMPLETED)
```

Never mutate entity fields directly -- frozen dataclasses raise
`FrozenInstanceError`.

### Settings Flow

`config.py` defines a Pydantic `Settings` class with `@lru_cache`-wrapped
`get_settings()`. The composition root calls `get_settings()` once and passes
the instance explicitly through the entire wired pipeline:

```python
settings = get_settings()
orchestrator = ExtractionOrchestrator(..., settings=settings)
```

This ensures env-var overrides (e.g. regression-check harnesses that call
`get_settings.cache_clear()`) propagate through every stage.
