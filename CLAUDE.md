# Daisy Search

Personal identity exposure / OSINT research system.
Operated via Claude Code or Claude Desktop as an MCP server.

## CRITICAL RULES
- NEVER run any git commands (status, log, diff, add, commit, push, etc.) unless the user explicitly says to in that message
- NEVER ask to commit, suggest committing, or mention git in any way
- NEVER propose or suggest git operations — the user will tell you when they want to use git

## Tech Stack
- Python 3.11, FastAPI, SQLAlchemy 2.x (async), Alembic, Postgres
- MCP server for Claude Code / Claude Desktop integration
- Package manager: UV (never use pip/pip install directly)

## OSINT Scripts
- `scripts/leakcheck.sh <email|phone|username> ...` — LeakCheck.io breach database lookup
  - Reads `LEAKCHECK_API_KEY` from `~/.secrets/api_keys.sh`
  - Run: `source ~/.secrets/api_keys.sh && scripts/leakcheck.sh "<email>"`
  - Supports: email, phone (digits only, no +1), username
  - Returns: breach sources, exposed fields, plaintext passwords, PII (NPD has SSN/address history)
  - Multiple queries in one call: `scripts/leakcheck.sh "<email>" "<email>" "<phone>"`

## Commands
- `uv sync` -- install/sync dependencies
- `uv run daisy-mcp` -- start the MCP server (stdio)
- `uv run python -m daisy.cli` -- CLI for testing/debugging
- `uv run uvicorn daisy.api.app:app --reload` -- run web dashboard
- `uv run alembic upgrade head` -- run migrations
- `uv run alembic revision --autogenerate -m "description"` -- create migration
- `uv run pytest` -- run tests
- `uv run ruff check src/` -- lint
- `uv run ruff check --fix src/` -- lint and auto-fix
- `docker compose up -d` -- start Postgres + Redis

## Project Layout
- `src/daisy/mcp_server.py` -- MCP server with all research tools
- `src/daisy/constants.py` -- all enums, magic strings, default values
- `src/daisy/config.py` -- pydantic-settings, env var overrides
- `src/daisy/models/` -- SQLAlchemy ORM models (project, subject, fact, source, job)
- `src/daisy/workers/` -- legacy worker classes (intake helpers still used)
- `src/daisy/queue/` -- legacy job dispatch (kept for optional batch mode)
- `src/daisy/api/` -- FastAPI web dashboard (read-only presentation)
- `src/daisy/cli.py` -- CLI for testing outside of Claude
- `migrations/` -- Alembic migrations

## MCP Server
- 13 tools: daisy_projects, daisy_new_project, daisy_switch_project, daisy_archive_project, daisy_intake, daisy_search, daisy_fetch, daisy_extract, daisy_add_fact, daisy_facts, daisy_confirm, daisy_report, daisy_sources
- All research tools scoped to the active project
- Extraction priority: Ollama > Anthropic API > return content for in-context extraction
- Methodology guide loaded as MCP resource at daisy://methodology

## Conventions
- All DB access is async (asyncpg). Alembic uses sync (psycopg2).
- Facts are immutable records. Only lifecycle_state is mutable, logged in fact_events.
- NEVER hardcode magic strings. Use StrEnum constants from constants.py.
- LLM model IDs use aliases (claude-sonnet-4-6) not dated versions.
- Config loaded from .env via pydantic-settings.
- Projects are the top-level container. All facts/subjects scoped to a project.

## Database
- Local dev: `docker compose up -d` starts Postgres on 5432 and Redis on 6379
- DB name: daisy, user: daisy, password: daisy_dev
- Always create Alembic migrations for schema changes

## Architecture
- Claude Code/Desktop = orchestrator (drives research via MCP tools)
- MCP server = thin tool layer (DB + search + fetch + extract)
- Postgres = shared brain (persists across sessions)
- Web dashboard = read-only presentation layer
- Tethernet = browser automation for auth-walled pages (Claude Code only)
- See WRINKLES.md for open architectural issues
