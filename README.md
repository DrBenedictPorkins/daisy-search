# Daisy Search

A personal identity exposure and internet presence mapping system. Discovers, collects, evaluates, and continuously re-validates information about a person found on the public internet.

Use cases:
- **Self-audit** -- understand your own digital footprint
- **Research** -- map the public presence of any individual
- **Continuous monitoring** -- re-validate findings as new evidence surfaces

## Quick Start

### Prerequisites
- [UV](https://docs.astral.sh/uv/) (Python package manager)
- [Docker](https://www.docker.com/) (for Postgres and Redis)

### Install & Run

```bash
# Clone and enter the project
cd daisy-search

# Start infrastructure
docker compose up -d

# Install dependencies
uv sync

# Copy environment config
cp .env.example .env

# Run database migrations
uv run alembic upgrade head

# Start the API server
uv run uvicorn daisy.api.app:app --reload
```

The API will be available at `http://localhost:8000`. Health check: `GET /health`.

## Architecture

Daisy Search uses a **worker pipeline** architecture. Each stage of the research process is handled by a specialized worker:

```
Intake -> Discovery -> Acquisition -> Extraction -> Scoring -> Resolution
                                                                  |
                                                            Human Review
```

### Workers

| Worker | Responsibility |
|--------|---------------|
| **Intake** | Expands vague inputs into search queries, name variants, and username guesses |
| **Discovery** | Runs exploratory searches and returns candidate URLs |
| **Acquisition** | Fetches and stores raw content snapshots |
| **Extraction** | Extracts structured claims from content (LLM-assisted) |
| **Scoring** | Calculates confidence scores and updates fact lifecycle |
| **Resolution** | Clusters identity matches and handles name collisions |
| **Review** | Prepares facts for human validation |

### Fact Lifecycle

Every discovered claim is stored as an immutable **fact** that moves through lifecycle states:

```
unconfirmed -> likely -> confirmed
                 \-> discarded
                 \-> needs_revalidation -> (re-enters pipeline)
```

Facts are never deleted or modified. State transitions are logged in `fact_events` for full auditability.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11 |
| API | FastAPI + Uvicorn |
| Database | PostgreSQL 16 |
| Queue | Redis + RQ |
| ORM | SQLAlchemy 2.x (async) |
| Migrations | Alembic |
| Config | pydantic-settings |
| Package Manager | UV |

## Development

### Running Tests
```bash
uv run pytest
```

### Linting
```bash
uv run ruff check src/
```

### Creating a Migration
```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

### Database Access
```bash
# Connect to local Postgres
docker exec -it daisy-postgres psql -U daisy -d daisy
```

## Project Structure

```
daisy-search/
├── src/daisy/
│   ├── models/          # SQLAlchemy ORM models
│   ├── workers/         # Pipeline workers
│   ├── queue/           # Job dispatch (RQ)
│   ├── api/             # FastAPI application
│   │   └── routes/      # API endpoints
│   └── web/             # Frontend templates & static assets
├── migrations/          # Alembic migrations
├── tests/               # Test suite
├── docker-compose.yml   # Postgres + Redis
└── pyproject.toml       # Project config (UV)
```

## Roadmap

- [x] Project scaffolding & DB schema
- [ ] Phase 1: Core pipeline (Intake -> Discovery -> Acquisition -> Extraction)
- [ ] Phase 2: Scoring, Resolution & Review UI
- [ ] Phase 3: Re-validation loop & continuous monitoring
- [ ] Phase 4: Tethernet integration, export, and multi-subject support

## Infrastructure

### Local Development
Everything runs on the local machine via Docker (Postgres + Redis) and UV (Python).

### Production
Designed to run on a GPU-equipped Ubuntu server for local LLM inference (extraction worker) and high-throughput crawling.
