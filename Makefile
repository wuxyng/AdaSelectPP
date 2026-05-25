SHELL := /usr/bin/env bash

.PHONY: phase05-env-check phase05-tests phase05-firstpass phase05-pack

phase05-env-check:
	bash scripts/server/env_check.sh

phase05-tests:
	bash scripts/server/run_phase05_tests.sh

phase05-firstpass:
	bash scripts/server/run_phase05_firstpass.sh

phase05-pack:
	@test -n "$(RUN_DIR)" || (echo "Usage: make phase05-pack RUN_DIR=<dir>" >&2; exit 2)
	bash scripts/server/pack_results.sh "$(RUN_DIR)"
