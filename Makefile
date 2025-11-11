# Makefile for OpenCompletion Testing Framework

.PHONY: help
help:
	@echo "OpenCompletion Testing Framework"
	@echo "================================"
	@echo ""
	@echo "🧪 Test Commands:"
	@echo "  test                  - Run all tests (unit, integration, functional)"
	@echo "  test-unit            - Run only unit tests"
	@echo "  test-integration     - Run only integration tests"  
	@echo "  test-functional      - Run only functional tests"
	@echo "  test-validator       - Run YAML validator tests"
	@echo "  test-yaml-loading    - Run YAML loading/parsing tests"
	@echo "  test-activity-flows  - Run activity flow tests"
	@echo "  test-battleship      - Run battleship game tests"
	@echo "  test-guarded-ai      - Run guarded_ai.py functionality tests"
	@echo "  test-multiple-files  - Run integration tests across all activity files"
	@echo ""
	@echo "📋 Validation Commands:"
	@echo "  validate-yaml        - Validate all YAML files in research/"
	@echo ""
	@echo "🛠️ Development Commands:"
	@echo "  venv                 - Create virtual environment and install dependencies"
	@echo "  dev-setup           - Install development dependencies"
	@echo "  lint                - Run code linting and formatting"
	@echo "  clean               - Clean up generated files"
	@echo "  clean-all           - Remove virtual environment"

# Setup virtual environment
.PHONY: venv
venv:
	@if [ ! -d "venv" ]; then \
		echo "🚀 Creating virtual environment..."; \
		python3 -m venv venv; \
		echo "📦 Installing basic dependencies..."; \
		venv/bin/pip install --upgrade pip; \
		venv/bin/pip install -r requirements.txt || echo "⚠️ Failed to install basic dependencies"; \
		echo "✅ Virtual environment ready!"; \
	else \
		echo "✅ Virtual environment already exists"; \
	fi

# ============================================================================
# MAIN TEST COMMANDS
# ============================================================================

# Run all tests
.PHONY: test
test: test-unit test-integration test-functional test-validator test-yaml-loading test-activity-flows test-battleship test-guarded-ai test-multiple-files validate-yaml
	@echo ""
	@echo "🎉 All tests completed!"
	@echo "📊 Test Summary:"
	@echo "   ✅ Unit tests - Core functionality"
	@echo "   ✅ Integration tests - Cross-component testing"  
	@echo "   ✅ Functional tests - End-to-end workflows"
	@echo "   ✅ YAML validation - All activity files"
	@echo "   ✅ All specific test targets completed"

# Run unit tests only  
.PHONY: test-unit
test-unit: venv
	@echo "🔬 Running unit tests..."
	@if command -v pytest >/dev/null 2>&1; then \
		python -m pytest tests/unit/ -v --tb=short; \
	else \
		echo "📝 Running unit tests directly..."; \
		python tests/unit/test_yaml_loading.py; \
		python tests/unit/test_activity_yaml_validator.py; \
	fi

# Run integration tests only
.PHONY: test-integration  
test-integration: venv
	@echo "🔗 Running integration tests..."
	@if command -v pytest >/dev/null 2>&1; then \
		python -m pytest tests/integration/ -v --tb=short; \
	else \
		echo "📝 Running integration tests directly..."; \
		python tests/integration/test_multiple_activities.py; \
	fi

# Run functional tests only
.PHONY: test-functional
test-functional: venv
	@echo "⚡ Running functional tests..."
	@if command -v pytest >/dev/null 2>&1; then \
		python -m pytest tests/functional/ -v --tb=short; \
	else \
		echo "📝 Running functional tests directly..."; \
		python tests/functional/test_activity_flows.py; \
		python tests/functional/test_battleship_pre_script.py; \
	fi

# ============================================================================
# SPECIFIC TEST COMMANDS
# ============================================================================

# Run YAML validator tests only
.PHONY: test-validator
test-validator: venv
	@echo "📋 Running YAML validator tests..."
	python tests/unit/test_activity_yaml_validator.py

# Run YAML loading tests only
.PHONY: test-yaml-loading
test-yaml-loading: venv
	@echo "📄 Running YAML loading/parsing tests..."
	python tests/unit/test_yaml_loading.py

# Run activity flow tests
.PHONY: test-activity-flows
test-activity-flows: venv
	@echo "🔄 Running activity flow tests..."
	python tests/functional/test_activity_flows.py

# Run battleship game tests
.PHONY: test-battleship
test-battleship: venv
	@echo "🚢 Running battleship game tests..."
	python tests/functional/test_battleship_pre_script.py

# Run guarded_ai functionality tests  
.PHONY: test-guarded-ai
test-guarded-ai: venv
	@echo "🛡️ Running guarded_ai.py functionality tests..."
	python tests/integration/test_regression_fixes.py

# Run integration tests across all activity files
.PHONY: test-multiple-files
test-multiple-files: venv
	@echo "📁 Running integration tests across all activity files..."
	python tests/integration/test_multiple_activities.py

# ============================================================================
# VALIDATION COMMANDS
# ============================================================================

# Validate all YAML files
.PHONY: validate-yaml
validate-yaml: venv
	@echo "📋 Validating all YAML files..."
	python activity_yaml_validator.py research/*.yaml

# ============================================================================
# DEVELOPMENT AND CI/CD COMMANDS
# ============================================================================

# Run tests with coverage (requires pytest and coverage)
.PHONY: test-cov
test-cov: dev-setup
	@echo "📊 Running tests with coverage..."
	venv/bin/pip install pytest-cov
	venv/bin/python -m pytest tests/ --cov=. --cov-report=html --cov-report=term-missing -v


# Format and lint code  
.PHONY: format
format: dev-setup
	@echo "🎨 Formatting code..."
	venv/bin/black .
	venv/bin/isort .

.PHONY: lint
lint: dev-setup
	@echo "🔍 Linting code..."
	venv/bin/black --check .
	venv/bin/isort --check-only .
	venv/bin/flake8 .
# Install development dependencies
.PHONY: dev-setup
dev-setup: venv
	@echo "🛠️  Installing development dependencies..."
	venv/bin/pip install black flake8 isort pytest coverage
	@echo "✅ Development environment ready!"

# ============================================================================
# CI/CD AND AUTOMATION COMMANDS
# ============================================================================

# Full CI pipeline
.PHONY: ci
ci: clean test validate-yaml lint
	@echo ""
	@echo "🎯 CI Pipeline Results:"
	@echo "   ✅ Tests passed"
	@echo "   ✅ YAML validation passed" 
	@echo "   ✅ Code linting completed"
	@echo "🚀 Ready for deployment!"


# ============================================================================
# UTILITY COMMANDS
# ============================================================================

# Clean generated files
.PHONY: clean
clean:
	@echo "🧹 Cleaning generated files..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.pyo" -delete 2>/dev/null || true
	find . -name "*~" -delete 2>/dev/null || true

.PHONY: init-db
init-db:
	@echo "🗄️ Initializing database tables..."
	@if [ -f vars.sh ]; then \
		. ./vars.sh && python init_db.py; \
		echo "✅ Database tables created successfully"; \
	else \
		echo "❌ Error: vars.sh not found. Please create it from vars.sh.sample"; \
		exit 1; \
	fi

clean-cache:
	rm -rf .pytest_cache/ 2>/dev/null || true
	rm -rf htmlcov/ 2>/dev/null || true
	rm -rf .coverage 2>/dev/null || true
	rm -rf *.tmp 2>/dev/null || true

# Remove virtual environment
.PHONY: clean-all
clean-all: clean
	@echo "💣 Removing virtual environment..."
	rm -rf venv

# Show test structure
.PHONY: test-info
test-info:
	@echo "📁 Test Structure:"
	@echo "   tests/"
	@echo "   ├── unit/                    - Unit tests for individual components"
	@echo "   │   ├── test_yaml_loading.py           - YAML loading/parsing tests"
	@echo "   │   └── test_activity_yaml_validator.py - Validator functionality tests"
	@echo "   ├── integration/             - Integration tests across components"
	@echo "   │   ├── test_multiple_activities.py    - Tests across all activity files"
	@echo "   │   └── test_regression_fixes.py       - Regression and fix validation"
	@echo "   └── functional/              - End-to-end functional tests"
	@echo "       ├── test_activity_flows.py         - Complete activity workflows"
	@echo "       └── test_battleship_pre_script.py  - Battleship game functionality"
	@echo ""
	@echo "🎯 Key Test Commands:"
	@echo "   make test           - Run all tests"
	@echo "   make validate-yaml  - Validate all YAML files"
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
