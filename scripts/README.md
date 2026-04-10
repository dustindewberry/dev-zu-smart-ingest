# scripts/

Operational helpers for the Zubot Ingestion Service. These scripts
are standalone — they import from `zubot_ingestion.*` but do not
modify any runtime code, and they are safe to run against a live
deployment as long as Ollama / Postgres / Redis are already up.

## Files

### `bench.py` — end-to-end extraction benchmark

Measures the wall-clock performance of
`ExtractionOrchestrator.run_pipeline()` against a corpus of PDFs.
Invokes the pipeline through the same composition root
(`build_orchestrator()`) the Celery worker uses. Does not start or
stop Ollama; assumes it is already running at `OLLAMA_HOST`.

Basic usage:

```bash
python scripts/bench.py --corpus /data/drawings --iterations 3 --warmup 1
```

Writes a JSON report to `scripts/bench_results/bench_{timestamp}.json`
(directory is gitignored) and prints a human-readable summary to
stdout. See `docs/PERFORMANCE.md` for a full description of the CLI,
the environment variables that affect results, and the JSON report
schema.

### `regression_check.py` — benchmark regression gate

Compares a baseline bench report against a candidate report and fails
loudly on a performance regression. Owned by task-3 and expected to
live at `scripts/regression_check.py`. See that file's module
docstring for usage once it has landed.

### `lint-architecture.sh` — import-linter wrapper

Runs the `import-linter` layering rules that enforce the hexagonal
architecture (api > services > infrastructure > domain). Called from
CI; run locally before committing changes that touch module boundaries.

## Conventions

- Scripts are invoked via `python scripts/NAME.py ...` from the repo
  root. They insert `src/` onto `sys.path` at startup so the
  `zubot_ingestion` package is importable without an editable install.
- Scripts must not modify runtime code in `src/`. New helpers may be
  added; existing services, orchestrators, and Celery tasks must stay
  untouched.
- Scripts write artifacts to `scripts/` subdirectories which are
  gitignored (e.g. `scripts/bench_results/`).
