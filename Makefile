PYTHON ?= python3
ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
export PYTHONPATH := $(ROOT)lambda

.PHONY: data test preview search

data:
	$(PYTHON) scripts/build_data.py

test:
	$(PYTHON) -m pytest -q tests
	node --test tests/picker.test.js

preview:
	$(PYTHON) scripts/preview.py --station 33605 --station 33506

search:
	$(PYTHON) scripts/search_stops.py $(Q)
