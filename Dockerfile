# SPDX-License-Identifier: Apache-2.0
#
# Kahzaabu — one-command reproduction (ADR 0010).
#
# Build:
#   docker build -t kahzaabu .                # default: CPU-only ML stack
#   docker build -t kahzaabu --build-arg EMBED_EXTRA= .   # no ML at all
#
# Run CLI:
#   docker run --rm kahzaabu --help
# Run web UI on host port 8765:
#   docker run --rm -p 8765:8765 \
#     -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
#     kahzaabu kahzaabu web --host 0.0.0.0 --port 8765
#
# Notes:
# - Python 3.11 slim base for predictable wheel availability + small footprint.
# - Editable install picks up the bundled `data/registry/` and
#   `data/constitution/` artefacts; the SQLite DB itself is NOT baked in
#   (operator mounts via `-v $(pwd)/data:/app/data` for production).
# - Default embedding backend is `ml-local` (sentence-transformers, $0)
#   pinned to CPU-only torch wheels via PIP_EXTRA_INDEX_URL — drops the
#   image from ~3.2 GB (CUDA-pulled-by-default) to ~1.1 GB.
# - For zero ML stack: `--build-arg EMBED_EXTRA=` (web + tui only,
#   ~250 MB). Falls back to OpenAI/Voyage embeddings at run-time if
#   KAHZAABU_EMBED_PROVIDER is set.
# - For OpenAI/Voyage backends: `--build-arg EMBED_EXTRA=ml-openai`
#   (or `ml-voyage`) and set the relevant API key at run-time.
#
FROM python:3.11-slim

ARG EMBED_EXTRA=ml-local

# OS deps: git for reproducibility.current_git_sha() inside the container;
# build-essential just enough for native wheels (sentence-transformers' deps).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy minimum needed to install. Skip data/backups (gitignored anyway)
# and large model caches.
COPY pyproject.toml README.md LICENSE ./
COPY kahzaabu ./kahzaabu
COPY hermes-plugin ./hermes-plugin
COPY skills ./skills
COPY scripts ./scripts
COPY tests ./tests
COPY docs ./docs
COPY data/registry      ./data/registry
COPY data/constitution  ./data/constitution

# Use PyTorch's CPU-only wheel index to avoid pulling CUDA / nvidia-*
# packages (~2 GB saved). sentence-transformers depends on torch; the
# default index serves the GPU build even on x86/arm Linux containers
# that have no GPU. Setting PIP_EXTRA_INDEX_URL routes torch resolution
# to the CPU build first.
ENV PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu

# Editable install with web + tui + chosen embedding backend.
# Bracket-empty case ([web,tui,]) is harmless to pip — just installs
# web + tui.
RUN pip install --no-cache-dir -e ".[web,tui${EMBED_EXTRA:+,$EMBED_EXTRA}]"

# /app/data/kahzaabu.db will be created lazily on first run if absent.
# To bring an existing corpus in: mount `-v /host/data:/app/data`.
VOLUME ["/app/data"]

EXPOSE 8765
ENTRYPOINT ["kahzaabu"]
CMD ["--help"]
