# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Seer is Sentry's AI service providing ML/AI capabilities including:
- **Autofix**: AI agent that identifies root causes and suggests fixes for Sentry issues
- **Codegen**: AI-powered code generation (unit tests, PR reviews)
- **Issue Summarization**: Automated issue analysis
- **Anomaly Detection**: Time-series anomaly detection for alerts
- **Grouping/Severity**: Issue classification and severity prediction

**Tech Stack**: Python 3.11, Flask, Celery, PostgreSQL with pgvector, RabbitMQ, Docker

## Common Commands

```bash
# Development
make update          # Build images & apply database migrations
make dev             # Start dev environment (port 9091)
make shell           # Open bash shell in container

# Testing
make test            # Run all tests with coverage
docker compose up -d test-db  # Start test database (required for individual tests)
# Then in shell: pytest tests/path/to/test.py::test_name

# Code quality
make mypy            # Type checking

# Database
make migration       # Create new migration
make db-downgrade    # Downgrade one version
make db-reset        # Drop and recreate database (run make update after)

# VCR cassettes (for HTTP request recording)
make vcr-encrypt-prep  # First time setup
make vcr-encrypt       # Encrypt before commit
make vcr-decrypt       # Decrypt for running tests

# Dependencies
make upgrade-package-versions  # Update requirements.txt from constraints
```

## Architecture

### Directory Structure
```
src/seer/
├── automation/           # AI automation features
│   ├── autofix/         # Autofix agent
│   ├── codegen/         # Code generation
│   ├── agent/           # LLM agent client (multi-provider: Anthropic, OpenAI, Google)
│   ├── codebase/        # Repository management
│   └── summarize/       # Issue summarization
├── anomaly_detection/   # Time-series anomaly detection
├── grouping/            # Issue grouping
├── severity/            # Severity prediction
├── app.py               # Flask application
├── db.py                # Database models & ORM
├── configuration.py     # AppConfig (Pydantic BaseModel)
├── dependency_injection.py  # Custom DI framework
└── bootup.py            # Application initialization
```

### Dependency Injection System

Seer uses a custom DI framework (see `DI.md` for full documentation). Key patterns:

```python
from seer.dependency_injection import Module, inject, injected

module = Module()
module.enable()

@module.provider
def get_config() -> AppConfig:
    return AppConfig()

@inject
def my_function(config: AppConfig = injected):
    # config automatically resolved from enabled module
    pass
```

**Testing with DI**: Override providers using context managers:
```python
with Module().constant(Settings, Settings(DISABLE=True)):
    do_logic()  # Uses overridden Settings
```

### LLM Client Architecture (`automation/agent/client.py`)
- Multi-provider support: Anthropic (Claude), OpenAI, Google Gemini
- Unified interface via `BaseLlmProvider`
- Streaming support with timeout handling
- Region preferences and fallback logic
- Langfuse integration for observability

## Testing Guidelines

1. **Use real database, not mocks** - Test DB available via `docker compose up -d test-db`
2. **Test actual functionality** - Avoid testing mocks or logging
3. **Avoid `caplog` for log assertions** - Makes tests flaky
4. **Use DI for test isolation** - Override providers in test context

```bash
# Run individual tests
docker compose up -d test-db
make shell
pytest tests/path/to/test.py::test_name -vv
```

### VCR Cassettes
VCR records HTTP requests for replay in tests. Cassettes must be encrypted before commit:
```bash
make vcr-decrypt      # Decrypt to run tests
make vcr-encrypt      # Encrypt before commit (CLEAN=1 by default)
```

## Code Style

- **Line length**: 100 characters
- **Formatter**: Black
- **Import sorting**: isort (profile: black)
- **Type checker**: mypy (minimal strictness, disallow_incomplete_defs)

Pre-commit hooks handle formatting automatically. Run manually:
```bash
pre-commit run --files <files>
```

## Environment Setup

Required environment variables (set in `.env`, copy from `.env.example`):
- `OPENAI_API_KEY`: For OpenAI LLM calls
- `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_HOST`: Observability
- `GITHUB_TOKEN` or `GITHUB_PRIVATE_KEY`+`GITHUB_APP_ID`: GitHub integration

Optional:
- `NO_REAL_MODELS=1`: Use model stubs in development
- `NO_SENTRY_INTEGRATION=1`: Disable local Sentry integration
- `DEV=1`: Enable development mode

### Model Artifacts
```bash
gsutil cp -r gs://sentry-ml/seer/models .
# If auth issues: gcloud auth login && gcloud auth application-default login
```

## Sentry Integration

Seer runs on port 9091 (default Sentry dev expects this). For local Sentry integration, add to `~/.sentry/sentry.conf.py`:
```python
SEER_RPC_SHARED_SECRET = ["seers-also-very-long-value-haha"]
SENTRY_FEATURES["organizations:gen-ai-features"] = True
SENTRY_FEATURES["organizations:gen-ai-consent"] = True
```

## Services

Docker Compose runs via supervisord:
- **app**: Flask application (port 9091)
- **db**: PostgreSQL with pgvector (port 5433)
- **test-db**: Separate test database
- **rabbitmq**: Message broker (ports 5672, 15672)
- **celeryworker**: Celery worker (auto-restart in DEV mode)
- **celerybeat**: Celery beat scheduler
- **flower**: Celery dashboard (http://localhost:5555 when DEV=1)
