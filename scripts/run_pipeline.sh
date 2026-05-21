#!/bin/bash
# Wrapper for launchd / cron: loads API key from a file with restricted
# perms, then runs `kahzaabu pipeline`.
#
# Setup:
#   mkdir -p ~/.config/kahzaabu
#   echo 'sk-ant-...' > ~/.config/kahzaabu/api_key      # placeholder; real key
#   chmod 600 ~/.config/kahzaabu/api_key
#
# launchd plist points ProgramArguments at this script's absolute path
# on YOUR machine (e.g. /Users/<you>/Developer/.../scripts/run_pipeline.sh).
# The script itself derives PROJECT from $0 so it works regardless of
# where the repo is cloned.

set -euo pipefail

# Derive PROJECT from the script's own location ($0). Resolves symlinks
# so a launchd plist symlinked from ~/Library/LaunchAgents/ still gets
# the real repo path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

KEY_FILE="${ANTHROPIC_API_KEY_FILE:-$HOME/.config/kahzaabu/api_key}"

if [[ ! -f "$KEY_FILE" ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: API key file not found: $KEY_FILE" >&2
    exit 1
fi

# Read the key, strip whitespace, export it
export ANTHROPIC_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"

cd "$PROJECT"

exec "$PROJECT/.venv/bin/kahzaabu" pipeline "$@"
