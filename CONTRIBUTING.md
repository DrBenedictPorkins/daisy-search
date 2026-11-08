# Contributing

Thanks for taking a look. Daisy is a research tool that handles real people's data, so a
few of the rules below are stricter than a typical Python project.

## Setup

```bash
docker compose up -d          # Postgres · Redis · Neo4j · SearXNG · crawl4ai
uv sync
cp .env.example .env
uv run alembic upgrade head
```

## Before you open a PR

```bash
uv run ruff check .           # must be clean
uv run pytest -q              # must be green
```

CI runs both, plus a guard that fails if investigation data or a local absolute path
gets committed.

## Conventions

- **UV only.** Never `pip install`. Add dependencies with `uv add`.
- **No magic strings.** Every enum value, default, and status string lives in
  `src/daisy/constants.py` as a `StrEnum` or module constant.
- **Facts are immutable.** A fact record is never edited or deleted. Only `lifecycle_state`
  changes, and every transition is written to `fact_events`. If a change seems to require
  mutating a fact, it needs a new fact instead.
- **Every retrieval is logged.** Any new fetch path must write a `fetch_attempts` row
  (`fetched` / `auth_wall` / `error` / `duplicate`). The audit's whole value is that it
  distinguishes what was seen from what was assumed — a silent fetch defeats it.
- **Async DB access** via asyncpg. Alembic runs sync (psycopg2).
- **Schema changes need a migration**: `uv run alembic revision --autogenerate -m "..."`.
  Keep the history linear — one head.
- **Model IDs use aliases** (`claude-sonnet-5-5`), never dated versions.
- **Project-scoped.** Facts and subjects always belong to a project.

## Adding an MCP tool

1. Declare it in `TOOLS` in `src/daisy/mcp_server.py` via the `_tool()` helper.
2. Add an `async def _handle_<name>` handler and wire it into the `handlers` dict in
   `call_tool`.
3. Add the tool to the table in `README.md` and the list in `CLAUDE.md`.
4. `tests/test_tool_registry.py` asserts the registry, dispatcher, and schemas agree —
   update the expected set there.

## Scope

Daisy is public-access OSINT only: public records, published reporting, and
breach-aggregator data. Contributions that defeat authentication, evade access controls,
use recovered credentials, or support surveillance, stalking, harassment, or the
identification of minors will be declined. See [SECURITY.md](SECURITY.md).

## Test data

Never commit real subject data — not in tests, fixtures, screenshots, or examples.
Use the synthetic conventions already in the repo: `example.com` / `example.gov` domains,
the reserved `555-01xx` phone range, and invented place names.
