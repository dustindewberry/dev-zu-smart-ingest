#!/usr/bin/env python3
"""Build gate check — prevents the zutec-smart-ingest 8-bug fingerprint from reappearing.

Run this as a CI step. Exits non-zero if any placeholder stubs, composition-root drift,
or protocol drift is detected.

See docs/BUILD_GATES.md for the full enumeration of the 8-bug fingerprint this script
guards against.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from zubot_ingestion.shared.constants import (
    BUILD_GATE_PROTECTED_FILES,
    PLACEHOLDER_SENTINEL_OVERWRITE,
    PLACEHOLDER_SENTINEL_OWNED_BY,
)


def main() -> int:
    errors: list[str] = []

    # Check 1: protected files are non-stub
    for rel in BUILD_GATE_PROTECTED_FILES:
        f = REPO_ROOT / rel
        if not f.exists():
            errors.append(f"[MISSING] {rel}")
            continue
        text = f.read_text()
        if PLACEHOLDER_SENTINEL_OVERWRITE in text:
            errors.append(
                f"[PLACEHOLDER SENTINEL] {rel} contains "
                f"'{PLACEHOLDER_SENTINEL_OVERWRITE}'"
            )
        if PLACEHOLDER_SENTINEL_OWNED_BY in text:
            errors.append(
                f"[PLACEHOLDER SENTINEL] {rel} contains "
                f"'{PLACEHOLDER_SENTINEL_OWNED_BY}'"
            )
        non_blank = [ln for ln in text.splitlines() if ln.strip()]
        if len(non_blank) < 4:
            errors.append(
                f"[STUB] {rel} has only {len(non_blank)} non-blank lines "
                f"(looks like a placeholder)"
            )

    # Check 2: composition root
    try:
        from zubot_ingestion.services import get_job_repository

        cm = get_job_repository()
        if not (hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")):
            errors.append(
                "[COMPOSITION DRIFT] get_job_repository() is not an async context manager"
            )
    except Exception as exc:
        errors.append(f"[COMPOSITION DRIFT] get_job_repository() raised: {exc}")

    # Check 3: protocol has update_job_result
    try:
        from zubot_ingestion.domain.protocols import IJobRepository

        if not hasattr(IJobRepository, "update_job_result"):
            errors.append("[PROTOCOL DRIFT] IJobRepository missing update_job_result")
    except Exception as exc:
        errors.append(f"[PROTOCOL DRIFT] could not import IJobRepository: {exc}")

    # Check 4: canonical modules importable
    canonical_imports = [
        ("zubot_ingestion.domain.pipeline.validation", "CompanionValidator"),
        ("zubot_ingestion.infrastructure.elasticsearch", "ElasticsearchSearchIndexer"),
        ("zubot_ingestion.infrastructure.callback", "CallbackHttpClient"),
        ("zubot_ingestion.api.routes.review", "router"),
    ]
    for mod, sym in canonical_imports:
        try:
            m = __import__(mod, fromlist=[sym])
            if not hasattr(m, sym):
                errors.append(f"[MISSING SYMBOL] {mod}.{sym}")
        except Exception as exc:
            errors.append(f"[IMPORT FAILED] {mod}: {exc}")

    if errors:
        print("BUILD GATE FAILURES:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("BUILD GATE CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
