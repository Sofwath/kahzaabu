#!/usr/bin/env bash
# install-hermes-skills.sh — symlink kahzaabu's hermes skills into
# ~/.hermes/skills/. Idempotent. Companion to install-hermes-plugin.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SRC="${REPO_ROOT}/skills"
HERMES_SKILLS_DIR="${HOME}/.hermes/skills"

if [[ ! -d "${SKILLS_SRC}" ]]; then
    echo "❌ ${SKILLS_SRC} not found" >&2
    exit 1
fi
mkdir -p "${HERMES_SKILLS_DIR}"

for skill_dir in "${SKILLS_SRC}"/*/; do
    name="$(basename "${skill_dir}")"
    dest="${HERMES_SKILLS_DIR}/${name}"
    if [[ -L "${dest}" ]]; then
        current="$(readlink "${dest}")"
        if [[ "${current}" == "${skill_dir%/}" ]]; then
            echo "  ✅ ${name} — already symlinked"
            continue
        fi
        echo "  ⚠ ${name} symlink points elsewhere: ${current}"
        read -r -p "    Replace it? [y/N] " ans
        [[ "${ans:-N}" =~ ^[Yy]$ ]] || continue
        rm "${dest}"
    elif [[ -d "${dest}" ]]; then
        backup="${dest}.backup.$(date +%s)"
        echo "  ⚠ ${name} is a real directory — moving to ${backup}"
        mv "${dest}" "${backup}"
    fi
    ln -s "${skill_dir%/}" "${dest}"
    echo "  ✅ ${name} — linked"
done

echo
echo "Done. Verify with:  hermes skills list"
