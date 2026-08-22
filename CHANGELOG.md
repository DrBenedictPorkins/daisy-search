# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- MIT license, `SECURITY.md` (including the explicit public-access scope boundary),
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue and pull-request templates.
- CI: lint, tests, a single-Alembic-head check, and a guard that fails the build if
  investigation workspaces or local absolute paths are ever committed.
- Test suite covering the MCP tool registry and dispatcher, auth-wall detection, slugging,
  and lifecycle constants.
- `QUICK_SCAN.md` — no-database triage mode for sizing up a single lead.
- README: "How it compares" section positioning Daisy against other agent-native OSINT tools.

### Changed
- `.env.example` rewritten to match `config.py`; removed a stale SearXNG profile flag.
- `docker-compose.yml` and `.mcp.json` no longer hard-code a personal host or absolute path.
- Ruff now passes across the whole repo; Alembic revision files are excluded from linting.

### Fixed
- Documentation reported 13 MCP tools; there are 17, plus `daisy_fetch_clean` when
  Crawl4AI is configured.

## [0.1.0]

### Added
- MCP server exposing project, discovery, fact, fetch-audit, graph, and reporting tools.
- Postgres-backed persistence with immutable, sourced facts and a logged lifecycle.
- Fetch audit recording every retrieval as fetched / auth-walled / errored / duplicate.
- Neo4j identity graph built from confirmed facts.
- Read-only FastAPI dashboard.
