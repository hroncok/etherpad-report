#!/bin/bash
# Generate report from JSON data using Claude CLI

set -euo pipefail

if [ ! -f "json" ]; then
    echo "Error: 'json' file not found" >&2
    exit 1
fi

if [ ! -f "README" ]; then
    echo "Error: 'README' file not found" >&2
    exit 1
fi

exec claude \
    --system-prompt-file README \
    --allowedTools "Read,Write" \
    -- "Read the file $PWD/json and follow your system prompt instructions."
