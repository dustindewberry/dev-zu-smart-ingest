#!/usr/bin/env bash
#
# lint-architecture.sh
#
# Enforces the layered architecture defined in
# .forge/blueprints/<id>/phase2/boundary-contracts.md by running:
#
#   1. import-linter (lint-imports) against .importlinter
#   2. pydeps cycle detection against .pydeps.yml
#
# Exits non-zero on any contract violation or import cycle so it can be wired
# into pre-commit hooks and CI pipelines.

set -euo pipefail

# Resolve repo root regardless of where the script is invoked from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Make src/ importable so import-linter can resolve zubot_ingestion.*
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
RESET=$'\033[0m'

fail() {
    echo "${RED}[lint-architecture] FAIL:${RESET} $*" >&2
    exit 1
}

info() {
    echo "${GREEN}[lint-architecture]${RESET} $*"
}

warn() {
    echo "${YELLOW}[lint-architecture]${RESET} $*"
}

# ---------------------------------------------------------------------------
# 1. import-linter (layer + forbidden contracts)
# ---------------------------------------------------------------------------
if ! command -v lint-imports >/dev/null 2>&1; then
    fail "lint-imports not found on PATH. Install with: pip install import-linter"
fi

if [[ ! -f "${REPO_ROOT}/.importlinter" ]]; then
    fail ".importlinter config missing at ${REPO_ROOT}/.importlinter"
fi

info "Running import-linter against .importlinter ..."
if ! lint-imports --config "${REPO_ROOT}/.importlinter"; then
    fail "import-linter detected a layer or forbidden-import violation."
fi
info "import-linter: all contracts passed."

# ---------------------------------------------------------------------------
# 2. pydeps cycle detection (best-effort, non-fatal if pydeps missing)
# ---------------------------------------------------------------------------
if command -v pydeps >/dev/null 2>&1; then
    if [[ -f "${REPO_ROOT}/.pydeps.yml" ]]; then
        info "Running pydeps cycle detection ..."
        # --show-cycles prints cycles to stdout and exits non-zero when any exist
        if ! pydeps src/zubot_ingestion \
                --config-file "${REPO_ROOT}/.pydeps.yml" \
                --show-cycles \
                --no-output \
                --noshow; then
            fail "pydeps detected an import cycle in zubot_ingestion."
        fi
        info "pydeps: no import cycles detected."
    else
        warn ".pydeps.yml not found - skipping cycle detection."
    fi
else
    warn "pydeps not installed - skipping cycle detection. Install with: pip install pydeps"
fi

info "All architecture checks passed."
exit 0
