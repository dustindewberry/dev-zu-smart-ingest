#!/usr/bin/env python
"""Manual end-to-end smoke test for the zutec-smart-ingest Celery pipeline.

This script is intended to be run by a human operator against a live
zutec-smart-ingest deployment (or against a single-node dev environment
running the API + Celery worker + Postgres + Redis + Ollama). It walks
the full extraction flow and verifies that the five indexed pipeline
columns (``confidence_tier``, ``confidence_score``, ``processing_time_ms``,
``otel_trace_id``, ``pipeline_trace``) are populated correctly on the
resulting job row.

Usage
-----

Minimal invocation (uses defaults)::

    python scripts/smoke_test_pipeline.py --pdf ./tests/fixtures/sample.pdf

With explicit base URL and API key::

    python scripts/smoke_test_pipeline.py \\
        --pdf ./tests/fixtures/sample.pdf \\
        --base-url http://localhost:8000 \\
        --api-key MY_SERVICE_KEY \\
        --timeout 120

Poll-until-terminal behaviour: the script submits the PDF via
``POST /jobs``, then polls ``GET /jobs/{id}`` every 2 seconds until the
job reaches a terminal state (``COMPLETED``, ``REVIEW``, ``REJECTED``,
``FAILED``) or the overall timeout expires.

Exit codes
----------

- ``0`` — PASS: job reached a non-failing terminal state with all five
  indexed columns populated, and (if tier==REVIEW) the job appears in
  ``GET /review/pending``.
- ``1`` — FAIL: missing indexed columns, wrong status, or the review
  queue lookup did not find the job.
- ``2`` — TIMEOUT: the job did not reach a terminal state within the
  timeout window.
- ``3`` — CONFIG / TRANSPORT error: the script could not contact the
  API or one of the HTTP calls returned an unexpected error.

The script deliberately makes no assertions about the *values* of the
indexed columns (it only checks that they are NOT NULL) because the
concrete values depend on the content of the PDF and the configured
Ollama model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any


_TERMINAL_STATUSES = {"completed", "review", "rejected", "failed"}
_FAIL_STATUSES = {"failed", "rejected"}


def _print_header(title: str) -> None:
    bar = "=" * 72
    print(bar)
    print(f"  {title}")
    print(bar)


def _print_result(label: str, value: Any) -> None:
    print(f"  {label:.<40} {value}")


async def _submit_pdf(
    client: Any,
    *,
    base_url: str,
    pdf_path: Path,
    api_key: str | None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/jobs"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    with pdf_path.open("rb") as fh:
        files = {"file": (pdf_path.name, fh, "application/pdf")}
        response = await client.post(url, headers=headers, files=files)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"POST {url} returned {response.status_code}: {response.text}"
        )
    return response.json()


async def _poll_job(
    client: Any,
    *,
    base_url: str,
    job_id: str,
    api_key: str | None,
    timeout_seconds: float,
    poll_interval: float = 2.0,
) -> dict[str, Any] | None:
    url = f"{base_url.rstrip('/')}/jobs/{job_id}"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    while time.monotonic() < deadline:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"GET {url} returned {response.status_code}: {response.text}"
            )
        body = response.json()
        status = str(body.get("status", "")).lower()
        if status != last_status:
            _print_result("current status", status)
            last_status = status
        if status in _TERMINAL_STATUSES:
            return body
        await asyncio.sleep(poll_interval)

    return None


async def _check_review_queue(
    client: Any,
    *,
    base_url: str,
    job_id: str,
    api_key: str | None,
) -> bool:
    url = f"{base_url.rstrip('/')}/review/pending"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    response = await client.get(url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(
            f"GET {url} returned {response.status_code}: {response.text}"
        )
    body = response.json()
    items = body.get("items", [])
    for item in items:
        if str(item.get("job_id")) == str(job_id):
            return True
    return False


async def _run_smoke_test(args: argparse.Namespace) -> int:
    import httpx  # lazy import so --help works without httpx installed

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"ERROR: PDF file not found: {pdf_path}", file=sys.stderr)
        return 3

    _print_header(f"Submitting {pdf_path.name}")

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        try:
            submission = await _submit_pdf(
                client,
                base_url=args.base_url,
                pdf_path=pdf_path,
                api_key=args.api_key,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: submission failed: {exc}", file=sys.stderr)
            return 3

        # The API returns either a job_id directly or a batch_id with an
        # array of jobs. Be tolerant of both shapes.
        job_id = (
            submission.get("job_id")
            or (submission.get("jobs") or [{}])[0].get("job_id")
        )
        if not job_id:
            print(
                f"ERROR: could not find job_id in submission response: "
                f"{json.dumps(submission)}",
                file=sys.stderr,
            )
            return 3

        _print_result("job_id", job_id)
        _print_header("Polling for terminal status")

        try:
            job = await _poll_job(
                client,
                base_url=args.base_url,
                job_id=str(job_id),
                api_key=args.api_key,
                timeout_seconds=float(args.timeout),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: polling failed: {exc}", file=sys.stderr)
            return 3

        if job is None:
            print("TIMEOUT: job did not reach a terminal status", file=sys.stderr)
            return 2

        _print_header("Terminal job state")
        _print_result("status", job.get("status"))
        _print_result("confidence_tier", job.get("confidence_tier"))
        _print_result("confidence_score", job.get("confidence_score"))
        _print_result("processing_time_ms", job.get("processing_time_ms"))
        _print_result("otel_trace_id", job.get("otel_trace_id"))

        status = str(job.get("status", "")).lower()
        if status in _FAIL_STATUSES:
            print(
                f"FAIL: job ended in {status} state: "
                f"{job.get('error_message')}",
                file=sys.stderr,
            )
            return 1

        # Indexed-column checks — the core loop-breaker for QA cycle #3.
        missing: list[str] = []
        for field_name in (
            "confidence_tier",
            "confidence_score",
            "processing_time_ms",
        ):
            if job.get(field_name) in (None, ""):
                missing.append(field_name)
        if missing:
            print(
                "FAIL: indexed pipeline columns are NULL on the terminal "
                "job row: " + ", ".join(missing),
                file=sys.stderr,
            )
            print(
                "This is the Critical Issue #3 regression — the Celery "
                "task persisted results via update_job_status(result=...) "
                "instead of update_job_result, leaving indexed columns "
                "NULL.",
                file=sys.stderr,
            )
            return 1

        # Review-queue check when the job landed in REVIEW tier.
        tier = str(job.get("confidence_tier", "")).lower()
        if tier == "review":
            _print_header("Checking review queue")
            try:
                found = await _check_review_queue(
                    client,
                    base_url=args.base_url,
                    job_id=str(job_id),
                    api_key=args.api_key,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: review queue lookup failed: {exc}", file=sys.stderr)
                return 3
            if not found:
                print(
                    "FAIL: job is REVIEW-tier but does not appear in "
                    "GET /review/pending. The review queue API is not "
                    "filtering on JobORM.confidence_tier correctly — "
                    "Critical Issue #4 regression.",
                    file=sys.stderr,
                )
                return 1
            _print_result("found in review queue", "yes")

    _print_header("PASS")
    print("  All indexed pipeline columns populated and review queue OK.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manual end-to-end smoke test for the zutec-smart-ingest "
            "Celery pipeline. Submits a PDF, polls for terminal status, "
            "and verifies all five indexed pipeline columns are "
            "populated on the resulting job row."
        ),
    )
    parser.add_argument(
        "--pdf",
        required=True,
        help="Path to the PDF file to submit",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the zutec-smart-ingest API (default: %(default)s)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional X-API-Key for service-to-service auth",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Overall timeout in seconds for the poll loop (default: %(default)s)",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run_smoke_test(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
