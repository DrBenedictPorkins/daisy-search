# Daisy Search

Personal identity exposure / OSINT research system.
Operated via Claude Code or Claude Desktop as an MCP server.

## CRITICAL RULES
- NEVER run any git commands (status, log, diff, add, commit, push, etc.) unless the user explicitly says to in that message
- NEVER ask to commit, suggest committing, or mention git in any way
- NEVER propose or suggest git operations — the user will tell you when they want to use git

## Scope — public-access OSINT only
- Sources are **public records, published reporting, and breach-aggregator data**. Nothing else.
- Never defeat authentication, evade access controls, or use recovered credentials.
  An auth wall is a stopping point, recorded as `auth_wall` in the fetch log — not an obstacle to route around.
- Browser tools (Claude-in-Chrome / Tethernet) are for pages the **operator is already logged into**
  in their own session, or for JS-heavy public pages. They are not a bypass.
- Never research or identify a subject's minor children — not even passively for confirmation.
- Prefer government / primary sources. Avoid paywalled people-search aggregators.

## Tech Stack
- Python 3.11, FastAPI, SQLAlchemy 2.x (async), Alembic, Postgres
- Neo4j for the identity graph; SearXNG for search; crawl4ai for clean page extraction
- MCP server for Claude Code / Claude Desktop integration
- Package manager: UV (never use pip/pip install directly)

## OSINT Scripts
- `scripts/leakcheck.sh <email|phone|username> ...` — LeakCheck.io breach database lookup
  - Reads `LEAKCHECK_API_KEY` from `~/.secrets/api_keys.sh`
  - Run: `source ~/.secrets/api_keys.sh && scripts/leakcheck.sh "<email>"`
  - Supports: email, phone (digits only, no +1), username
  - Returns: breach sources, exposed fields, plaintext passwords, PII (NPD has SSN/address history)
  - Multiple queries in one call: `scripts/leakcheck.sh "<email>" "<email>" "<phone>"`
- `scripts/new_search.sh "<name>" "<context>"` — scaffold a per-case workspace under `projects/`
  from `templates/` (CLAUDE.md, SESSION_STATE.md, local MCP settings)
- `scripts/gen_report.py` — render a case dossier (Quarto/HTML) from the database
- `scripts/gen_graph_html.py` — standalone vis.js identity-graph page from confirmed facts
- `scripts/vpncheck.sh` — report whether a VPN is currently connected
- `scripts/send_email.py` — send a report via Amazon SES (credentials via boto3 chain; never echoed)

## Commands
- `uv sync` -- install/sync dependencies
- `uv run daisy-mcp` -- start the MCP server (stdio)
- `uv run python -m daisy.cli` -- CLI for testing/debugging
- `uv run uvicorn daisy.api.app:app --reload` -- run web dashboard
- `uv run alembic upgrade head` -- run migrations
- `uv run alembic revision --autogenerate -m "description"` -- create migration
- `uv run pytest` -- run tests (tool registry shape, auth-wall detection, lifecycle constants)
- `uv run ruff check .` -- lint (Alembic revision files are excluded)
- `uv run ruff check --fix .` -- lint and auto-fix
- `docker compose up -d` -- start Postgres, Redis, Neo4j, SearXNG, crawl4ai

## Project Layout
- `src/daisy/mcp_server.py` -- MCP server with all research tools
- `src/daisy/graph.py` -- identity graph construction (entity extraction, dedup, Neo4j projection)
- `src/daisy/constants.py` -- all enums, magic strings, default values
- `src/daisy/config.py` -- pydantic-settings, env var overrides
- `src/daisy/models/` -- SQLAlchemy ORM models (project, subject, fact, source, job, fetch_attempt)
- `src/daisy/workers/` -- legacy worker classes (intake helpers still used)
- `src/daisy/queue/` -- legacy job dispatch (kept for optional batch mode)
- `src/daisy/api/` -- FastAPI web dashboard (read-only presentation)
- `src/daisy/cli.py` -- CLI for testing outside of Claude
- `migrations/` -- Alembic migrations
- `templates/` -- per-case workspace templates used by `scripts/new_search.sh`
- `projects/`, `searches/` -- per-case workspaces; git-ignored, contain investigation data
- `docs/screenshots/` -- README images (synthetic demo persona only)

## MCP Server
- 17 always-on tools, plus `daisy_fetch_clean` when `CRAWL4AI_URL` is configured:
  - Projects: `daisy_projects`, `daisy_new_project`, `daisy_switch_project`, `daisy_archive_project`
  - Discovery: `daisy_intake`, `daisy_search`, `daisy_fetch`, `daisy_extract` (+ `daisy_fetch_clean`)
  - Fetch audit: `daisy_fetch_summary`, `daisy_fetch_log`
  - Facts: `daisy_add_fact`, `daisy_facts`, `daisy_confirm`, `daisy_sources`
  - Graph: `daisy_build_graph`, `daisy_graph`
  - Output: `daisy_report`
- All research tools scoped to the active project
- Extraction priority: Ollama > Anthropic API > return content for in-context extraction
- Methodology guide loaded as MCP resource at daisy://methodology

## Modes
- **Full workflow** — project-backed, sourced case file. The default.
- **Quick scan** — no-DB triage of a single lead. See `QUICK_SCAN.md`. No project, no facts, display only.

## Conventions
- All DB access is async (asyncpg). Alembic uses sync (psycopg2).
- Facts are immutable records. Only lifecycle_state is mutable, logged in fact_events.
- NEVER hardcode magic strings. Use StrEnum constants from constants.py.
- LLM model IDs use aliases (claude-sonnet-5-5) not dated versions.
- Config loaded from .env via pydantic-settings.
- Projects are the top-level container. All facts/subjects scoped to a project.
- Every fetch attempt is logged (fetched / auth_wall / error / duplicate) so the record
  distinguishes what was actually seen from what was assumed.

## Database
- Local dev: `docker compose up -d` starts Postgres on 5432 and Redis on 6379
- Also started: Neo4j (7474 / 7687), SearXNG (9741), crawl4ai (11235)
- DB name: daisy, user: daisy, password: daisy_dev
- Always create Alembic migrations for schema changes

## Architecture
- Claude Code/Desktop = orchestrator (drives research via MCP tools)
- MCP server = thin tool layer (DB + search + fetch + extract + graph)
- Postgres = shared brain (persists across sessions)
- Neo4j = identity graph built from confirmed facts
- Web dashboard = read-only presentation layer
- Tethernet / Claude-in-Chrome = browser access for JS-heavy or operator-authenticated pages (Claude Code only)
- See WRINKLES.md for open architectural issues

## Tethernet / Browser Form Automation — CRITICAL

A tool call's return flag (`selected: true`, `typed: true`, `clicked: true`) only means the DOM operation executed — it is NOT confirmation the intended state was achieved. Never chain the next step off an unverified assumption.

1. **Inspect before acting.** Before interacting with a form on an unfamiliar site, inspect the real element first — `execute_script` reading `.value`/`.options`, or `find()` / accessibility tree — rather than guessing a CSS selector.
2. **Verify after every state-changing action.** After `select_option` or `type_text`, read back the actual resulting value before proceeding. A `select_option` on a non-matching value (e.g. passing a label like `"Owner"` when the real `<option>` values are numeric indices `"0"`, `"1"`, `"2"`) silently leaves the field broken and reports success anyway.
3. **Default to accessibility-tree tools** (`find`, `get_accessibility_tree`) over raw CSS selectors on unfamiliar/modern sites. CSS-selector-based tools silently fail on Shadow DOM (e.g. Salesforce Experience Cloud widgets) and can hit the wrong element when a page has multiple similar inputs (e.g. a generic site-search box loading before the real form). Accessibility-tree tools work correctly on both plain DOM and Shadow DOM.
4. **On failure, escalate in order:** diagnose via inspection → switch tool/strategy (e.g. CSS selector → `find()`) → only then ask the user to drive manually, and only once per site, not once per retry.
5. **If a submit/navigation lands somewhere unexpected**, stop and diagnose why before retrying — don't re-navigate and retry blind.
