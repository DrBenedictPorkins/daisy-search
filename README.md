<div align="center">

# 🌼 Daisy Search

**An AI-orchestrated OSINT research engine.**

Point a Claude agent at a name. It plans the investigation, runs the searches, fetches the pages,
extracts structured facts, scores them, cross-links them into an identity graph — and remembers
everything across sessions in a shared Postgres brain.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-brain-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Neo4j](https://img.shields.io/badge/Neo4j-graph-4581C3?logo=neo4j&logoColor=white)](https://neo4j.com/)
[![MCP](https://img.shields.io/badge/MCP-server-black)](https://modelcontextprotocol.io/)
[![UV](https://img.shields.io/badge/UV-managed-DE5FE9)](https://docs.astral.sh/uv/)

</div>

---

## What it is

Daisy is not a script you run — it's a **toolbox an AI agent drives.** It plugs into Claude Code or
Claude Desktop as a [Model Context Protocol](https://modelcontextprotocol.io/) server. The agent is the
investigator; Daisy is the lab. The agent decides *what* to look for and *when* to trust it; Daisy handles
search, retrieval, extraction, persistence, and graphing — and enforces the discipline that keeps a
messy OSINT trail honest.

```
        ┌──────────────────────────────────────────────┐
        │   Claude Code / Desktop  (the investigator)   │
        │   plans · reasons · decides what to trust     │
        └───────────────────────┬──────────────────────┘
                     MCP tools   │   (17 of them)
        ┌───────────────────────▼──────────────────────┐
        │            Daisy MCP Server (the lab)         │
        │  search · fetch · extract · fact · graph      │
        └───┬───────────┬───────────┬───────────┬──────┘
            │           │           │           │
      ┌─────▼────┐ ┌────▼─────┐ ┌───▼────┐ ┌────▼─────┐
      │ Postgres │ │  Neo4j   │ │ SearXNG│ │ crawl4ai │
      │  (brain) │ │ (graph)  │ │(search)│ │ (fetch)  │
      └──────────┘ └──────────┘ └────────┘ └──────────┘
                    ▲
        ┌───────────┴───────────┐
        │  Web dashboard (RO)   │  read-only view of the case file
        └───────────────────────┘
```

## Why it's different

- **Agent-native.** Everything is an MCP tool. No brittle scraping DAGs — the model orchestrates,
  branches, and backtracks in natural language.
- **Facts are immutable & sourced.** Every claim is a permanent, timestamped record tied to its source.
  Nothing is silently edited. Only a fact's *lifecycle state* changes, and every transition is logged.
- **Built-in skepticism.** Findings require converging signals before promotion. A single noisy
  people-search hit doesn't become truth.
- **A shared brain across sessions.** Close the laptop, come back tomorrow — the case is exactly where
  you left it, in Postgres.
- **Fetch audit trail.** Every retrieval attempt is recorded (success, block, auth-wall), so you always
  know what was actually seen versus assumed.
- **Identity graph.** Entities, addresses, employers, and accounts are de-duplicated and linked into a
  navigable Neo4j graph.

## The fact lifecycle

Every discovered claim is an immutable record that moves through states — it is never deleted or rewritten:

```
   unconfirmed ──▶ likely ──▶ confirmed
        │            │
        └────────────┴──▶ discarded
                     └──▶ needs_revalidation ──▶ (re-enters the pipeline)
```

## The toolbox

The agent works entirely through these MCP tools, all scoped to the active project:

| Area | Tools |
|------|-------|
| **Projects** | `daisy_projects` · `daisy_new_project` · `daisy_switch_project` · `daisy_archive_project` |
| **Discovery** | `daisy_intake` · `daisy_search` · `daisy_fetch` · `daisy_extract` |
| **Fetch audit** | `daisy_fetch_log` · `daisy_fetch_summary` |
| **Facts** | `daisy_add_fact` · `daisy_facts` · `daisy_confirm` · `daisy_sources` |
| **Graph** | `daisy_build_graph` · `daisy_graph` |
| **Output** | `daisy_report` |

The methodology guide ships as an MCP resource at `daisy://methodology`, so the agent onboards itself.

## Quick start

**Prerequisites:** [UV](https://docs.astral.sh/uv/) · [Docker](https://www.docker.com/)

```bash
# 1. Bring up the stack (Postgres · Redis · Neo4j · SearXNG · crawl4ai)
docker compose up -d

# 2. Install dependencies
uv sync

# 3. Configure and migrate
cp .env.example .env
uv run alembic upgrade head

# 4a. Run as an MCP server (for Claude Code / Desktop)
uv run daisy-mcp

# 4b. …or open the read-only dashboard
uv run uvicorn daisy.api.app:app --reload   # http://localhost:8000
```

Then register the server with your Claude client and say: *"Start a new search for …"*

## Tech stack

| Layer | Tech |
|-------|------|
| Orchestrator | Claude Code / Claude Desktop (via MCP) |
| Tool server | Python 3.11 · [MCP](https://modelcontextprotocol.io/) |
| API / dashboard | FastAPI · Uvicorn · Jinja2 |
| Persistence | PostgreSQL (async, asyncpg) · SQLAlchemy 2.x · Alembic |
| Identity graph | Neo4j |
| Search / fetch | SearXNG · crawl4ai · trafilatura |
| Queue (optional batch) | Redis · RQ |
| Packaging | UV |

## Development

```bash
uv run pytest                 # tests
uv run ruff check src/        # lint
uv run alembic revision --autogenerate -m "…"   # new migration
docker exec -it daisy-postgres psql -U daisy -d daisy   # DB shell
```

## Project layout

```
src/daisy/
├── mcp_server.py     # all research tools live here
├── constants.py      # enums, magic strings, defaults
├── config.py         # pydantic-settings (env-driven)
├── graph.py          # identity graph construction
├── models/           # SQLAlchemy ORM (project · subject · fact · source · …)
├── api/              # read-only FastAPI dashboard
└── web/              # templates & static assets
migrations/           # Alembic
```

## Responsible use

Daisy is built for **legitimate, authorized work** — auditing your own footprint, consented
research, journalism, and security work — using only **public records, published reporting, and
breach-aggregator data.** It does not defeat authentication, evade access controls, or use recovered
credentials, and it is not for surveillance, stalking, or harassment. You are responsible for
complying with all applicable laws and platform terms in your jurisdiction.
