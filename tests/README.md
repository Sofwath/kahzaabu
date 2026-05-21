# tests/

| File | Kind | Run via |
|---|---|---|
| `test_host_llm_branch.py` | unittest — offline, fast | `.venv/bin/python -m unittest discover tests/` or `.venv/bin/python tests/test_host_llm_branch.py` |
| `system_check.py` | standalone integration script — needs live server | `.venv/bin/python tests/system_check.py` |

**Naming convention**: only `test_*.py` files participate in `unittest discover` (and would in pytest if installed). Integration scripts that need a live web server use a different prefix (`system_check.py`, etc.) and are run explicitly — keeps automatic discovery from accidentally pulling in heavy / interactive suites.

`pytest` is not a project dependency; the unit tests use the stdlib `unittest` module directly. Install pytest yourself if you want it.

## Unit tests (`test_*.py`)

These run offline, no external dependencies, fast — safe to run on every commit.

- **`test_host_llm_branch.py`** — pins down the narrative-tricks `ctx.llm` branch in `qna_agentic.ask_agentic()`. Guards the kwarg signature, verifies the guarantee-pass conditions, confirms `host_llm.complete()` is called only when the section is missing AND article tools were touched. 4 tests, runs in < 0.01s.

## Integration scripts (no `test_` prefix)

Run these explicitly when you want a full-stack sanity check.

- **`system_check.py`** — exercises every web page + API endpoint, auth flow, rate-limiting, public-mode filtering, `/api/ask` budget cap, CLI publish/unpublish, and DB consistency between `/api/stats` and direct SQL counts. Requires a running web server at `$KAHZAABU_BASE` (default `http://127.0.0.1:8765`) and an admin user.

## The hermes self-improver

The self-improver skill (at `~/.hermes/skills/kahzaabu/kahzaabu-self-improver/`) writes generated unit tests into this directory under the `improve/unit-tests-*` branch family. Most recent: 17 unit tests for `claims_db.py` on branch `improve/unit-tests-claims-db`. Merge when reviewed.
