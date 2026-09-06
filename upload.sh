#!/usr/bin/env bash
# Quick Google Photos uploader wrapper (keeps the progress bar).
#
# Usage:
#   ./upload.sh <file> [more files...]
#   ./upload.sh --dir ./example-files-upload
#   ./upload.sh --links <file>            # also print DDL + CF links
#
# Any up.py flags (--dir, --links, --threads N, --auth ...) are passed through.

set -euo pipefail

# Run from the script's own directory so config.py / cookies.txt resolve.
cd "$(dirname "$(readlink -f "$0")")"

if [[ $# -eq 0 ]]; then
    echo "Usage: ./upload.sh <file> [more files...] [--dir DIR] [--links]" >&2
    exit 1
fi

exec uv run up.py "$@"
