---
name: Bug report
about: Something in kahzaabu is broken or wrong
title: '[bug] '
labels: bug
assignees: ''
---

## Reproduction steps

What did you run? Include the exact command(s) — `kahzaabu pipeline`,
`kahzaabu eval`, a Python snippet, an HTTP request to a web endpoint, a
hermes chat invocation, etc.

```bash
# paste here
```

## What you expected

What output / behaviour should have happened.

## What actually happened

Include the actual output, error message, stack trace. If a request
returned a wrong HTTP status, paste `curl -i` output.

```
# paste here
```

## Environment

- OS:                              (e.g. macOS 14.3 / Ubuntu 22.04)
- Python:                          (`python3 --version`)
- kahzaabu version / commit:       (`git rev-parse HEAD` or release tag)
- Hermes plugin (if relevant):     (`hermes plugins list | grep kahzaabu`)
- Embedding provider in use:       (`echo $KAHZAABU_EMBED_PROVIDER` — default `local`)
- Anthropic SDK version:           (`pip show anthropic | grep Version`)

## Suspected scope

Pick one or more:

- [ ] V1 pipeline stage (scrape / extract / inspect / curate / verify / dv-compare)
- [ ] V2 enrichment (decompose / match / find-contradictions / enrich-factchecks / export-claimreview)
- [ ] Quality eval / golden set
- [ ] Web UI / HTTP API
- [ ] CLI ergonomics
- [ ] Hermes plugin integration
- [ ] Database schema / migrations
- [ ] Docs / README drift
- [ ] Other (describe)

## Additional context

Anything else — screenshots, links to source articles, links to
relevant ADRs, prior issues, etc.
