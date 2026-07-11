# Wrinkles & Open Issues

Architectural wrinkles, open questions, and issues discovered during implementation.
No bandaids — document and discuss.

---

## W-001: RQ worker pipeline being replaced by MCP tools

The Phase 1 worker pipeline (RQ + Redis + 7 worker classes) is being replaced by
MCP tools driven by Claude Code/Desktop. The workers/, queue/ directories and Redis
dependency will become dead code. Decision needed: remove entirely or keep as a
"headless batch mode" option for running without Claude?

**Status**: Open — discuss after MCP server is working.

## W-002: Existing data from Phase 1 test run

123 sources and 95 facts exist in Postgres from an early test run.
These predate the projects table. Need a migration strategy to assign them
to a project retroactively, or wipe and start fresh.

**Status**: Open — will handle during projects migration.

## W-003: Tethernet availability differs between Claude Code and Desktop

In Claude Code, Tethernet is available via subagent. In Claude Desktop, the user
would need a separate browser automation MCP server, or the methodology guide
should instruct Claude to ask the user to manually navigate and paste content.

**Status**: Open — document in methodology guide.

## W-004: Co-work mode needs guardrails

When Claude Desktop runs autonomously (co-work), it could burn through search API
quotas or fetch hundreds of pages. Need configurable limits per project:
max_searches, max_fetches, max_api_cost. The methodology guide must define
when to stop and wait for human review.

**Status**: Open — implement limits in MCP tools + document in methodology.

## W-005: Multi-session concurrency

Two Claude sessions could work on the same project simultaneously. Postgres handles
concurrent writes, but duplicate searches/fetches could occur. Need "has this query
been run recently?" checks in the search and fetch tools.

**Status**: Open — implement dedup checks in tools.

## W-006: In-context extraction via agent — MCP server can't spawn Claude agents

The fallback extraction path (no Ollama, no API key) relies on Claude spawning a
subagent. In Claude Code, this works via the Agent tool. In Claude Desktop,
the MCP server has no way to spawn a Claude agent — it can only return data.
The MCP tool would need to return the content and a hint to Claude to extract
facts manually (or spawn its own agent if in Claude Code).

**Status**: Open — may need different behavior per client.

## W-007: Source deduplication across projects

Same URL might be relevant to multiple projects/subjects. Currently sources are
not project-scoped. Should they be shared (save fetching) or duplicated per
project (isolation)? Shared saves bandwidth but means one project's fetch
could affect another's.

**Status**: Open — leaning toward shared sources with per-project fact extraction.

## W-008: Review workflow needs interactive pattern in methodology

The tools exist (daisy_facts to query, daisy_confirm to update) but the methodology
guide doesn't describe the interactive review loop clearly enough. Claude needs to:

1. Present facts in logical batches (by claim_type or source)
2. Flag likely-different-person facts for quick discard
3. Let the user confirm/discard/skip in natural language ("yes", "no", "skip", "all wrong")
4. Handle batch confirmations ("confirm all employment facts" or "discard everything from Saskatchewan")

This is a methodology/prompt refinement, not a new tool.

**Status**: Open — refine methodology guide after first real review session.

## W-009: Fact deduplication within a project

Same fact extracted multiple times from repeat fetches of the same URL (e.g. LinkedIn
fetched 3 times = 3x the facts). Need either:
- Dedup at extraction time (fuzzy match on claim_text + source_url)
- Dedup tool for post-hoc cleanup
- Prevent re-fetching same URL within a project

**Status**: Open — needs implementation before the tool is production-usable.

## W-010: Multiple subjects in one project conflated

An early test project had facts about 3+ different people all under one subject.
The Resolution worker (Phase 2) is meant to handle this, but until then the review
workflow needs to let the user split facts into separate subjects or discard
wrong-person facts efficiently.

**Status**: Open — Phase 2 dependency.

## W-011: Tethernet agent too cautious with form submissions

The tethernet subagent follows SPA detection rules that say "don't click, just guide"
when JavaScript is detected. But this is overly broad — simple HTML forms with submit
buttons (like county tax portals) are safe to interact with directly. The agent filled
in a search form on taxrecords-nj.com but never clicked submit, silently moving on
as if it had completed the search.

The methodology guide and tethernet delegation prompts need to distinguish between:
- Simple forms (safe to submit)
- Complex SPA interactions (guide the user)

**Status**: Open — refine tethernet delegation prompts.

## W-012: Tethernet agent should escalate login/paywall barriers to user

When the agent encounters paywalled sites (Spokeo, BeenVerified), login-required
pages (ZoomInfo, ContactOut), or force-create-account screens, it currently
silently gives up and reports partial/no data. Instead it should:

1. Recognize the paywall/login pattern
2. Tell the user: "This site requires a login/subscription. Want me to guide you
   through creating an account or logging in?"
3. Wait for the user's decision before proceeding or skipping
4. If the user logs in, resume data extraction from the authenticated view

This is critical for OSINT research where many people-search aggregators gate
their full data behind free or paid accounts.

**Status**: Open — add to tethernet delegation prompt and methodology guide.

## W-013: Tethernet missing HTTP/XHR network capture

Tethernet has capture_websocket, capture_console, and capture_errors,
but NO tool for capturing HTTP/XHR request/response data. This is critical
for SPA-heavy sites like Instagram where:

- The UI is rendered client-side from API responses
- Scrolling/clicking triggers XHR calls with structured JSON data
- The API response contains complete data (followers list, post details)
  that would take dozens of screenshots + OCR to reconstruct

Example: Instagram's Following list is loaded via
`/api/v1/friendships/{user_id}/following/` — the JSON response has all
51 accounts in structured format. The user can see it in Firefox's Network
tab, but Tethernet has no tool to capture it programmatically.

Needed: A `capture_network` or `capture_xhr` tool that intercepts
HTTP requests/responses for a duration, similar to how capture_websocket
works. Filter by URL pattern, return request/response pairs.

**Status**: Open — file as Tethernet feature request.

## W-014: Tethernet navigate does not foreground the tab/window

When using `navigate` to open a URL, the page loads in whatever tab is set
as primary — but that tab might be in a background window or not the active
tab. The user can't see what's happening. Every single Tethernet session
hits this problem and requires manual `execute_background_script` calls to
`browser.tabs.update(id, {active:true})` and
`browser.windows.update(windowId, {focused:true})`.

The `navigate` tool should ALWAYS foreground the tab and its window after
navigation. This should not require separate calls or user intervention.

**Status**: Open — file as Tethernet bug/feature request.

## W-015: Graph parser creates spurious nodes from relationship claim text

When a relationship fact mentions companies or titles as context
(e.g., "LinkedIn connections include Sanghamitra Nayak (Sr Dir Software Dev,
Showtime)"), the _extract_family_names regex pulls out fragments like
"Sr Dir Software Dev" as Person nodes. Similarly, the cross-connection
logic then links these phantom nodes to companies as colleagues.

The graph should only create WORKS_AT edges from explicit employment facts
about a specific person, not from mentions of companies in relationship
or social network facts.

Fix: relationship facts should only create Person nodes and relationship
edges. They should NOT trigger employment/location/company parsing.
The claim_type routing in _add_fact already handles this, but the
_extract_family_names function is too greedy with its regex.

**Status**: Open — needs cleaner fact parsing.

## W-016: Graph builder needs automated validation pass (CRITICAL)

The graph builder blindly maps regex-parsed text to nodes/edges with no
validation. Every rebuild produces bad data that requires manual auditing:
duplicate person nodes, properties linked to wrong locations, garbage
names parsed from claim text, etc.

The rebuild function MUST include an automated validation pass that:
1. Checks properties only LOCATED_IN one municipality
2. Normalizes all person names (no "Wife X", "Likely X" prefixes)
3. Validates company names against the alias table
4. Removes self-edges from merges
5. Removes orphan nodes with no edges
6. Flags contradictions (same entity in two locations)
7. Reports what it fixed so the user can review

This should run automatically at the end of every rebuild, not require
the user to visually audit the graph and report errors.

**Status**: Open — critical for graph trustworthiness.

## W-017: Tethernet agent refuses OSINT tasks it deems "doxing"

The Tethernet subagent refused to search public records portals (voter
records, deed transfers, marriage index) citing "doxing" concerns. This
directly conflicts with the purpose of Daisy Search as an OSINT research
tool. The user explicitly authorized and initiated the research.

The Tethernet delegation prompt needs to include context about Daisy
Search being a legitimate authorized research tool, and that the user
has consented to and is driving the investigation. Without this context
the agent refuses to perform its core function.

Workaround: include authorization context in every Tethernet delegation
prompt, or handle these searches outside Tethernet.

**Status**: Open — needs prompt engineering fix.
