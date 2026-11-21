# OpenCompletion tests

Every command here is a target in our `Makefile` (`make help` lists them all).
No test needs the network, an API key or `vars.sh`: `conftest.py` pins our
classifier, follow-ups & web tools off, and model calls are patched.

## Quick start

```bash
make venv    # create venv/ & install app + test dependencies
make test    # unit, integration, functional & YAML validation
make lint    # black --check & flake8
make ci      # lint + test, exactly what GitHub Actions runs
```

## Layout

```
tests/
├── conftest.py     # shared setup: placeholder model, knobs pinned off
├── unit/           # one module at a time
│   └── crawler/    # our vendored crawler's own suite (never edit here)
├── integration/    # a real Flask app & database: chat, rooms, chains, tools, migrations
├── functional/     # whole flows: pages, sign in, streaming, games; *_ui.py drive headless Chromium
└── fixtures/       # invalid YAML & known answers shared with upstream (provenance, toolcall_repair)
```

## Targets

| target | runs |
|--------|------|
| `make test` | `test-unit`, `test-integration`, `test-functional`, `validate-yaml` |
| `make test-unit` | `tests/unit/` |
| `make test-integration` | `tests/integration/` |
| `make test-functional` | `tests/functional/` |
| `make test-ui` | page contract & mobile layouts in headless Chromium |
| `make test-validator` | YAML validator tests |
| `make test-yaml-loading` | YAML loading & parsing tests |
| `make test-activity-flows` | activity flow tests |
| `make test-battleship` | battleship modes, Jev Reasoner & game flow |
| `make test-guarded-ai` | `research/guarded_ai.py` tests |
| `make test-multiple-files` | integration tests across every activity file |
| `make validate-yaml` | our validator over every `research/*.yaml` |
| `make activity-walk` | every activity played with dice, no model |
| `make coverage` | coverage reports in `reports/` (`make test-cov` is the same) |
| `make coverage-check` | fails below our coverage floor (CI runs this) |

One file or one test:

```bash
venv/bin/python -m pytest tests/unit/test_jev_hunter.py
venv/bin/python -m pytest tests/unit/test_jev_hunter.py -k grid
venv/bin/python activity_yaml_validator.py research/activity26-magic-8-ball.yaml
```

## Before a commit

1. `make lint` (`make format` runs black over our tree)
2. `make test`
3. For a changed activity: `make validate-yaml` & `make activity-walk`

## Troubleshooting

- A broken or stale venv: `make clean-all`, then `make venv`.
- Import errors: run from our repo root, through `make` or `venv/bin/python -m pytest`.
- `make test-ui` needs Chromium or Google Chrome installed.
