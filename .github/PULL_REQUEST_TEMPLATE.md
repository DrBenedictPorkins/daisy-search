## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `uv run ruff check .` is clean
- [ ] `uv run pytest -q` is green
- [ ] Schema change? An Alembic migration is included and the history has one head
- [ ] New MCP tool? Registered in `TOOLS`, wired into `call_tool`, documented in
      `README.md` + `CLAUDE.md`, and `tests/test_tool_registry.py` updated
- [ ] New fetch path? It writes a `fetch_attempts` row
- [ ] No real subject data, credentials, or local absolute paths in the diff
- [ ] Stays within public-access OSINT scope (see [SECURITY.md](../SECURITY.md))
