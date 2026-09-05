PYTHON ?= python3

.PHONY: test lint env-check scrub gate build

test:
	$(PYTHON) -m pytest tests -q -p no:cacheprovider
	bash tools/scrub-audit.sh .
	$(MAKE) env-check
	$(MAKE) gate

lint:
	$(PYTHON) -m compileall -q src
	$(PYTHON) tools/env-inventory.py --check

env-check:
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	$(PYTHON) tools/env-inventory.py --output "$$tmp" && diff -u docs/env.md "$$tmp"
	$(PYTHON) tools/env-inventory.py --check

scrub:
	bash tools/scrub-audit.sh .

gate:
	$(PYTHON) -B tools/check_publication_gate.py --selftest
	$(PYTHON) -B tools/check_publication_gate.py --root . --account-slug shojikumaru --no-registry

build:
	uv build
