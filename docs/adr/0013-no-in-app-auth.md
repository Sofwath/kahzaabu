# ADR 0013 — No in-app authentication; web UI is read-only public

**Status**: Accepted (2026-05-21)

## Context

Kahzaabu V2 shipped with a session-cookie-based admin/editor system
(`kahzaabu/auth.py` + `web/api/auth.py` + `web/api/admin.py` +
`/login` + `/admin/queue` + `/admin/run` + `web_users` table + bcrypt
via `passlib` + signed cookies via `itsdangerous` + two CLI
commands `kahzaabu create-user` and `kahzaabu set-password`). The
intent was a publish-workflow gate: fact-checks land as
`published = 0` and an admin reviews them in the web queue before
flipping to `published = 1`.

In practice, three problems compounded:

1. **The operator IS the only admin.** Kahzaabu is a single-person
   civic-tech project; the publish queue's reviewer and the
   pipeline's operator are the same individual. Adding a password
   layer between them is friction without benefit.

2. **In-app credentials are an OSS-distribution liability.** A
   leaked bcrypt admin hash from a deployed instance, a re-used
   password across deployments, or a signed-cookie-secret rotation
   that gets missed — these are real failure modes for a project
   that publishes both the code AND a reference deployment.
   `kahzaabu` is meant to be cloned and run by other small-state
   civic-tech maintainers; every one of those instances would
   accumulate its own credentials surface.

3. **Existing operator actions already work via CLI.** Every
   workflow gated by the web admin queue has a CLI equivalent:
   `kahzaabu publish <id>`, `kahzaabu pipeline`, `kahzaabu eval`,
   etc. The CLI inherits the operator's filesystem permissions —
   no in-app credential is needed because the OS already
   authenticates them via shell login.

The publish workflow itself remains valuable (curator output is
draft until reviewed), but the *gating mechanism* should be
filesystem permissions, not an in-app password.

## Decision

**Delete all in-app authentication.** Specifically:

1. **Remove modules**:
   - `kahzaabu/auth.py` (password hashing + session signing)
   - `kahzaabu/web/api/auth.py` (`/api/login`, `/api/logout`, `/api/me`)
   - `kahzaabu/web/api/admin.py` (`/api/admin/queue`, `/api/admin/publish`,
     `/api/admin/pipeline/run`)
2. **Remove HTML pages**: `login.html`, `admin_queue.html`,
   `admin_run.html`.
3. **Remove CLI commands**: `kahzaabu create-user`,
   `kahzaabu set-password`.
4. **Remove Python dependencies** from `[web]` extras: `passlib[bcrypt]`,
   `itsdangerous`. The transitive `bcrypt` goes with them.
5. **Rewrite every `web/api/*.py`** to drop `Depends(current_user)`
   parameters. Helpers like `_gated(user)` collapse to
   "always filter `published = 1`"; `_public_filter(user)` collapses
   to `" AND published = 1"`. The web UI surfaces only published
   items, unconditionally.
6. **Preserve the `web_users` table CREATE statement** in
   `CLAIMS_SCHEMA` so already-deployed DBs don't break on migration.
   The table stays empty; no code reads or writes it any more.
7. **Operator workflows move to CLI-only**:
   - Publish a fact-check: `kahzaabu publish <id>`
   - Trigger pipeline: `kahzaabu pipeline` (or the `kahzaabu_pipeline_run`
     hermes tool, gated by `KAHZAABU_ALLOW_PIPELINE=1` so it's still
     a deliberate opt-in).
   - Backups, restores, audit reports, transparency reports — all CLI.

The web UI's only writes that remain are: rate-limited `/api/ask`
(Q&A budget-capped per day) and `/api/corrections` (public moderation-
queue form that appends rows; the operator reads them with the CLI).

## Alternatives considered

- **Keep password auth, just make it stronger** (2FA, OAuth, etc.).
  Rejected — the problem isn't the strength of the credential; it's
  that an in-app credential exists at all for a single-operator
  civic-tech project. Strengthening it adds complexity without
  removing the OSS-distribution liability.
- **Replace password auth with SSO** (GitHub OAuth, etc.). Rejected
  for the same reason plus a new one: it ties the project to a
  third-party identity provider, which fragments the install story
  (each operator must configure OAuth credentials).
- **IP allowlist instead of password.** Rejected — IP allowlists
  are brittle (operator's home IP changes, CGNAT, VPN), and the
  threat model doesn't justify the operational pain. The operator
  is the only one with shell access anyway.
- **Local-only listen** (bind to 127.0.0.1; require an SSH tunnel
  for remote admin). Acceptable as a deploy-side decision but not
  a code-level requirement. Documented as the recommended pattern
  in `docs/MAINTENANCE.md`.
- **Soft-delete the auth code rather than remove it.** Rejected —
  dead-but-importable code is worse than removed code. The git
  history preserves the prior implementation; the Apache-2.0
  license preserves the right to revive it in a fork.

## Consequences

**Positive.**

- **Zero in-app credentials.** No bcrypt hashes, no cookie secrets,
  no `KAHZAABU_SECRET_KEY` env var to manage, no `web_users` row to
  rotate. The attack surface shrinks to "the operator has shell
  access," which is the actual threat model already.
- **Two fewer Python dependencies** (`passlib`, `itsdangerous`).
  Both were narrow-purpose; their removal slightly shrinks the
  Docker image and eliminates two CVE-monitoring obligations.
- **Cleaner OSS-distribution story.** A fresh clone has no
  credentials to initialise, no admin user to create, no
  `KAHZAABU_SECRET_KEY` to generate. `kahzaabu web` works out of
  the box as a read-only viewer; the operator runs `kahzaabu
  publish` from the same shell they use for everything else.
- **Net ~1,200 LOC removed** (auth module + endpoints + login UI
  + admin UI + dependent conditionals collapsed).

**Negative.**

- **Anyone with filesystem access to the DB is "the operator."**
  This is fine for single-operator instances (the typical case);
  it would not be fine for multi-tenant SaaS-style deployment. We
  accept this trade-off — kahzaabu was never designed as multi-
  tenant SaaS, and an operator who wants that should re-introduce
  auth in their fork.
- **No web UI for the corrections moderation queue.** The
  operator now reads the `corrections` table with the CLI (or
  any SQLite client) and decides what to publish. Adequate for
  current volume (~1–2 corrections per release window).
- **A future operator who wants to give an editor publish access
  without shell access has to either** (a) give them a CLI-only
  shell account on the server, or (b) re-introduce auth in their
  fork. Documented in this ADR so the trade-off is visible.

## Regression guards

`tests/test_secrets_hygiene.py::NoAuthSurfaceTests` (4 tests) pins
the posture and fails CI on any re-introduction:

- `test_auth_modules_absent` — verifies `kahzaabu/auth.py`,
  `web/api/auth.py`, `web/api/admin.py` do not exist
- `test_auth_html_pages_absent` — verifies `login.html`,
  `admin_queue.html`, `admin_run.html` do not exist
- `test_pyproject_does_not_declare_auth_deps` — scans
  `pyproject.toml` for declared lines matching `passlib`,
  `itsdangerous`, or `bcrypt`
- `test_no_login_or_admin_routes_in_active_code` — greps every
  `web/*.py` for re-introduced `/login` or `/admin` route
  declarations
