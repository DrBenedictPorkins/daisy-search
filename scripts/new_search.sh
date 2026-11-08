#!/bin/bash
# Thin wrapper around `daisy new-search`.
#
# Creates the project row in Postgres AND scaffolds its case workspace in one
# step, so the database and the folder on disk always agree. The scaffolding
# itself lives in src/daisy/workspace.py — this script exists only so the flow
# is reachable from a plain shell.
#
# Usage: ./scripts/new_search.sh "<name>" ["<description>"]

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <subject_name> [description]" >&2
    exit 1
fi

DAISY_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run --directory "$DAISY_ROOT" daisy new-search "$@"
