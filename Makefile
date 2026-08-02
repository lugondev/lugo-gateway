# speech-text-transformer — service & dev tasks
# Usage: `make help`

# ---- Config (override on the CLI, e.g. `make start PORT=9000`) ----
VENV        ?= .venv
HOST        ?= 0.0.0.0
PORT        ?= 8000
APP         ?= app.main:app
PYTHONPATH  := apps/api_gateway
# macOS: let ctypes/opuslib find Homebrew's libopus (harmless on Linux, where
# libopus0 is already on the default search path).
DYLD_FALLBACK_LIBRARY_PATH ?= /opt/homebrew/lib:/usr/local/lib
export DYLD_FALLBACK_LIBRARY_PATH
RUN_DIR     ?= .run
PID         := $(RUN_DIR)/gateway.pid
LOG         ?= $(RUN_DIR)/gateway.log

PY          := $(VENV)/bin/python
UVICORN     := $(VENV)/bin/uvicorn
RUFF        := $(VENV)/bin/ruff
PYTEST      := $(VENV)/bin/pytest
export PYTHONPATH

.DEFAULT_GOAL := help

# ---- Help ----
.PHONY: help
help: ## Show this help
	@echo "speech-text-transformer — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---- Setup ----
.PHONY: install
install: ## Install the package + dev deps into .venv
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PY) -m pip install -e ".[dev,whisper]"

.PHONY: setup
setup: ## Interactive setup checklist: tick engines to install (host-aware). Scripted: scripts/setup.sh
	PYTHON=$(PY) $(PY) scripts/setup.py

.PHONY: check-system
check-system: ## Scan hardware, validate the setup, recommend an engine stack + compose file
	$(PY) scripts/check_system.py

# ---- Run (foreground, hot-reload) ----
.PHONY: dev run
dev run: ## Run in foreground with --reload (Ctrl-C to stop)
	$(UVICORN) $(APP) --reload --host $(HOST) --port $(PORT)

# ---- Service (background, PID-managed) ----
.PHONY: start
start: ## Start the gateway in the background (logs -> $(LOG))
	@mkdir -p $(RUN_DIR)
	@if [ -f $(PID) ] && kill -0 `cat $(PID)` 2>/dev/null; then \
		echo "already running (pid `cat $(PID)`) on port $(PORT)"; \
	elif lsof -ti tcp:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "port $(PORT) already in use by pid(s) `lsof -ti tcp:$(PORT) -sTCP:LISTEN | tr '\n' ' '`— run 'make stop' or set PORT=..."; \
		exit 1; \
	else \
		nohup $(UVICORN) $(APP) --host $(HOST) --port $(PORT) > $(LOG) 2>&1 & \
		echo $$! > $(PID); \
		sleep 1; \
		if kill -0 `cat $(PID)` 2>/dev/null; then \
			echo "started gateway (pid `cat $(PID)`) http://$(HOST):$(PORT)/ui — logs: $(LOG)"; \
		else \
			echo "failed to start — see $(LOG):"; tail -5 $(LOG); rm -f $(PID); exit 1; \
		fi; \
	fi

.PHONY: stop
stop: ## Stop the background gateway
	@if [ -f $(PID) ] && kill -0 `cat $(PID)` 2>/dev/null; then \
		kill `cat $(PID)` && echo "stopped pid `cat $(PID)`"; \
		rm -f $(PID); \
	else \
		echo "no PID file process; killing anything on port $(PORT)"; \
		lsof -ti tcp:$(PORT) | xargs -r kill 2>/dev/null || true; \
		rm -f $(PID); \
	fi

.PHONY: restart
restart: stop ## Restart the background gateway
	@sleep 1
	@$(MAKE) --no-print-directory start

.PHONY: status
status: ## Show whether the gateway is running
	@if [ -f $(PID) ] && kill -0 `cat $(PID)` 2>/dev/null; then \
		echo "running (pid `cat $(PID)`) on port $(PORT)"; \
	else \
		echo "not running (via PID file)"; \
	fi
	@lsof -i tcp:$(PORT) -sTCP:LISTEN 2>/dev/null || true

.PHONY: logs
logs: ## Tail the background gateway log
	@touch $(LOG); tail -f $(LOG)

# ---- Quality ----
.PHONY: test
test: ## Run the test suite
	$(PYTEST) -q

.PHONY: lint
lint: ## Lint with ruff
	$(RUFF) check apps tests

.PHONY: fmt
fmt: ## Auto-fix lint + format with ruff
	$(RUFF) check --fix apps tests
	$(RUFF) format apps tests
