# Use the local venv when it exists, otherwise the system interpreter.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: all check strict build install clean serve venv

all: check build

## Validate the data registry. Exits non-zero on a broken invariant.
check:
	$(PY) scripts/validate.py

## Same, but warnings fail too. Use before publishing anything.
strict:
	$(PY) scripts/validate.py --strict

## Render the report + the interactive seat simulator. Refuses to run while
## validation reports errors.
build:
	$(PY) scripts/build.py
	$(PY) scripts/build_simulator.py

## One-time environment setup inside the workspace.
venv:
	python3 -m venv --system-site-packages .venv
	.venv/bin/python -m pip install -r requirements.txt

install:
	$(PY) -m pip install -r requirements.txt

## Preview the report locally at http://localhost:8000/report.html
serve: build
	$(PY) -m http.server 8000 --directory output

## Remove generated artefacts (data/ is never touched).
clean:
	rm -rf output/report.html output/ceagi_scores.csv output/ceagi_scores.json \
	       output/audit_trail.md output/charts output/seat_simulator.html
