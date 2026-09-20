# AATM - Agent Action Transaction Manager
# Convenience targets for setup, testing, and demo.

PYTHON ?= python

.PHONY: help install install-dev test test-unit test-integration test-property test-e2e \
        plan run demo clean lint report

help:
	@echo "AATM Makefile targets:"
	@echo "  install          Install runtime dependencies"
	@echo "  install-dev      Install runtime + dev dependencies (editable)"
	@echo "  test             Run the full test suite"
	@echo "  test-unit        Run unit tests only"
	@echo "  test-integration Run integration tests only"
	@echo "  test-property    Run property-based tests only"
	@echo "  test-e2e         Run end-to-end tests only"
	@echo "  demo             Run the canonical demo sequence"
	@echo "  clean            Remove generated runtime output and caches"

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

test-unit:
	$(PYTHON) -m pytest tests/unit -v

test-integration:
	$(PYTHON) -m pytest tests/integration -v

test-property:
	$(PYTHON) -m pytest tests/property -v

test-e2e:
	$(PYTHON) -m pytest tests/e2e -v

plan:
	$(PYTHON) main.py plan workflows/travel_booking.yaml

run:
	$(PYTHON) main.py run workflows/travel_booking.yaml

demo:
	bash demo.sh

clean:
	$(PYTHON) -c "import shutil,glob,os; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob('**/__pycache__', recursive=True)]"
	$(PYTHON) -c "import glob,os; [os.remove(p) for sub in ('audit','checkpoints','reports','runs') for p in glob.glob('output/'+sub+'/*') if not p.endswith('.gitkeep')]"
	@echo "Cleaned generated output and caches."
