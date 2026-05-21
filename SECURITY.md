# Security policy

## Reporting a vulnerability

Email **Sofwathullah.Mohamed@gmail.com** with `[kahzaabu-security]` in the subject.

Please **do not** open public GitHub issues for security reports. We follow a
**90-day responsible-disclosure** window:

1. We acknowledge receipt within 72 hours.
2. We investigate, fix, and release a patched version.
3. After the fix ships (or after 90 days from the report date, whichever
   comes first), we publish the report with attribution and remediation
   details.

## What to report

Kahzaabu is a fact-checking pipeline. The security-sensitive surfaces are:

**Posture note**: kahzaabu has no in-app authentication. The web UI is
read-only public; operator actions (publish, pipeline runs, backups,
restores) happen via the `kahzaabu` CLI on the operator's filesystem
and inherit OS-level permissions. There are no passwords, sessions,
or admin users anywhere in the codebase.

| Surface | Concern |
|---|---|
| `kahzaabu/web/*` — FastAPI app | XSS, SQL injection, rate-limit bypass on `/api/ask`. (No auth surface to bypass — none exists.) |
| `kahzaabu/extractor.py`, `curator.py`, `decomposer.py`, `verifier.py`, `qna*.py` | Prompt injection that exfiltrates corpus data, system-prompt leakage, jailbreak vectors |
| `kahzaabu/claims_db.py` | SQL injection (we use parameterised queries everywhere — flag any deviation) |
| `kahzaabu/scraper.py` | SSRF, request smuggling, scraper-side cache poisoning |
| `kahzaabu/embeddings.py` | API-key leakage through provider error messages |
| `kahzaabu/web/static/*` | DOM-based XSS in user-rendered content (especially the contradictions reasoning-chain JSON) |
| `scripts/*` | Shell injection in backup/restore paths |
| `data/kahzaabu.db` | If you have access and find PII exposure (corpus is all public sources, but extraction artifacts may surface inferred attributes) |

## What is NOT a vulnerability

- LLM outputs that disagree with expected verdicts — that's a quality issue,
  file a regular issue with a golden-set fixture.
- Cost over-runs from the agentic `/api/ask` endpoint — covered by
  `slowapi` rate limits; tune them per deployment if needed.
- Public press releases being archived — corpus is intentionally public.

## Supported versions

This is a research/civic-tech project. We support the `main` branch.
Older tagged releases get security patches if the underlying issue
still exists; if not, please rebase onto `main`.
