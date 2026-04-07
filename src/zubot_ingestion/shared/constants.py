"""Shared constants for the Zubot Ingestion Service.

NOTE: This worktree was branched before task-26 (shared-constants-and-config-keys)
was merged. To make this task's modules importable in isolation, this file
contains only the two symbols required by ``api/app.py`` and ``__main__.py``
(``SERVICE_NAME`` and ``SERVICE_VERSION``). The canonical, full version of
this module is produced by task-26 and will overwrite this stub on merge.

The values below are intentionally kept identical to the canonical version
so the merge resolution is trivial (and the runtime behavior is unchanged
either way).
"""

from __future__ import annotations

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"

__all__ = ["SERVICE_NAME", "SERVICE_VERSION"]
