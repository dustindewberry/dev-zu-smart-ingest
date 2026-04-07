"""TRANSITIONAL STUB — minimal subset of the canonical task-2 shared types.

This file exists ONLY because the sharp-spark worktree was branched from the
initial commit and the canonical ``src/zubot_ingestion/shared/types.py``
produced by sibling task-2 (bright-pike) is not present here.

It contains a minimal :class:`AuthContext` whose field shape is byte-identical
to the canonical entity. On merge, the canonical file MUST overwrite this stub.
The rate-limit middleware reads ``request.state.auth_context.user_id`` to
build a per-user rate-limit key, so only the ``user_id`` field is exercised
by this task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AuthContext:
    """Authenticated principal attached to ``request.state`` by AuthMiddleware."""

    user_id: str
    auth_method: Literal["api_key", "wod_token"]
    deployment_id: int | None = None
    node_id: int | None = None
    permissions: list[str] = field(default_factory=list)


__all__ = ["AuthContext"]
