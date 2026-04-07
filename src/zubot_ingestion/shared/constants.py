"""Shared constants — minimal stub for this worktree.

The canonical full version is produced by task-26 (witty-maple). On merge,
that version (which is a strict superset of this stub) should win.
"""

SERVICE_NAME = "zubot-ingestion"
SERVICE_VERSION = "0.1.0"

# Sensitive key fragments that the structured-logging scrubber must redact.
# Any log field whose key contains one of these (case-insensitive) is replaced
# with the REDACTION_PLACEHOLDER value below.
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "jwt",
    "token",
    "password",
    "secret",
    "file_bytes",
    "file_contents",
    "authorization",
)

REDACTION_PLACEHOLDER = "***REDACTED***"
