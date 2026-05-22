# SPDX-License-Identifier: Apache-2.0
#
# kahzaabu — operator entry points.
#
# Each target maps to a script that already exists; this Makefile is
# just a discoverable index. `make help` lists everything.

.DEFAULT_GOAL := help
.PHONY: help test ci-dry-run js-verify ui-smoke check-updates check-links \
        backup restore audit transparency-report docker-build \
        docker-lean docker-cpu eval clean-images

## help: show this message
help:
	@echo "kahzaabu maintenance targets:"
	@echo
	@echo "  TESTING"
	@echo "  make test                 Full Python test suite ($(shell ls tests/test_*.py 2>/dev/null | wc -l | tr -d ' ') test files)"
	@echo "  make ci-dry-run           Run the workflow in a fresh worktree"
	@echo "  make js-verify            Verify vendored JS libs against real call sites"
	@echo "  make ui-smoke             Full-page smoke test (needs live web server)"
	@echo "  make eval                 Golden-set quality eval (ADR 0008)"
	@echo
	@echo "  MAINTENANCE"
	@echo "  make check-updates        Detect drift in vendored JS libs (npm registry)"
	@echo "  make check-links          Probe /laws tile URLs against mvlaw.gov.mv"
	@echo "  make backup               Snapshot the SQLite DB to data/backups/"
	@echo "  make restore DATE=YYYY-MM-DD   Restore from a dated backup"
	@echo
	@echo "  AUDIT + REPORTING"
	@echo "  make audit                Bias/fairness chi-squared report (ADR 0010)"
	@echo "  make transparency-report SINCE=YYYY-MM-DD"
	@echo "                            Window-scoped public-facing report"
	@echo
	@echo "  DOCKER"
	@echo "  make docker-cpu           Build CPU-only image (~2.4 GB; default ML stack)"
	@echo "  make docker-lean          Build lean image (~210 MB; no ML)"
	@echo "  make clean-images         Remove old kahzaabu Docker images"
	@echo
	@echo "  Maintenance cadence: see docs/MAINTENANCE.md"

## test: run the full test suite + stale-name guard
test:
	./scripts/test.sh

## ci-dry-run: validate workflow against a fresh checkout
ci-dry-run:
	./scripts/ci-dry-run.sh

## js-verify: vendored-JS library call-site verification
js-verify:
	@cd scripts/js-verify && \
	  if [ ! -d node_modules ]; then npm install --silent --no-audit --no-fund; fi && \
	  npm run verify

## ui-smoke: load every web route in headless DOM + assert content renders
##           (requires a live server; defaults to HOST=http://127.0.0.1:8765)
ui-smoke:
	@cd scripts/js-verify && \
	  if [ ! -d node_modules ]; then npm install --silent --no-audit --no-fund; fi && \
	  HOST=$${HOST:-http://127.0.0.1:8765} node ui-smoke.mjs

## check-updates: scan npm registry for newer versions of vendored libs
check-updates:
	./scripts/check-vendor-updates.sh

## check-links: probe /laws tile URLs against the live AGO host
check-links:
	./scripts/check-external-links.sh

## backup: snapshot the SQLite DB
backup:
	./scripts/backup.sh

## restore: restore from a dated backup (DATE=YYYY-MM-DD required)
restore:
	@if [ -z "$(DATE)" ]; then \
	    echo "usage: make restore DATE=YYYY-MM-DD"; exit 2; \
	  fi
	./scripts/restore.sh $(DATE)

## eval: golden-set quality evaluation
eval:
	.venv/bin/kahzaabu eval

## audit: bias / fairness markdown report
audit:
	.venv/bin/kahzaabu audit

## transparency-report: public-facing window report (SINCE=YYYY-MM-DD)
transparency-report:
	@if [ -z "$(SINCE)" ]; then \
	    echo "usage: make transparency-report SINCE=YYYY-MM-DD"; exit 2; \
	  fi
	.venv/bin/kahzaabu transparency-report --since $(SINCE)

## docker-cpu: build the default CPU-only Docker image (~2.4 GB)
docker-cpu:
	docker build -t kahzaabu:cpu-light .

## docker-lean: build the no-ML Docker image (~210 MB)
docker-lean:
	docker build --build-arg EMBED_EXTRA= -t kahzaabu:lean .

## clean-images: remove all kahzaabu Docker images
clean-images:
	@docker images --format '{{.Repository}}:{{.Tag}}' \
	    | grep '^kahzaabu:' \
	    | xargs -r docker rmi || true
	@echo "remaining kahzaabu images:"
	@docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' \
	    | grep '^kahzaabu:' || echo "  (none)"
