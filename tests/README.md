# OpenCompletion Testing Framework

Comprehensive testing suite for OpenCompletion with unit tests, integration tests, functional tests, and YAML validation.

## Quick Start

```bash
# Setup testing environment
make setup

# Run all tests
make test

# Run specific test types
make test-unit
make test-integration 
make test-functional
make test-validator
make test-yaml-loading
make test-activity-flows
make test-battleship
make test-guarded-ai
make test-multiple-files

# Validate YAML files
make validate-yaml
```

## Test Structure

```
tests/
├── unit/                    # Unit tests for individual functions
│   ├── test_app.py         # Tests for app.py core functions
│   └── test_activity_yaml_validator.py  # Tests for YAML validator
├── integration/            # Integration tests for complete flows
│   └── test_activity_processing.py      # Activity processing integration
├── functional/             # End-to-end functional tests
│   └── test_battleship_game_flow.py    # Complete battleship game scenarios
└── fixtures/               # Test data and invalid samples
    └── test_invalid.yaml   # Intentionally invalid YAML for testing
```

## Test Categories

### Unit Tests (`tests/unit/`)

**test_app.py** - Tests core app.py functions:
- Utility functions (client management, S3 operations)  
- Activity processing functions (script execution, metadata operations)
- Response categorization and feedback generation
- Translation and language handling
- Navigation between activity steps

**test_activity_yaml_validator.py** - Tests YAML validator:
- YAML syntax validation
- Schema compliance checking  
- Metadata operations validation
- Python code syntax checking
- Terminal step validation
- Logic flow validation

### Integration Tests (`tests/integration/`)

**test_activity_processing.py** - Tests complete activity workflows:
- End-to-end activity processing
- Script execution with metadata updates
- Pre-script and post-script integration
- Navigation between sections and steps
- Error handling and recovery

### Functional Tests (`tests/functional/`)

**test_battleship_game_flow.py** - Tests complete battleship game scenarios:
- Game setup and board generation
- Shot processing and hit detection
- Ship sinking logic
- AI behavior (random, hunter, super hunter modes)
- Win condition detection
- Edge case handling

## Features Tested

### YAML Validation
- ✅ Syntax validation
- ✅ Schema compliance  
- ✅ Required fields checking
- ✅ Metadata operations (`metadata_add`, `metadata_remove`, `metadata_feedback_filter`, etc.)
- ✅ Terminal step validation (no questions in final steps)
- ✅ Python code syntax checking
- ✅ Logic flow validation
- ✅ Transition validation

### Core Application Features
- ✅ Activity loading (local files and S3)
- ✅ Script execution with metadata manipulation
- ✅ Response categorization using AI
- ✅ Feedback generation
- ✅ Multi-language support and translation
- ✅ Step navigation and flow control
- ✅ Error handling and recovery

### Battleship Game Logic
- ✅ Board generation and ship placement
- ✅ Shot processing and validation
- ✅ Hit/miss detection
- ✅ Ship sinking logic
- ✅ AI opponent behavior (multiple difficulty levels)
- ✅ Win/lose conditions
- ✅ Game state consistency validation

## Running Tests

### All Tests
```bash
make test
```
Runs all unit, integration, and functional tests, plus YAML validation.

### Specific Test Categories
```bash
make test-unit          # Unit tests only
make test-integration   # Integration tests only  
make test-functional    # Functional tests only
make test-validator     # YAML validator tests only
make test-yaml-loading  # YAML loading/parsing tests
make test-activity-flows # Activity flow tests
make test-battleship    # Battleship game tests
make test-guarded-ai    # Guarded AI functionality tests
make test-multiple-files # Integration tests across all activity files
```

### YAML Validation
```bash
make validate-yaml      # Validate all research/*.yaml files
```

### With Coverage
```bash
make test-cov          # Run tests with coverage report
```

### Quick Development Testing
```bash
make quick             # Fast test run for development
```

## Test Configuration

### Virtual Environment
Tests run in an isolated virtual environment with all necessary dependencies:
- pytest, pytest-cov, pytest-mock, pytest-flask
- pyyaml, requests, flask, flask-socketio
- gevent, eventlet, boto3, openai

### Mocking Strategy
- External APIs (OpenAI, S3) are mocked to avoid API calls during testing
- Database operations are mocked to avoid needing a real database
- Socket.IO events are mocked for testing real-time features

### Test Data
- **Valid YAML**: Real battleship configuration files
- **Invalid YAML**: Intentionally broken files in `tests/fixtures/`
- **Mock Game States**: Simulated battleship game states for testing
- **Sample Scripts**: Python scripts for testing execution

## Continuous Integration

The testing framework is designed for CI/CD integration:

```yaml
# Example GitHub Actions workflow
- name: Setup and Test
  run: |
    make setup
    make test
    make validate-yaml
```

## Development Workflow

1. **Before committing**: Run `make test` to ensure all tests pass
2. **Adding new features**: Write tests in the appropriate category
3. **YAML changes**: Run `make validate-yaml` to check syntax
4. **Code formatting**: Run `make format` to format and lint code

## Test Coverage

Current test coverage includes:
- **YAML Validator**: 17 test cases covering all validation scenarios
- **Core App Functions**: Comprehensive testing of utility and processing functions
- **Activity Processing**: End-to-end workflow testing
- **Battleship Logic**: Complete game scenario testing

## Troubleshooting

### Common Issues

**Virtual environment not found**:
```bash
make clean-all  # Remove old venv
make setup      # Create new venv
```

**Import errors**:
```bash
# Ensure you're in the project root directory
cd /path/to/opencompletion
make test
```

**YAML validation errors**:
```bash
# Check specific file
venv/bin/python activity_yaml_validator.py research/problematic-file.yaml
```

## Adding New Tests

### Unit Test Example
```python
def test_new_function(self):
    """Test description"""
    result = app.new_function("input")
    self.assertEqual(result, "expected")
```

### Integration Test Example  
```python
def test_new_workflow(self):
    """Test complete workflow"""
    with patch('app.external_dependency'):
        result = complete_workflow()
        self.assertTrue(result.success)
```

### Functional Test Example
```python
def test_new_game_scenario(self):
    """Test complete game scenario"""
    game_state = setup_game()
    result = play_complete_game(game_state)
    self.assertEqual(result.winner, "user")
```

## Contributing

1. Write tests for all new features
2. Ensure tests pass: `make test`
3. Follow existing patterns and naming conventions
4. Update this README if adding new test categories