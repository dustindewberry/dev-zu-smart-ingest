"""Transitional shared types stub for the sharp-owl worktree (task step-10).

This file exists ONLY because the canonical task-2 (bright-pike) types module
is not present in this worktree's branch. It contains a minimal ``AuthContext``
dataclass that the auth middleware attaches to ``request.state`` and which
route handlers receive via the ``get_auth_context`` dependency.

The field names and type annotations here are byte-identical to the canonical
``AuthContext`` in bright-pike's ``src/zubot_ingestion/shared/types.py`` (lines
45-53). On merge with the canonical task-2 output, the canonical, full-featured
types module is a strict superset of this stub and MUST overwrite this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AuthContext:
    """Authenticated user context attached to every request by AuthMiddleware.

    Populated by ``AuthMiddleware._authenticate`` on a successful credential
    check and stored on ``request.state.auth_context`` so route handlers can
    consume it via the ``get_auth_context`` FastAPI dependency.
    """

    user_id: str
    auth_method: Literal["api_key", "wod_token"]
    deployment_id: int | None = None
    node_id: int | None = None
    permissions: list[str] = field(default_factory=list)


__all__ = ["AuthContext"]
