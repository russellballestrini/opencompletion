# Makefile for OpenCompletion Testing Framework

.PHONY: help
help:
	@echo "OpenCompletion Testing Framework"
	@echo "================================"
	@echo ""
	@echo "Available targets:"
	@echo "  venv       - Create virtual environment and install dependencies"
	@echo "  test       - Run all tests"
	@echo "  test-unit  - Run only unit tests"
	@echo "  test-integration - Run only integration tests"
	@echo "  test-functional - Run only functional tests"
	@echo "  test-validator - Run only YAML validator tests"
	@echo "  validate-yaml - Validate all YAML files in research/"
	@echo "  lint       - Run code linting"
	@echo "  clean      - Clean up generated files"
	@echo "  clean-all  - Remove virtual environment"

# Setup virtual environment
.PHONY: venv
venv:
	@echo "🚀 Creating virtual environment..."
	python3 -m venv venv
	@echo "📦 Installing dependencies..."
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt
	venv/bin/pip install -r requirements-test.txt
	@echo "✅ Virtual environment ready!"

# Run all tests
.PHONY: test
test: venv
	@echo "🧪 Running all tests..."
	venv/bin/python -m pytest tests/ -v --tb=short
	@echo "📋 Validating YAML files..."
	venv/bin/python activity_yaml_validator.py research/*.yaml || true

# Run unit tests only
.PHONY: test-unit
test-unit:
	@echo "🔬 Running unit tests..."
	venv/bin/python -m pytest tests/unit/ -v --tb=short

# Run integration tests only
.PHONY: test-integration
test-integration:
	@echo "🔗 Running integration tests..."
	venv/bin/python -m pytest tests/integration/ -v --tb=short

# Run functional tests only
.PHONY: test-functional
test-functional:
	@echo "⚡ Running functional tests..."
	venv/bin/python -m pytest tests/functional/ -v --tb=short

# Run YAML validator tests only
.PHONY: test-validator
test-validator:
	@echo "📋 Running YAML validator tests..."
	venv/bin/python -m pytest tests/unit/test_activity_yaml_validator.py -v --tb=short

# Validate YAML files
.PHONY: validate-yaml
validate-yaml:
	@echo "📋 Validating YAML files..."
	venv/bin/python activity_yaml_validator.py research/*.yaml

# Run tests with coverage
.PHONY: test-cov
test-cov:
	@echo "🧪 Running tests with coverage..."
	venv/bin/python -m pytest tests/ --cov=. --cov-report=html --cov-report=term-missing -v

# Format and lint code (combined target)
.PHONY: format lint
format lint:
	@echo "🎨 Formatting and linting code..."
	venv/bin/pip install black isort flake8 || true
	venv/bin/black .
	venv/bin/isort .
	venv/bin/flake8 . || echo "⚠️  Linting issues found"

# Clean generated files
.PHONY: clean
clean:
	@echo "🧹 Cleaning generated files..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.pyo" -delete 2>/dev/null || true
	find . -name "*~" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ 2>/dev/null || true
	rm -rf htmlcov/ 2>/dev/null || true
	rm -rf .coverage 2>/dev/null || true

# Remove virtual environment
.PHONY: clean-all
clean-all: clean
	@echo "💣 Removing virtual environment..."
	rm -rf venv

# Quick test run (for development)
.PHONY: quick
quick:
	@echo "⚡ Quick test run..."
	venv/bin/python -m pytest tests/unit/test_activity_yaml_validator.py -v -x

# Install development dependencies
.PHONY: dev-setup
dev-setup: venv
	@echo "🛠️  Installing development dependencies..."
	venv/bin/pip install black flake8 isort mypy pre-commit
	@echo "✅ Development environment ready!"