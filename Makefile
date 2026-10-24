# EchoGuard sitting / standing tracker: developer commands (macOS / Linux).
#   make setup          create .venv and install pinned requirements (needs internet)
#   make setup-offline  recreate .venv from a Python that already has the deps (no network)
#   make setup-ml       optional: install scikit-learn/joblib for Random Forest training
#   make test           run the unit tests
#   make ports          list serial ports and likely ESP32 boards (read-only)
#   make monitor        print live CSI stats from the receiver (PORT=auto by default)
#   make dashboard      start the local web app on http://127.0.0.1:8050/ (ARGS="--serial /dev/cu.usbmodem2101")
#   make app            alias for make dashboard
#   make simulate       CLI monitor on SYNTHETIC simulated CSI (SCENARIO=demo, DURATION=0 = until Ctrl-C)
#   make dashboard-sim  web app on the SYNTHETIC simulation (SCENARIO=demo; desk_day for sitting/standing)
#   make evaluate       legacy: replay a recorded session through the fall heuristic (SESSION=data/recordings/<id>)
#   make train-rf       optional Random Forest training (needs make setup-ml)
#   make clean          remove caches (keeps .venv and data/)

VENV    := .venv
PY      := $(VENV)/bin/python
PORT    ?= auto
BAUD    ?= 921600
SCENARIO ?= demo
DURATION ?= 0
SESSION ?=
# Extra arguments for the dashboard, e.g. make dashboard ARGS="--serial auto"
ARGS    ?=

# Online setup: prefer Homebrew Python 3.11, then 3.12, then python3.11/3.12 on PATH.
# Python 3.14 is avoided on purpose (binary wheels for the pinned deps may be missing).
PYTHON_BOOTSTRAP ?= $(firstword $(wildcard /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3.12 $(HOME)/miniconda3/bin/python3.12) $(shell command -v python3.11 2>/dev/null) $(shell command -v python3.12 2>/dev/null))

# Offline setup: an interpreter that already has numpy/scipy/pandas/pyserial/flask/pytest.
# Uses PYTHON_BOOTSTRAP only when it was given explicitly (command line or environment).
ifneq ($(filter command line environment,$(origin PYTHON_BOOTSTRAP)),)
OFFLINE_PYTHON ?= $(PYTHON_BOOTSTRAP)
else
OFFLINE_PYTHON ?= $(HOME)/miniconda3/bin/python3.12
endif

IMPORT_CHECK := import importlib.metadata as m, numpy, scipy, pandas, serial, flask, pytest; \
	print('deps OK:', ', '.join(p + ' ' + m.version(p) for p in ('numpy', 'scipy', 'pandas', 'pyserial', 'flask', 'pytest')))

.PHONY: setup setup-offline setup-ml test ports monitor dashboard app simulate dashboard-sim evaluate train-rf clean

setup:
	@if [ -z "$(PYTHON_BOOTSTRAP)" ]; then echo "No python3.11/python3.12 found. Install with: brew install python@3.11"; exit 1; fi
	@echo "Using $(PYTHON_BOOTSTRAP) ($$($(PYTHON_BOOTSTRAP) --version))"
	@if [ ! -x "$(PY)" ]; then $(PYTHON_BOOTSTRAP) -m venv $(VENV); fi
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	$(PY) -c "$(IMPORT_CHECK)"

setup-offline:
	@if [ ! -x "$(OFFLINE_PYTHON)" ]; then echo "$(OFFLINE_PYTHON) not found. Pass one with: make setup-offline PYTHON_BOOTSTRAP=/path/to/python3"; exit 1; fi
	@echo "Using $(OFFLINE_PYTHON) ($$($(OFFLINE_PYTHON) --version)) with --system-site-packages (no network)"
	@$(OFFLINE_PYTHON) -c "$(IMPORT_CHECK)" || { echo "$(OFFLINE_PYTHON) lacks required packages (see requirements.txt)"; exit 1; }
	rm -rf $(VENV)
	$(OFFLINE_PYTHON) -m venv --system-site-packages $(VENV)
	$(PY) -c "$(IMPORT_CHECK)"

setup-ml:
	$(PY) -m pip install -r requirements-ml.txt

test:
	$(PY) -m pytest -q tests/

ports:
	$(PY) -m backend.cli ports

monitor:
	$(PY) -m backend.cli monitor --port $(PORT) --baud $(BAUD)

dashboard:
	$(PY) -m dashboard $(ARGS)

app: dashboard

# SYNTHETIC data, no ESP32 needed: make simulate SCENARIO=potential_fall DURATION=12
simulate:
	$(PY) -m backend.cli monitor --simulate $(SCENARIO) --duration $(DURATION)

dashboard-sim:
	$(PY) -m dashboard --simulate $(SCENARIO) $(ARGS)

# Heuristic check on recorded takes: make evaluate SESSION=data/recordings/<session_id>
evaluate:
	@if [ -z "$(SESSION)" ]; then echo "usage: make evaluate SESSION=data/recordings/<session_id>"; exit 2; fi
	$(PY) -m backend.cli evaluate --session $(SESSION)

# Optional (needs make setup-ml): Random Forest with session-held-out evaluation
train-rf:
	$(PY) -m backend.ml.train_rf --recordings data/recordings $(ARGS)

clean:
	rm -rf .pytest_cache
	find . -name __pycache__ -type d -prune -not -path "./$(VENV)/*" -exec rm -rf {} +
