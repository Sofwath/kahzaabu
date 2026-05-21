# tests/

End-to-end and unit tests for the kahzaabu package.

| File | What it covers |
|---|---|
| `test_system.py` | End-to-end web stack: every page renders 200, every API returns the expected shape, public-mode filtering, auth flow, rate limiting, `/api/ask` budget cap, CLI side-effects. Runs against a live `kahzaabu web` instance. |

Run:

```bash
.venv/bin/python tests/test_system.py    # starts the server, exercises it
```

The hermes self-improver also produces unit tests at `tests/test_claims_db.py`
(on the `improve/unit-tests-claims-db` branch) — merge when ready.
