#!/usr/bin/env bash
# install-hermes-plugin.sh — install the kahzaabu hermes plugin via symlink.
#
# Idempotent. Run after cloning this repo on a machine where hermes is
# installed. Creates a symlink so edits in this repo are reflected live
# in hermes — no copy step required.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/hermes-plugin"
HERMES_PLUGINS_DIR="${HOME}/.hermes/hermes-agent/plugins"
PLUGIN_DEST="${HERMES_PLUGINS_DIR}/kahzaabu"

if [[ ! -d "${PLUGIN_SRC}" ]]; then
    echo "❌ Plugin source not found at ${PLUGIN_SRC}" >&2
    exit 1
fi

if [[ ! -d "${HERMES_PLUGINS_DIR}" ]]; then
    echo "❌ Hermes plugins directory not found at ${HERMES_PLUGINS_DIR}" >&2
    echo "   Is hermes-agent installed? See https://github.com/NousResearch/hermes-agent" >&2
    exit 1
fi

# If a kahzaabu plugin already exists at the destination, decide what to do.
if [[ -L "${PLUGIN_DEST}" ]]; then
    current="$(readlink "${PLUGIN_DEST}")"
    if [[ "${current}" == "${PLUGIN_SRC}" ]]; then
        echo "✅ Symlink already points to this repo: ${PLUGIN_DEST} -> ${current}"
        exit 0
    fi
    echo "⚠️  ${PLUGIN_DEST} is a symlink to a DIFFERENT location:"
    echo "     ${current}"
    read -r -p "    Replace it with a symlink to ${PLUGIN_SRC}? [y/N] " ans
    [[ "${ans:-N}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
    rm "${PLUGIN_DEST}"
elif [[ -d "${PLUGIN_DEST}" ]]; then
    echo "⚠️  ${PLUGIN_DEST} is a real directory (not a symlink)."
    echo "    A previous install probably copied the plugin in directly."
    echo "    To proceed I'll move it to a backup and replace with a symlink."
    backup="${PLUGIN_DEST}.backup.$(date +%s)"
    read -r -p "    Move to ${backup} and continue? [y/N] " ans
    [[ "${ans:-N}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
    mv "${PLUGIN_DEST}" "${backup}"
    echo "    Backed up to ${backup}"
elif [[ -e "${PLUGIN_DEST}" ]]; then
    echo "❌ ${PLUGIN_DEST} exists but is not a directory or symlink. Refusing to clobber." >&2
    exit 1
fi

ln -s "${PLUGIN_SRC}" "${PLUGIN_DEST}"
echo "✅ Linked ${PLUGIN_DEST} -> ${PLUGIN_SRC}"

# Enable in hermes if not already enabled. Best-effort — non-fatal on failure.
if command -v hermes >/dev/null 2>&1; then
    hermes plugins enable kahzaabu 2>&1 | sed 's/^/   /'
    echo
    echo "Verify with:  hermes kahzaabu doctor"
else
    echo "ℹ️  'hermes' not on PATH — manually run: hermes plugins enable kahzaabu"
fi
