# EchoGuard developer commands (macOS / Linux).
#   make setup      create .venv and install pinned requirements
#   make test       run the unit tests
#   make ports      list serial ports and likely ESP32 boards (read-only)
#   make monitor    print live CSI stats from the receiver (PORT=auto by default)
#   make dashboard  start the local dashboard (added in Milestone 2)
#   make clean      remove caches (keeps .venv and data/)

VENV    := .venv
PY      := $(VENV)/bin/python
PORT    ?= auto
BAUD    ?= 921600

# Prefer Homebrew Python 3.11, then 3.12, then whatever python3.11/3.12 is on PATH.
# Python 3.14 is avoided on purpose (binary wheels for the pinned deps may be missing).
PYTHON_BOOTSTRAP ?= $(firstword $(wildcard /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3.12 $(HOME)/miniconda3/bin/python3.12) $(shell command -v python3.11 2>/dev/null) $(shell command -v python3.12 2>/dev/null))

.PHONY: setup test ports monitor dashboard clean

setup:
	@if [ -z "$(PYTHON_BOOTSTRAP)" ]; then echo "No python3.11/python3.12 found. Install with: brew install python@3.11"; exit 1; fi
	@echo "Using $(PYTHON_BOOTSTRAP) ($$($(PYTHON_BOOTSTRAP) --version))"
	@if [ ! -x "$(PY)" ]; then $(PYTHON_BOOTSTRAP) -m venv $(VENV); fi
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest -q tests/

ports:
	$(PY) -m backend.cli ports

monitor:
	$(PY) -m backend.cli monitor --port $(PORT) --baud $(BAUD)

dashboard:
	@echo "The dashboard arrives in Milestone 2. For now use: make monitor"

clean:
	rm -rf .pytest_cache
	find . -name __pycache__ -type d -prune -not -path "./$(VENV)/*" -exec rm -rf {} +
