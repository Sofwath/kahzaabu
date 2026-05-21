#!/bin/bash
# Wrapper for launchd: loads API key from a file with restricted perms,
# then runs `kahzaabu pipeline`.
#
# Setup:
#   mkdir -p ~/.config/kahzaabu
#   echo 'sk-ant-...' > ~/.config/kahzaabu/api_key
#   chmod 600 ~/.config/kahzaabu/api_key
#
# launchd plist should point ProgramArguments at this script:
#   <string>/Users/sofwath/Developer/myLabs/kahzaabu/scripts/run_pipeline.sh</string>

set -euo pipefail

PROJECT="/Users/sofwath/Developer/myLabs/kahzaabu"
KEY_FILE="${ANTHROPIC_API_KEY_FILE:-$HOME/.config/kahzaabu/api_key}"

if [[ ! -f "$KEY_FILE" ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: API key file not found: $KEY_FILE" >&2
    exit 1
fi

# Read the key, strip whitespace, export it
export ANTHROPIC_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"

cd "$PROJECT"

exec "$PROJECT/.venv/bin/kahzaabu" pipeline "$@"
