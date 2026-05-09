# SpiderWire — dev shortcuts for the gss-ctrl CLI.

# Load .env if present and export every var to recipe shells.
# Recipe lines run in their own shell, so plain `make` vars don't reach
# tools like twine that look at the environment — `export` bridges that.
ifneq (,$(wildcard .env))
include .env
export
endif

PORT     ?= /dev/ttyUSB0
ADDR     ?= 0x0A
QTY      ?= 28
SPEED    ?= 15
PCT      ?= 50
INTERVAL ?= 1.0
HEARTBEAT ?= 3.5

PYTHON ?= uv run python

.PHONY: install test lint build check publish-test publish scan poll read fan blower light help

help:
	@echo "Dev:"
	@echo "  make install                       Install deps + dev extras (uv sync --extra dev)"
	@echo "  make test                          Run pytest suite"
	@echo "  make lint                          Run ruff"
	@echo "  make build                         Build wheel + sdist into dist/"
	@echo "  make check                         twine check --strict dist/*"
	@echo "  make publish-test                  Upload dist/ to TestPyPI (creds via .env)"
	@echo "  make publish                       Upload dist/ to PyPI     (creds via .env)"
	@echo ""
	@echo "  .env (gitignored) is auto-loaded. Expected keys for publish:"
	@echo "    TWINE_USERNAME=__token__"
	@echo "    TWINE_PASSWORD=pypi-...   # PyPI or TestPyPI API token"
	@echo ""
	@echo "Bus operations:"
	@echo "  make scan    PORT=/dev/ttyUSB0      Scan bus for devices"
	@echo "  make poll    PORT=/dev/ttyUSB0      Master mode (poll + heartbeat)"
	@echo "  make read    PORT=... ADDR=0x0A QTY=28"
	@echo "  make fan     PORT=... ADDR=0x04 SPEED=15"
	@echo "  make light   PORT=... PCT=50"
	@echo "  make blower  PORT=... PCT=40"

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .

build:
	rm -rf dist/
	uv run python -m build

check:
	uv run twine check --strict dist/*

publish-test:
	uv run twine upload --repository testpypi dist/*

publish:
	uv run twine upload dist/*

scan:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) scan

poll:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) poll --interval $(INTERVAL) --heartbeat $(HEARTBEAT)

read:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) read $(ADDR) $(QTY)

fan:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) fan $(ADDR) $(SPEED)

light:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) light $(PCT)

blower:
	$(PYTHON) -m gss_ctrl_pc -v $(PORT) blower $(PCT)
