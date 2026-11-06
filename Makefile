# vgi-pdf — dev and test targets.
#
# Usage:
#   make test       # unit/integration (pytest) + end-to-end SQL (haybarn-unittest)
#   make test-unit  # pytest only
#   make test-sql   # DuckDB sqllogictest .test files via haybarn-unittest
#
# test-sql is self-contained: it points VGI_PDF_WORKER at the worker run as a uv
# stdio subprocess (exactly how DuckDB drives it after ATTACH) and runs the
# files under test/sql/. haybarn-unittest is a uv tool:
#   uv tool install --prerelease allow 'haybarn-unittest>=2.0.0rc11,<2.1'   # ~/.local/bin/haybarn-unittest
# haybarn 2.0 is required: its community `vgi` extension is the one that speaks the
# worker's VGI protocol (vgi.v3 / 3.1). --keep-home lets the runner see the
# extensions installed under the real $HOME (haybarn-unittest 2.0 isolates it).

# Worker command DuckDB uses for ATTACH (overridable).
WORKER_STDIO    ?= uv run --python 3.13 pdf_worker.py

# haybarn-unittest lives in the uv tools bin; keep it on PATH.
HAYBARN_BIN     ?= $(HOME)/.local/bin
TEST_DIR         = .
TEST_PATTERN     = test/sql/*

.PHONY: test test-unit test-sql lint

test: test-unit test-sql

test-unit:
	uv run pytest -q

test-sql:
	PATH="$(HAYBARN_BIN):$$PATH" \
		VGI_PDF_WORKER="$(WORKER_STDIO)" \
		haybarn-unittest --keep-home --test-dir "$(TEST_DIR)" "$(TEST_PATTERN)"

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy vgi_pdf/
