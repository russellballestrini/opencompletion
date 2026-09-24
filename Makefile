# Makefile for OpenCompletion
#
# Every target runs through venv/bin/python so a bare `make test` on a fresh
# checkout, a developer shell & GitHub Actions all run our same tools. CI
# (.github/workflows/test.yml) calls these targets and nothing else.

.PHONY: help
help:
	@echo "OpenCompletion"
	@echo "=============="
	@echo ""
	@echo "Setup:"
	@echo "  venv                 - Create venv/ & install app + test dependencies"
	@echo "  init-db              - Create database tables (needs vars.sh)"
	@echo ""
	@echo "Tests (no network, no API keys):"
	@echo "  test                 - Unit, integration, functional & YAML validation"
	@echo "  test-unit            - tests/unit/"
	@echo "  test-integration     - tests/integration/"
	@echo "  test-functional      - tests/functional/ (pages, auth, streaming, games)"
	@echo "  test-ui              - Page contract: shared shell, mobile, honest copy"
	@echo "  test-validator       - YAML validator tests"
	@echo "  test-yaml-loading    - YAML loading/parsing tests"
	@echo "  test-activity-flows  - Activity flow tests"
	@echo "  test-battleship      - Battleship modes, Jev Reasoner & game flow"
	@echo "  test-guarded-ai      - research/guarded_ai.py tests"
	@echo "  test-multiple-files  - Integration tests across every activity file"
	@echo "  validate-yaml        - Validate every YAML file in research/"
	@echo ""
	@echo "Lint & CI:"
	@echo "  lint                 - black --check & flake8 (syntax, undefined names)"
	@echo "  format               - black the tree"
	@echo "  ci                   - lint + test, exactly what GitHub Actions runs"
	@echo ""
	@echo "Quality reports (in reports/):"
	@echo "  quality              - Coverage, cyclomatic complexity & CRAP"
	@echo "  coverage             - Terminal, HTML, JSON & XML coverage"
	@echo "  coverage-check       - Fail below our 58% coverage floor (CI runs this)"
	@echo "  cc                   - Function complexity, ranked text & JSON"
	@echo "  crap                 - Fresh coverage plus ranked CRAP risk"
	@echo "  Override PYTHON, REPORT_DIR, TEST_PATHS or COVERAGE_MIN as needed"
	@echo "  CRAP uses statement coverage: CC squared * (1 - coverage)^3 + CC"
	@echo ""
	@echo "Machine learning & games (network where noted):"
	@echo "  classifier-check     - Smoke test our classifier model (network)"
	@echo "  jev-bench            - Self-play Jev Reasoner's probability grid (no network)"
	@echo "  arena                - Every battleship mode plays every other (network)"
	@echo "  test-artifact        - Compile C on a code executor & run the binary (network)"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean                - Remove caches & compiled files"
	@echo "  clean-all            - clean, then remove venv/"

# ============================================================================
# SETUP
# ============================================================================

# venv/.installed is rebuilt whenever a requirements file changes, so a
# pulled dependency lands on the next make without a manual reinstall.
.PHONY: venv dev-setup
venv: venv/.installed
dev-setup: venv/.installed

venv/.installed: requirements.txt requirements-test.txt
	@echo "🚀 Preparing venv/..."
	test -d venv || python3 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt -r requirements-test.txt
	touch venv/.installed

.PHONY: init-db
init-db: venv
	@echo "🗄️ Initializing database tables..."
	@if [ -f vars.sh ]; then \
		. ./vars.sh && venv/bin/python init_db.py && \
		echo "✅ Database tables created successfully"; \
	else \
		echo "❌ Error: vars.sh not found. Please create it from vars.sh.sample"; \
		exit 1; \
	fi

# ============================================================================
# TESTS
# ============================================================================

.PHONY: test
test: test-unit test-integration test-functional validate-yaml
	@echo "🎉 All tests passed"

.PHONY: test-unit
test-unit: venv
	@echo "🔬 Running unit tests..."
	venv/bin/python -m pytest tests/unit/

.PHONY: test-integration
test-integration: venv
	@echo "🔗 Running integration tests..."
	venv/bin/python -m pytest tests/integration/

.PHONY: test-functional
test-functional: venv
	@echo "⚡ Running functional tests..."
	venv/bin/python -m pytest tests/functional/

.PHONY: test-ui
test-ui: venv
	@echo "📱 Running page contract tests..."
	venv/bin/python -m pytest tests/functional/test_ui_contract.py tests/functional/test_auth_ui_regressions.py

.PHONY: test-validator
test-validator: venv
	@echo "📋 Running YAML validator tests..."
	venv/bin/python -m pytest tests/unit/test_activity_yaml_validator.py

.PHONY: test-yaml-loading
test-yaml-loading: venv
	@echo "📄 Running YAML loading/parsing tests..."
	venv/bin/python -m pytest tests/unit/test_yaml_loading.py

.PHONY: test-activity-flows
test-activity-flows: venv
	@echo "🔄 Running activity flow tests..."
	venv/bin/python -m pytest tests/functional/test_activity_flows.py

.PHONY: test-battleship
test-battleship: venv
	@echo "🚢 Running battleship tests..."
	venv/bin/python -m pytest tests/unit/test_battleship_modes.py tests/unit/test_jev_hunter.py tests/functional/test_battleship_pre_script.py tests/functional/test_battleship_game_flow.py

.PHONY: test-guarded-ai
test-guarded-ai: venv
	@echo "🛡️ Running guarded_ai.py tests..."
	venv/bin/python -m pytest tests/unit/test_guarded_ai_functions.py tests/functional/test_guarded_ai.py

.PHONY: test-multiple-files
test-multiple-files: venv
	@echo "📁 Running integration tests across all activity files..."
	venv/bin/python -m pytest tests/integration/test_multiple_activities.py

.PHONY: validate-yaml
validate-yaml: venv
	@echo "📋 Validating all YAML files..."
	venv/bin/python activity_yaml_validator.py research/*.yaml

# ============================================================================
# LINT & CI
# ============================================================================

# black's excludes live in pyproject.toml, flake8's in .flake8.
.PHONY: lint
lint: venv
	@echo "🔍 Linting code..."
	venv/bin/black --check .
	venv/bin/flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

.PHONY: format
format: venv
	@echo "🎨 Formatting code..."
	venv/bin/black .

.PHONY: ci
ci: lint test
	@echo "🚀 CI passed: lint, unit, integration, functional & YAML validation"

# ============================================================================
# QUALITY REPORTS
# ============================================================================

# Production-only metrics.
PYTHON ?= venv/bin/python
REPORT_DIR ?= reports
QUALITY_SOURCES := activity.py activity_utils.py activity_yaml_validator.py app.py auth.py init_db.py models.py research/guarded_ai.py routes/__init__.py routes/accounts.py routes/code.py routes/pages.py routes/rooms.py
TEST_PATHS ?= tests/
COVERAGE_MIN ?= 0

.PHONY: quality-setup test-cov coverage cc crap quality
quality-setup: venv

test-cov: coverage
coverage: venv
	@mkdir -p $(REPORT_DIR)
	COVERAGE_FILE=$(REPORT_DIR)/.coverage $(PYTHON) -m pytest $(TEST_PATHS) --cov --cov-config=.coveragerc --cov-report=term-missing --cov-report=html:$(REPORT_DIR)/htmlcov --cov-report=json:$(REPORT_DIR)/coverage.json --cov-report=xml:$(REPORT_DIR)/coverage.xml --cov-fail-under=$(COVERAGE_MIN)

# CI's floor: fails when coverage of our own code (vendored un.py excluded,
# see .coveragerc) drops below 58%. It was 59.7% on 2026-09-24; raise the
# number as tests land, never lower it.
.PHONY: coverage-check
coverage-check: venv
	venv/bin/python -m pytest tests/ -q --cov --cov-config=.coveragerc --cov-report=term --cov-fail-under=58

cc: venv
	$(PYTHON) quality_report.py $(QUALITY_SOURCES) --output $(REPORT_DIR)/cc

# Always regenerate coverage; shared prerequisite runs once even with make -j.
crap: coverage
	$(PYTHON) quality_report.py $(QUALITY_SOURCES) --coverage $(REPORT_DIR)/coverage.json --output $(REPORT_DIR)/crap

quality: cc crap

# ============================================================================
# CLEANUP
# ============================================================================

.PHONY: clean
clean:
	@echo "🧹 Cleaning generated files..."
	find . -path ./venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -path ./venv -prune -o -name "*.py[co]" -delete 2>/dev/null || true
	find . -path ./venv -prune -o -name "*~" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ htmlcov/ .coverage reports/

.PHONY: clean-cache
clean-cache: clean

.PHONY: clean-all
clean-all: clean
	@echo "💣 Removing virtual environment..."
	rm -rf venv

# ============================================================================
# CODE EXECUTOR API TESTING
# ============================================================================

# Test artifact retrieval - compile C code, get base64 binary, decode and test execution
# URL can be overridden: make test-artifact URL=https://code.ai.unturf.com
.PHONY: test-artifact
test-artifact:
	$(eval URL ?= http://127.0.0.1:8080)
	@echo "=========================================="
	@echo "Testing Binary Artifact Retrieval"
	@echo "=========================================="
	@echo "API: $(URL)"
	@echo ""
	@echo "Step 1: Compiling C code and retrieving base64 binary..."
	@curl -s -X POST $(URL)/execute \
		-H "Content-Type: application/json" \
		-d '{"language": "c", "code": "#include <stdio.h>\nint main() { printf(\"Hello from artifact!\\n\"); return 0; }", "return_artifact": true}' \
		| jq -r '.stdout.artifact.data' > /tmp/artifact.b64
	@echo "✓ Base64 artifact saved to /tmp/artifact.b64"
	@echo "  Size: $$(wc -c < /tmp/artifact.b64) bytes (base64)"
	@echo ""
	@echo "Step 2: Decoding base64 to binary..."
	@base64 -d /tmp/artifact.b64 > /tmp/artifact_binary
	@chmod +x /tmp/artifact_binary
	@echo "✓ Binary decoded to /tmp/artifact_binary"
	@echo "  Size: $$(wc -c < /tmp/artifact_binary) bytes (ELF binary)"
	@echo ""
	@echo "Step 3: Verifying ELF binary..."
	@file /tmp/artifact_binary
	@echo ""
	@echo "Step 4: Executing binary..."
	@/tmp/artifact_binary
	@echo ""
	@echo "✓ Artifact test complete!"
	@echo ""
	@echo "Cleanup: rm /tmp/artifact.b64 /tmp/artifact_binary"

# Smoke test a classifier model (a decision endpoint, see classifier.py):
# lists each instance's models & asks one sample categorization. The key
# never leaves the request builder.
.PHONY: classifier-check
classifier-check: venv
	@echo "🧭 Checking our classifier model..."
	venv/bin/python classifier.py

# Self-play benchmark of Jev Reasoner's exact placement-density grid
# (jev_hunter.py), grid only, no network: mean shots to sink a random fleet.
.PHONY: jev-bench
jev-bench: venv
	@echo "🎯 Self-playing Jev Reasoner's grid..."
	OPENCOMPLETION_CLASSIFIER=off venv/bin/python jev_hunter.py 200 0

# Round robin of every battleship admiral (battleship_arena.py): 10 games
# per pair plus solo clears, one JSON of results & a text table. The two
# model-backed modes use MODEL_1 & the classifier from the environment;
# source vars.sh first for the real thing.
.PHONY: arena
arena: venv
	@echo "⚓ Running our battleship arena..."
	PYTHONUNBUFFERED=1 venv/bin/python battleship_arena.py --games 10 --seed 0 --out arena_results.json
