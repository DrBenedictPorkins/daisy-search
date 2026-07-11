"""Daisy Search MCP Server.

Exposes OSINT research tools to Claude Code and Claude Desktop.
All research operations are scoped to an active project.
"""

import asyncio
import json
import re
import uuid as uuid_mod
from datetime import date

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from daisy.config import settings
from daisy.constants import (
    DEFAULT_FACT_CONFIDENCE,
    AliasType,
    ClaimType,
    LifecycleState,
    ProjectStatus,
    SourceType,
)

# ---------------------------------------------------------------------------
# Database setup (fresh engine for the MCP server process)
# ---------------------------------------------------------------------------

_engine = create_async_engine(
    settings.database_url, echo=False, pool_pre_ping=True
)
_session_factory = async_sessionmaker(
    _engine, class_=AsyncSession, expire_on_commit=False
)

# Active project (in-memory, per server process)
_active_project_id: uuid_mod.UUID | None = None

# Crawl4AI availability (set at startup by health check)
_crawl4ai_available: bool = False

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("daisy-search")


def _tool(
    name: str,
    description: str,
    properties: dict,
    required: list | None = None,
) -> Tool:
    """Helper to build a Tool object."""
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return Tool(name=name, description=description, inputSchema=schema)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    _tool(
        "daisy_projects",
        "List all research projects with their status and stats.",
        {},
    ),
    _tool(
        "daisy_new_project",
        "Create a new research project.",
        {
            "name": {"type": "string", "description": "Project name"},
            "description": {"type": "string", "description": "Project description"},
        },
        required=["name"],
    ),
    _tool(
        "daisy_switch_project",
        "Switch the active project by ID or name.",
        {
            "project": {
                "type": "string",
                "description": "Project UUID or name (partial match)",
            },
        },
        required=["project"],
    ),
    _tool(
        "daisy_archive_project",
        "Archive a project (set status to archived).",
        {
            "project_id": {"type": "string", "description": "Project UUID to archive"},
        },
        required=["project_id"],
    ),
    _tool(
        "daisy_intake",
        (
            "Add a research subject to the active project."
            " Generates name variants, username guesses,"
            " and search queries."
        ),
        {
            "name": {"type": "string", "description": "Full name of the person"},
            "locations": {
                "type": "array", "items": {"type": "string"},
                "description": "Known locations",
            },
            "occupations": {
                "type": "array", "items": {"type": "string"},
                "description": "Known occupations or titles",
            },
            "known_usernames": {
                "type": "array", "items": {"type": "string"},
                "description": "Known usernames",
            },
            "known_emails": {
                "type": "array", "items": {"type": "string"},
                "description": "Known email addresses",
            },
            "known_facts": {
                "type": "array", "items": {"type": "string"},
                "description": "Known facts, e.g. 'works at Paramount'",
            },
            "notes": {"type": "string", "description": "Additional notes"},
        },
        required=["name"],
    ),
    _tool(
        "daisy_search",
        "Run search queries and return candidate URLs. Uses Brave Search or SearXNG.",
        {
            "queries": {
                "type": "array", "items": {"type": "string"},
                "description": "Search queries to execute",
            },
        },
        required=["queries"],
    ),
    _tool(
        "daisy_fetch",
        "Fetch a URL and store the raw content as a source.",
        {
            "url": {"type": "string", "description": "URL to fetch"},
        },
        required=["url"],
    ),
    _tool(
        "daisy_extract",
        (
            "Extract facts from a stored source."
            " Tries Ollama, then Anthropic API."
            " If neither available, returns cleaned"
            " text for in-context extraction."
        ),
        {
            "source_id": {
                "type": "string",
                "description": "Source UUID to extract from",
            },
            "subject_id": {
                "type": "string",
                "description": (
                    "Subject UUID to associate facts with"
                ),
            },
        },
        required=["source_id", "subject_id"],
    ),
    _tool(
        "daisy_add_fact",
        "Manually add a fact to the active project.",
        {
            "subject_id": {"type": "string", "description": "Subject UUID"},
            "claim_text": {"type": "string", "description": "The claim"},
            "claim_type": {
                "type": "string",
                "description": (
                    "Type: employment, education,"
                    " location, account, relationship,"
                    " publication, legal, other"
                ),
            },
            "source_url": {
                "type": "string",
                "description": "Source URL or 'user_input'",
            },
            "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
            "event_date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD if known",
            },
            "raw_snippet": {"type": "string", "description": "Supporting text snippet"},
            "metadata": {"type": "object", "description": "Optional arbitrary metadata"},
        },
        required=["subject_id", "claim_text", "claim_type"],
    ),
    _tool(
        "daisy_facts",
        (
            "Query facts for the active project."
            " Filterable by subject, type,"
            " state, confidence."
        ),
        {
            "subject_id": {"type": "string", "description": "Filter by subject UUID"},
            "claim_type": {"type": "string", "description": "Filter by claim type"},
            "lifecycle_state": {"type": "string", "description": "Filter by state"},
            "min_confidence": {"type": "number", "description": "Minimum confidence"},
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
    ),
    _tool(
        "daisy_confirm",
        "Update the lifecycle state of a fact.",
        {
            "fact_id": {"type": "string", "description": "Fact UUID"},
            "new_state": {
                "type": "string",
                "description": (
                    "New state: confirmed, discarded,"
                    " likely, needs_revalidation"
                ),
            },
            "reason": {"type": "string", "description": "Why the state changed"},
        },
        required=["fact_id", "new_state"],
    ),
    _tool(
        "daisy_report",
        "Generate a summary report for the active project or a specific subject.",
        {
            "subject_id": {
                "type": "string",
                "description": (
                    "Optional subject UUID"
                    " (default: all subjects)"
                ),
            },
        },
    ),
    _tool(
        "daisy_sources",
        "List fetched sources for the active project.",
        {
            "subject_id": {"type": "string", "description": "Filter by subject UUID"},
            "limit": {"type": "integer", "description": "Max results (default 20)"},
        },
    ),
    _tool(
        "daisy_fetch_summary",
        "Domain-grouped status report of all fetch attempts for the active project. "
        "Call after bulk discovery to see what succeeded, what hit auth walls, and "
        "what errored — grouped by site so the user can decide how to proceed.",
        {},
    ),
    _tool(
        "daisy_fetch_log",
        "Show fetch attempt history for the active project. "
        "Includes auth walls, errors, duplicates, and successes. "
        "Use to see what still needs Claude-in-Chrome or retry.",
        {
            "outcome": {
                "type": "string",
                "description": "Filter by outcome: fetched, auth_wall, error, duplicate",
            },
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
    ),
    _tool(
        "daisy_build_graph",
        "Rebuild the Neo4j graph for the active project "
        "from confirmed facts in Postgres.",
        {},
    ),
    _tool(
        "daisy_graph",
        "Query the relationship graph for a subject. "
        "Returns all connections.",
        {
            "subject_id": {
                "type": "string",
                "description": "Subject UUID",
            },
        },
        required=["subject_id"],
    ),
]


_CRAWL4AI_FETCH_CLEAN_TOOL = _tool(
    "daisy_fetch_clean",
    (
        "Fetch a URL (or accept pre-fetched HTML) and return clean extracted "
        "markdown via Crawl4AI. Use for public URLs to get clean text without "
        "storing raw HTML. For auth-walled pages: pass the html argument with "
        "content fetched via Tethernet — raw HTML never enters the CC context."
    ),
    {
        "url": {"type": "string", "description": "URL to fetch and clean"},
        "html": {
            "type": "string",
            "description": (
                "Pre-fetched raw HTML (optional). If provided, skips "
                "fetching and sends directly to Crawl4AI."
            ),
        },
    },
    required=["url"],
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = list(TOOLS)
    if _crawl4ai_available:
        tools.append(_CRAWL4AI_FETCH_CLEAN_TOOL)
    return tools


@server.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri="daisy://methodology",
            name="OSINT Research Methodology",
            description="Guide for conducting OSINT research with Daisy Search tools",
            mimeType="text/markdown",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    if uri == "daisy://methodology":
        return _get_methodology()
    raise ValueError(f"Unknown resource: {uri}")


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handlers = {
        "daisy_projects": _handle_projects,
        "daisy_new_project": _handle_new_project,
        "daisy_switch_project": _handle_switch_project,
        "daisy_archive_project": _handle_archive_project,
        "daisy_intake": _handle_intake,
        "daisy_search": _handle_search,
        "daisy_fetch": _handle_fetch,
        "daisy_extract": _handle_extract,
        "daisy_add_fact": _handle_add_fact,
        "daisy_facts": _handle_facts,
        "daisy_confirm": _handle_confirm,
        "daisy_report": _handle_report,
        "daisy_sources": _handle_sources,
        "daisy_fetch_summary": _handle_fetch_summary,
        "daisy_fetch_log": _handle_fetch_log,
        "daisy_build_graph": _handle_build_graph,
        "daisy_graph": _handle_graph,
        "daisy_fetch_clean": _handle_fetch_clean,
    }

    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    result = await handler(arguments)
    return [TextContent(type="text", text=result)]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _handle_projects(args: dict) -> str:
    from daisy.models.fact import Fact
    from daisy.models.project import Project
    from daisy.models.subject import Subject

    async with _session_factory() as session:
        result = await session.execute(
            select(Project).order_by(Project.created_at.desc())
        )
        projects = result.scalars().all()

        if not projects:
            return "No projects found. Create one with daisy_new_project."

        lines = []
        for p in projects:
            # Count subjects
            sub_count = (await session.execute(
                select(func.count()).where(Subject.project_id == p.id)
            )).scalar()
            # Count facts
            fact_count = (await session.execute(
                select(func.count()).where(Fact.project_id == p.id)
            )).scalar()

            active = " << ACTIVE" if p.id == _active_project_id else ""
            lines.append(
                f"- [{p.status}] {p.name}{active}\n"
                f"  ID: {p.id}\n"
                f"  {sub_count} subjects, {fact_count} facts\n"
                f"  Created: {p.created_at.strftime('%Y-%m-%d')}"
            )

        return "\n\n".join(lines)


async def _handle_new_project(args: dict) -> str:
    global _active_project_id
    from pathlib import Path

    from daisy.models.project import Project

    async with _session_factory() as session:
        async with session.begin():
            project = Project(
                name=args["name"],
                description=args.get("description"),
            )
            session.add(project)
            await session.flush()
            _active_project_id = project.id
            project_id = str(project.id)
            project_name = project.name

    # Create project workspace directory
    projects_root = Path(settings.projects_dir).resolve()
    slug = _slugify(project_name)
    project_dir = projects_root / slug
    # Avoid collisions with an existing project dir of the same name
    if project_dir.exists():
        n = 2
        while (projects_root / f"{slug}-{n}").exists():
            n += 1
        project_dir = projects_root / f"{slug}-{n}"
    for subdir in [
        "downloads",
        "screenshots",
        "reports",
        "raw",
        "notes",
    ]:
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Save workspace path to DB
    from daisy.models.project import Project

    async with _session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(Project).where(
                    Project.id == uuid_mod.UUID(project_id)
                )
            )
            proj = result.scalar_one()
            proj.workspace_path = str(project_dir)

    return (
        f"Project created and set as active.\n"
        f"ID: {project_id}\n"
        f"Name: {project_name}\n"
        f"Workspace: {project_dir}"
    )


async def _handle_switch_project(args: dict) -> str:
    global _active_project_id
    from daisy.models.project import Project

    query = args["project"]

    async with _session_factory() as session:
        # Try UUID first
        try:
            pid = uuid_mod.UUID(query)
            result = await session.execute(
                select(Project).where(Project.id == pid)
            )
            project = result.scalar_one_or_none()
        except ValueError:
            # Try name match
            result = await session.execute(
                select(Project).where(
                    Project.name.ilike(f"%{query}%")
                )
            )
            project = result.scalars().first()

        if not project:
            return f"No project found matching '{query}'."

        _active_project_id = project.id
        return (
            f"Switched to project: {project.name}\n"
            f"ID: {project.id}\n"
            f"Status: {project.status}"
        )


async def _handle_archive_project(args: dict) -> str:
    global _active_project_id
    from daisy.models.project import Project

    pid = uuid_mod.UUID(args["project_id"])

    async with _session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(Project).where(Project.id == pid)
            )
            project = result.scalar_one_or_none()
            if not project:
                return "Project not found."
            project.status = ProjectStatus.ARCHIVED

            if _active_project_id == pid:
                _active_project_id = None

    return f"Archived project: {project.name}"


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:50].rstrip("-")


def _require_active_project() -> uuid_mod.UUID:
    if _active_project_id is None:
        raise ValueError(
            "No active project. Use daisy_switch_project or "
            "daisy_new_project first."
        )
    return _active_project_id


async def _handle_intake(args: dict) -> str:
    from daisy.models.fact import Fact
    from daisy.models.subject import Subject, SubjectAlias
    from daisy.workers.intake import (
        _build_search_queries,
        _generate_name_variants,
        _generate_username_guesses,
    )

    project_id = _require_active_project()
    name = args["name"]
    locations = args.get("locations", [])
    occupations = args.get("occupations", [])
    known_usernames = args.get("known_usernames", [])
    known_emails = args.get("known_emails", [])
    known_facts = args.get("known_facts", [])
    notes = args.get("notes")

    name_variants = _generate_name_variants(name)
    username_guesses = _generate_username_guesses(
        name, known_usernames
    )
    search_queries = _build_search_queries(
        name, name_variants, locations, occupations, known_facts
    )

    async with _session_factory() as session:
        async with session.begin():
            subject = Subject(
                name=name,
                project_id=project_id,
                notes=notes,
            )
            session.add(subject)
            await session.flush()

            for variant in name_variants:
                if variant.lower() != name.lower():
                    session.add(SubjectAlias(
                        subject_id=subject.id,
                        alias_type=AliasType.NAME_VARIANT,
                        alias_value=variant,
                    ))

            for uname in username_guesses:
                session.add(SubjectAlias(
                    subject_id=subject.id,
                    alias_type=AliasType.USERNAME,
                    alias_value=uname,
                    confidence=0.3,
                ))

            for uname in known_usernames:
                if uname not in username_guesses:
                    session.add(SubjectAlias(
                        subject_id=subject.id,
                        alias_type=AliasType.USERNAME,
                        alias_value=uname,
                        confidence=0.9,
                    ))

            for email in known_emails:
                session.add(SubjectAlias(
                    subject_id=subject.id,
                    alias_type=AliasType.EMAIL,
                    alias_value=email,
                    confidence=0.9,
                ))

            for fact_text in known_facts:
                session.add(Fact(
                    project_id=project_id,
                    subject_id=subject.id,
                    claim_text=fact_text,
                    claim_type=ClaimType.OTHER,
                    source_url="user_input",
                    source_type="user_input",
                    raw_snippet=fact_text,
                    local_confidence=1.0,
                    lifecycle_state=LifecycleState.CONFIRMED,
                ))

            subject_id = subject.id

    return (
        f"Subject created: {name}\n"
        f"ID: {subject_id}\n"
        f"Name variants: {len(name_variants)}\n"
        f"Username guesses: {len(username_guesses)}\n"
        f"Known facts seeded: {len(known_facts)}\n\n"
        f"Suggested search queries ({len(search_queries)}):\n"
        + "\n".join(f"  - {q}" for q in search_queries)
    )


async def _handle_search(args: dict) -> str:
    queries = args["queries"]
    # MCP may pass arrays as JSON strings
    if isinstance(queries, str):
        queries = json.loads(queries)
    results = []
    seen_urls: set[str] = set()

    for query in queries:
        hits = await _search_api(query)
        for hit in hits:
            if hit["url"] not in seen_urls:
                seen_urls.add(hit["url"])
                results.append(hit)

    if not results:
        return "No results found."

    lines = [f"Found {len(results)} unique URLs:\n"]
    for r in results:
        title = r.get("title", "")[:60]
        snippet = r.get("snippet", "")[:100]
        lines.append(
            f"- {r['url']}\n"
            f"  {title}\n"
            f"  {snippet}"
        )

    return "\n".join(lines)


async def _log_fetch_attempt(
    url: str,
    outcome: str,
    final_url: str | None = None,
    http_status: int | None = None,
    auth_wall_reason: str | None = None,
    recommended_tool: str | None = None,
    permanent: bool | None = None,
    error_message: str | None = None,
) -> None:
    from daisy.models.fetch_attempt import FetchAttempt
    async with _session_factory() as session:
        async with session.begin():
            session.add(FetchAttempt(
                project_id=_active_project_id,
                url=url,
                final_url=final_url,
                outcome=outcome,
                http_status=http_status,
                auth_wall_reason=auth_wall_reason,
                recommended_tool=recommended_tool,
                permanent=permanent,
                error_message=error_message,
            ))


async def _handle_fetch(args: dict) -> str:
    import hashlib

    from daisy.models.source import Source

    url = args["url"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DaisySearch/0.1)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, max_redirects=5
        ) as client:
            resp = await client.get(url, headers=headers)
            raw = resp.text
            status = resp.status_code
            ctype = (
                resp.headers.get("content-type", "text/html")
                .split(";")[0]
                .strip()
            )
    except Exception as e:
        await _log_fetch_attempt(url, outcome="error", error_message=str(e))
        return f"Fetch failed: {e}"

    content_hash = hashlib.sha256(raw.encode()).hexdigest()
    final_url = str(resp.url)

    # Auth-wall detection
    auth_wall = _detect_auth_wall(status, final_url, raw)
    if auth_wall:
        reason, tool, permanent = auth_wall
        await _log_fetch_attempt(
            url, outcome="auth_wall", final_url=final_url,
            http_status=status, auth_wall_reason=reason,
            recommended_tool=tool, permanent=permanent,
        )
        permanence = "PERMANENT — retry will not help" if permanent else "POSSIBLY TEMPORARY — retry may work"
        next_steps = {
            "Claude-in-Chrome": (
                "1. Load Claude-in-Chrome tools via ToolSearch\n"
                "2. Call mcp__claude-in-chrome__tabs_context_mcp to get tab context\n"
                "3. Call mcp__claude-in-chrome__navigate with the original URL\n"
                "4. Call mcp__claude-in-chrome__read_page to extract content\n"
                "   (Chrome already has your logged-in session)"
            ),
            "retry daisy_fetch": (
                "Call daisy_fetch with the same URL again — nothing was stored so\n"
                "there is no duplicate conflict. If it fails again, use Claude-in-Chrome."
            ),
            "retry daisy_fetch after a short wait": (
                "Wait ~30 seconds then call daisy_fetch with the same URL again.\n"
                "Nothing was stored so there is no duplicate conflict."
            ),
        }.get(tool, f"Use {tool}")
        return (
            f"AUTH WALL DETECTED — {reason}\n"
            f"{permanence}\n"
            f"Original URL: {url}\n"
            f"Final URL: {final_url}\n"
            f"HTTP {status}\n"
            f"\nRecommended action: {tool}\n"
            f"\nNext steps:\n{next_steps}"
        )

    # Check duplicate
    async with _session_factory() as session:
        existing = await session.execute(
            select(Source.id).where(Source.content_hash == content_hash)
        )
        if existing.scalar_one_or_none():
            await _log_fetch_attempt(
                url, outcome="duplicate", final_url=final_url, http_status=status,
            )
            return (
                f"Content already fetched (duplicate hash).\n"
                f"URL: {url}"
            )

    # Store source
    async with _session_factory() as session:
        async with session.begin():
            source = Source(
                url=url,
                raw_content=raw,
                content_hash=content_hash,
                content_type=ctype,
                http_status=status,
            )
            session.add(source)
            await session.flush()
            source_id = source.id

    await _log_fetch_attempt(
        url, outcome="fetched", final_url=final_url, http_status=status,
    )
    return (
        f"Fetched and stored.\n"
        f"Source ID: {source_id}\n"
        f"URL: {url}\n"
        f"HTTP {status} | {ctype} | {len(raw):,} chars"
    )


async def _handle_extract(args: dict) -> str:
    """Extract facts from a source. Priority: Ollama > API > return content."""
    import trafilatura

    from daisy.models.source import Source
    from daisy.models.subject import Subject

    project_id = _require_active_project()
    source_id = uuid_mod.UUID(args["source_id"])
    subject_id = uuid_mod.UUID(args["subject_id"])

    async with _session_factory() as session:
        source = (await session.execute(
            select(Source).where(Source.id == source_id)
        )).scalar_one()

        subject_name = (await session.execute(
            select(Subject.name).where(Subject.id == subject_id)
        )).scalar_one()

    # Clean HTML
    clean_text = trafilatura.extract(
        source.raw_content, url=source.url, include_comments=False
    )
    if not clean_text or len(clean_text.strip()) < 50:
        return "No extractable content in this source."

    # Truncate
    max_chars = 15000
    if len(clean_text) > max_chars:
        clean_text = clean_text[:max_chars] + "\n[... truncated]"

    # Try extraction chain
    claims = await _try_extract_ollama(clean_text, subject_name)
    method = "ollama"

    if claims is None:
        claims = await _try_extract_anthropic(clean_text, subject_name)
        method = "anthropic_api"

    if claims is None:
        # Fallback: return content for in-context extraction
        return (
            f"EXTRACTION_NEEDED\n"
            f"No Ollama or API key available. "
            f"Please extract facts from the following content "
            f"about {subject_name} and call daisy_add_fact for each.\n"
            f"Source: {source.url}\n"
            f"Subject ID: {subject_id}\n\n"
            f"---\n\n{clean_text}"
        )

    # Store extracted facts
    from daisy.models.fact import Fact

    fact_ids = []
    async with _session_factory() as session:
        async with session.begin():
            for claim in claims:
                event_date = None
                if claim.get("event_date"):
                    try:
                        event_date = date.fromisoformat(
                            claim["event_date"]
                        )
                    except ValueError:
                        pass

                fact = Fact(
                    project_id=project_id,
                    subject_id=subject_id,
                    claim_text=claim["claim_text"],
                    claim_type=claim.get("claim_type", "other"),
                    source_url=source.url,
                    source_type=_infer_source_type(source.url),
                    raw_snippet=claim.get(
                        "raw_snippet", ""
                    )[:500],
                    event_date=event_date,
                    local_confidence=claim.get(
                        "confidence", DEFAULT_FACT_CONFIDENCE
                    ),
                )
                session.add(fact)
                await session.flush()
                fact_ids.append(str(fact.id))

    return (
        f"Extracted {len(fact_ids)} facts via {method}.\n"
        f"Source: {source.url}\n"
        f"Fact IDs: {', '.join(fact_ids[:5])}"
        + (f" ... and {len(fact_ids)-5} more" if len(fact_ids) > 5 else "")
    )


async def _handle_add_fact(args: dict) -> str:
    from daisy.models.fact import Fact

    project_id = _require_active_project()
    subject_id = uuid_mod.UUID(args["subject_id"])

    event_date = None
    if args.get("event_date"):
        try:
            event_date = date.fromisoformat(args["event_date"])
        except ValueError:
            pass

    async with _session_factory() as session:
        async with session.begin():
            fact = Fact(
                project_id=project_id,
                subject_id=subject_id,
                claim_text=args["claim_text"],
                claim_type=args.get("claim_type", ClaimType.OTHER),
                source_url=args.get("source_url", "user_input"),
                source_type=args.get("source_type", "user_input"),
                raw_snippet=args.get("raw_snippet", "")[:500],
                event_date=event_date,
                local_confidence=args.get("confidence", 0.9),
                lifecycle_state=LifecycleState.UNCONFIRMED,
                attributes=args.get("metadata"),
            )
            session.add(fact)
            await session.flush()
            return f"Fact added: {fact.id}\n{args['claim_text']}"


async def _handle_facts(args: dict) -> str:
    from daisy.models.fact import Fact

    project_id = _require_active_project()
    query = select(Fact).where(Fact.project_id == project_id)

    if args.get("subject_id"):
        query = query.where(
            Fact.subject_id == uuid_mod.UUID(args["subject_id"])
        )
    if args.get("claim_type"):
        query = query.where(Fact.claim_type == args["claim_type"])
    if args.get("lifecycle_state"):
        query = query.where(
            Fact.lifecycle_state == args["lifecycle_state"]
        )
    if args.get("min_confidence"):
        query = query.where(
            Fact.local_confidence >= args["min_confidence"]
        )

    limit = args.get("limit", 50)
    query = query.order_by(
        Fact.local_confidence.desc()
    ).limit(limit)

    async with _session_factory() as session:
        result = await session.execute(query)
        facts = result.scalars().all()

    if not facts:
        return "No facts found matching the criteria."

    lines = [f"Facts ({len(facts)}):\n"]
    for f in facts:
        date_str = f" ({f.event_date})" if f.event_date else ""
        lines.append(
            f"- [{f.lifecycle_state}] [{f.local_confidence:.0%}] "
            f"{f.claim_text}{date_str}\n"
            f"  type: {f.claim_type} | source: {f.source_url}\n"
            f"  id: {f.id}"
        )

    return "\n".join(lines)


async def _handle_confirm(args: dict) -> str:
    from daisy.models.fact import Fact, FactEvent
    from daisy.models.subject import Subject

    fact_id = uuid_mod.UUID(args["fact_id"])
    new_state = args["new_state"]
    reason = args.get("reason", "human_review")

    async with _session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(Fact).where(Fact.id == fact_id)
            )
            fact = result.scalar_one_or_none()
            if not fact:
                return "Fact not found."

            old_state = fact.lifecycle_state
            fact.lifecycle_state = new_state

            session.add(FactEvent(
                fact_id=fact_id,
                old_state=old_state,
                new_state=new_state,
                reason=reason,
            ))

            # Capture for graph update
            fact_data = {
                "project_id": str(fact.project_id),
                "subject_id": str(fact.subject_id),
                "fact_id": str(fact.id),
                "claim_text": fact.claim_text,
                "claim_type": fact.claim_type,
                "source_url": fact.source_url,
            }

            # Get subject name
            subject_name = ""
            if fact.subject_id:
                sresult = await session.execute(
                    select(Subject.name).where(
                        Subject.id == fact.subject_id
                    )
                )
                subject_name = sresult.scalar_one_or_none() or ""

    # Update graph based on state transition
    try:
        from daisy.graph import (
            add_fact_to_graph,
            remove_fact_from_graph,
            update_edge_state,
        )

        if new_state in (
            LifecycleState.CONFIRMED,
            LifecycleState.LIKELY,
        ):
            add_fact_to_graph(
                project_id=fact_data["project_id"],
                subject_id=fact_data["subject_id"],
                subject_name=subject_name,
                fact_id=fact_data["fact_id"],
                claim_text=fact_data["claim_text"],
                claim_type=fact_data["claim_type"],
                source_url=fact_data["source_url"],
                state=new_state,
            )
        elif new_state == LifecycleState.DISCARDED:
            remove_fact_from_graph(fact_data["fact_id"])
        elif new_state == LifecycleState.NEEDS_REVALIDATION:
            update_edge_state(
                fact_data["fact_id"], new_state
            )
    except Exception:
        pass  # Graph is best-effort, Postgres is truth

    return (
        f"Fact {fact_id}: {old_state} -> {new_state}\n"
        f"Reason: {reason}"
    )


async def _handle_report(args: dict) -> str:
    from daisy.models.fact import Fact
    from daisy.models.project import Project
    from daisy.models.subject import Subject, SubjectAlias

    project_id = _require_active_project()

    async with _session_factory() as session:
        project = (await session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()

        # Subjects
        subjects = (await session.execute(
            select(Subject).where(Subject.project_id == project_id)
        )).scalars().all()

        # Filter by subject if specified
        fact_query = select(Fact).where(Fact.project_id == project_id)
        if args.get("subject_id"):
            fact_query = fact_query.where(
                Fact.subject_id == uuid_mod.UUID(args["subject_id"])
            )
        fact_query = fact_query.order_by(Fact.local_confidence.desc())
        facts = (await session.execute(fact_query)).scalars().all()

        # Aliases
        aliases = []
        for s in subjects:
            result = await session.execute(
                select(SubjectAlias).where(
                    SubjectAlias.subject_id == s.id
                )
            )
            aliases.extend(result.scalars().all())

    lines = [
        f"PROJECT: {project.name}",
        f"Status: {project.status}",
        f"Subjects: {len(subjects)}",
        f"Facts: {len(facts)}",
        "",
    ]

    for s in subjects:
        s_aliases = [a for a in aliases if a.subject_id == s.id]
        s_facts = [f for f in facts if f.subject_id == s.id]
        lines.append(f"--- {s.name} (ID: {s.id}) ---")
        lines.append(f"Aliases: {len(s_aliases)}")

        # Facts by type
        by_type: dict[str, list] = {}
        for f in s_facts:
            by_type.setdefault(f.claim_type, []).append(f)

        for ctype in sorted(by_type):
            items = by_type[ctype]
            lines.append(f"\n  [{ctype.upper()}] ({len(items)})")
            for f in items:
                conf = f"{f.local_confidence:.0%}"
                state = f.lifecycle_state
                date_str = ""
                if f.event_date:
                    date_str = f" ({f.event_date})"
                lines.append(
                    f"    [{state}] [{conf}] "
                    f"{f.claim_text}{date_str}"
                )
                if f.attributes:
                    for k, v in f.attributes.items():
                        lines.append(f"      {k}: {v}")

        lines.append("")

    # Summary
    state_counts: dict[str, int] = {}
    for f in facts:
        state_counts[f.lifecycle_state] = (
            state_counts.get(f.lifecycle_state, 0) + 1
        )
    lines.append("FACT SUMMARY:")
    for state, count in sorted(state_counts.items()):
        lines.append(f"  {state}: {count}")

    return "\n".join(lines)


async def _handle_sources(args: dict) -> str:
    from daisy.models.fact import Fact
    from daisy.models.source import Source

    project_id = _require_active_project()
    limit = args.get("limit", 20)

    # Get source URLs from facts in this project
    async with _session_factory() as session:
        fact_query = select(Fact.source_url.distinct()).where(
            Fact.project_id == project_id
        )
        if args.get("subject_id"):
            fact_query = fact_query.where(
                Fact.subject_id == uuid_mod.UUID(args["subject_id"])
            )
        urls = (await session.execute(fact_query)).scalars().all()

        if not urls:
            return "No sources found for this project."

        sources = (await session.execute(
            select(Source)
            .where(Source.url.in_(urls))
            .order_by(Source.fetch_date.desc())
            .limit(limit)
        )).scalars().all()

    lines = [f"Sources ({len(sources)}):\n"]
    for s in sources:
        size = f"{len(s.raw_content):,} chars" if s.raw_content else ""
        lines.append(
            f"- {s.url}\n"
            f"  ID: {s.id} | HTTP {s.http_status} | "
            f"{s.content_type} | {size}"
        )

    return "\n".join(lines)


async def _handle_fetch_summary(args: dict) -> str:
    from urllib.parse import urlparse

    from daisy.models.fetch_attempt import FetchAttempt

    project_id = _require_active_project()

    async with _session_factory() as session:
        rows = (await session.execute(
            select(FetchAttempt)
            .where(FetchAttempt.project_id == project_id)
            .order_by(FetchAttempt.attempted_at.desc())
        )).scalars().all()

    if not rows:
        return "No fetch attempts recorded for this project yet."

    by_outcome: dict[str, list] = {}
    for r in rows:
        by_outcome.setdefault(r.outcome, []).append(r)

    fetched = len(by_outcome.get("fetched", []))
    auth_walls = by_outcome.get("auth_wall", [])
    errors = by_outcome.get("error", [])
    dupes = len(by_outcome.get("duplicate", []))

    lines = [
        f"Fetch summary: {fetched} fetched | {len(auth_walls)} auth walls "
        f"| {len(errors)} errors | {dupes} duplicates"
    ]

    if auth_walls:
        by_domain: dict[str, list] = {}
        for r in auth_walls:
            domain = urlparse(r.url).netloc.replace("www.", "")
            by_domain.setdefault(domain, []).append(r)

        lines.append("\nAuth walls (need browser session):")
        for domain, items in sorted(by_domain.items()):
            perm = "permanent" if items[0].permanent else "possibly temporary"
            tool = items[0].recommended_tool or "Claude-in-Chrome"
            lines.append(f"  {domain} — {len(items)} URL(s), {perm} → {tool}")
            for r in items:
                lines.append(f"    {r.url}")

        permanent_domains = sorted({
            urlparse(r.url).netloc.replace("www.", "")
            for r in auth_walls if r.permanent
        })
        if permanent_domains:
            lines.append(
                f"\nTo proceed with these, confirm:\n"
                f"  1. Claude-in-Chrome extension is connected\n"
                f"  2. You are logged into: {', '.join(permanent_domains)}\n"
                f"Or tell me which sites to skip."
            )

    if errors:
        lines.append("\nErrors (may be retryable):")
        for r in errors:
            lines.append(f"  {r.url}: {r.error_message or 'unknown error'}")

    return "\n".join(lines)


async def _handle_fetch_log(args: dict) -> str:
    from daisy.models.fetch_attempt import FetchAttempt

    project_id = _require_active_project()
    outcome_filter = args.get("outcome")
    limit = int(args.get("limit", 50))

    async with _session_factory() as session:
        q = (
            select(FetchAttempt)
            .where(FetchAttempt.project_id == project_id)
            .order_by(FetchAttempt.attempted_at.desc())
            .limit(limit)
        )
        if outcome_filter:
            q = q.where(FetchAttempt.outcome == outcome_filter)
        rows = (await session.execute(q)).scalars().all()

    if not rows:
        filter_note = f" with outcome={outcome_filter!r}" if outcome_filter else ""
        return f"No fetch attempts found{filter_note} for this project."

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    summary = " | ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    lines = [f"Fetch log ({len(rows)} shown) — {summary}\n"]

    for r in rows:
        ts = str(r.attempted_at)[:16] if r.attempted_at else ""
        line = f"[{r.outcome.upper()}] {r.url}"
        if r.http_status:
            line += f" (HTTP {r.http_status})"
        if r.auth_wall_reason:
            perm = "permanent" if r.permanent else "temporary"
            line += f"\n  Auth wall: {r.auth_wall_reason} [{perm}]"
            line += f"\n  Use: {r.recommended_tool}"
        if r.error_message:
            line += f"\n  Error: {r.error_message}"
        if ts:
            line += f"\n  {ts}"
        lines.append(line)

    return "\n".join(lines)


async def _handle_build_graph(args: dict) -> str:
    from daisy.graph import (
        ensure_schema,
        get_project_graph_summary,
        rebuild_project_graph,
    )

    project_id = _require_active_project()

    ensure_schema()
    count = await rebuild_project_graph(str(project_id))
    summary = get_project_graph_summary(str(project_id))

    lines = [f"Graph rebuilt from {count} facts.\n"]
    if summary:
        lines.append("Graph contents:")
        for key, cnt in sorted(summary.items()):
            lines.append(f"  {key}: {cnt}")
    else:
        lines.append("Graph is empty.")

    lines.append(
        "\nView at http://localhost:7474 "
        "(login: neo4j / daisy_dev)"
    )
    return "\n".join(lines)


async def _handle_graph(args: dict) -> str:
    from daisy.graph import get_person_connections

    subject_id = args["subject_id"]
    connections = get_person_connections(subject_id)

    if not connections:
        return "No connections found in graph."

    lines = [f"Connections ({len(connections)}):\n"]
    for c in connections:
        target = c["target_name"] or c["target_id"]
        lines.append(
            f"  -[{c['relationship']}]-> "
            f"{c['target_type']}: {target} "
            f"[{c['state']}] "
            f"({c['fact_count']} facts)"
        )
        if c.get("claim"):
            lines.append(f"    {c['claim']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Crawl4AI helpers
# ---------------------------------------------------------------------------


async def _fetch_clean_via_crawl4ai_url(url: str) -> str | None:
    """Ask Crawl4AI to fetch a public URL and return fit markdown via /md."""
    if not settings.crawl4ai_url:
        return None

    headers = {"Content-Type": "application/json"}
    if settings.crawl4ai_api_token:
        headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.crawl4ai_url.rstrip('/')}/md",
                json={"url": url, "f": "fit"},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("markdown") or None
    except Exception:
        return None


def _html_to_markdown(html: str, url: str) -> str | None:
    """Convert raw HTML to clean text using trafilatura (already a dependency)."""
    import trafilatura

    result = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        output_format="markdown",
    )
    return result or None


async def _handle_fetch_clean(args: dict) -> str:
    """Fetch a URL (or use provided HTML) and return clean markdown via Crawl4AI."""
    url = args["url"]
    html = args.get("html")

    if html:
        # Pre-fetched HTML (e.g. from Tethernet) — convert locally via trafilatura.
        # Crawl4AI /md only accepts URLs; /html returns cleaned HTML not markdown.
        markdown = _html_to_markdown(html, url)
        if not markdown:
            return "Extraction returned no content from the provided HTML."
        return f"URL: {url}\nChars: {len(markdown):,}\n\n{markdown}"

    # Public URL — delegate to Crawl4AI /md for best-quality extraction
    markdown = await _fetch_clean_via_crawl4ai_url(url)
    if markdown is None:
        return "Crawl4AI extraction failed or returned empty content."

    return f"URL: {url}\nChars: {len(markdown):,}\n\n{markdown}"


# ---------------------------------------------------------------------------
# Search API helpers
# ---------------------------------------------------------------------------


# (domain fragment, reason, recommended_tool, permanent)
_AUTH_WALL_DOMAINS: list[tuple[str, str, str, bool]] = [
    ("linkedin.com",              "LinkedIn requires login",    "Claude-in-Chrome", True),
    ("facebook.com",              "Facebook requires login",    "Claude-in-Chrome", True),
    ("instagram.com",             "Instagram requires login",   "Claude-in-Chrome", True),
    ("nextdoor.com",              "Nextdoor requires login",    "Claude-in-Chrome", True),
    ("twitter.com",               "Twitter/X requires login",   "Claude-in-Chrome", True),
    ("x.com",                     "Twitter/X requires login",   "Claude-in-Chrome", True),
    ("glassdoor.com",             "Glassdoor requires login",   "Claude-in-Chrome", True),
    ("reddit.com",                "Reddit age/login gate",      "Claude-in-Chrome", True),
    ("login.microsoftonline.com", "Microsoft SSO redirect",     "Claude-in-Chrome", True),
    ("accounts.google.com",       "Google SSO redirect",        "Claude-in-Chrome", True),
]

_AUTH_WALL_URL_PATTERNS: list[tuple[str, str, bool]] = [
    ("/login",              "redirected to login page",       True),
    ("/signin",             "redirected to sign-in page",     True),
    ("/sign-in",            "redirected to sign-in page",     True),
    ("/log-in",             "redirected to login page",       True),
    ("/auth/",              "redirected to auth page",        True),
    ("/accounts/login",     "redirected to login page",       True),
    ("/session/new",        "redirected to session page",     True),
    ("/users/sign_in",      "redirected to sign-in page",     True),
    ("/authwall",           "LinkedIn authwall redirect",     True),
    ("/i/flow/login",       "Twitter login flow redirect",    True),
    ("challenge?returnurl", "challenge/captcha redirect",     False),
]

_AUTH_WALL_CONTENT_PATTERNS: list[tuple[str, str, bool]] = [
    ("sign in to",              "login page content",   True),
    ("log in to",               "login page content",   True),
    ("please log in",           "login page content",   True),
    ("please sign in",          "login page content",   True),
    ("you must be logged in",   "login page content",   True),
    ("login required",          "login page content",   True),
    ("sign up to view",         "login page content",   True),
    ("join to see",             "login page content",   True),
    ("members only",            "login page content",   True),
    ("join nextdoor",           "login page content",   True),
    ("join linkedin",           "login page content",   True),
    ("connect with linkedin",   "login page content",   True),
    ("create an account to",    "login page content",   True),
    ("just a moment",           "Cloudflare challenge", False),
    ("checking your browser",   "Cloudflare challenge", False),
    ("enable javascript and cookies", "Cloudflare challenge", False),
    ("ray id",                  "Cloudflare block",     False),
]


def _detect_auth_wall(
    status: int, final_url: str, body: str
) -> tuple[str, str, bool] | None:
    """Return (reason, recommended_tool, permanent) if auth wall detected, else None."""
    url_lower = final_url.lower()

    if status == 429:
        return ("HTTP 429 — rate limited", "retry daisy_fetch after a short wait", False)

    # Known auth-walled domains (most specific)
    for domain, reason, tool, permanent in _AUTH_WALL_DOMAINS:
        if domain in url_lower:
            return (reason, tool, permanent)

    if status == 401:
        return ("HTTP 401 — authentication required", "Claude-in-Chrome", True)
    if status == 403:
        body_lower = body[:2000].lower()
        if "cloudflare" in body_lower or "just a moment" in body_lower:
            return ("Cloudflare bot detection (HTTP 403)", "retry daisy_fetch", False)
        return ("HTTP 403 — access denied", "Claude-in-Chrome", True)

    for pat, reason, permanent in _AUTH_WALL_URL_PATTERNS:
        if pat in url_lower:
            return (f"{reason} ({pat})", "Claude-in-Chrome", permanent)

    body_lower = body[:4000].lower()
    for pat, reason, permanent in _AUTH_WALL_CONTENT_PATTERNS:
        if pat in body_lower:
            return (f"{reason} (\"{pat}\")", "Claude-in-Chrome", permanent)

    return None


async def _search_api(query: str) -> list[dict]:
    if settings.brave_search_api_key:
        return await _search_brave(query, settings.brave_search_api_key)
    elif settings.searxng_url:
        return await _search_searxng(query, settings.searxng_url)
    return []


async def _search_brave(query: str, api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 10},
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("description", ""),
        }
        for item in data.get("web", {}).get("results", [])
    ]


async def _search_searxng(query: str, base_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "pageno": 1},
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("content", ""),
        }
        for item in data.get("results", [])
    ]


# ---------------------------------------------------------------------------
# Extraction chain helpers
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are an OSINT fact extraction assistant. Given text content from a web page, \
extract all claims that could relate to a person named "{name}".

For each claim, return a JSON object with:
- "claim_text": a concise statement of the claim
- "claim_type": one of: employment, education, location, account, \
relationship, publication, legal, other
- "event_date": date if mentioned (YYYY-MM-DD format), or null
- "confidence": your confidence this claim relates to the named person (0.0-1.0)
- "raw_snippet": the exact text passage supporting this claim (max 500 chars)

Return a JSON array of claim objects. If no relevant claims, return [].
Only return the JSON array, no other text.
"""


async def _try_extract_ollama(
    text: str, subject_name: str
) -> list[dict] | None:
    if not settings.ollama_url:
        return None

    prompt = _EXTRACTION_PROMPT.format(name=subject_name)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{settings.ollama_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.ollama_extraction_model,
                    "prompt": f"{prompt}\n\n---\n\n{text}",
                    "stream": False,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return _parse_claims_json(data.get("response", ""))
    except Exception:
        return None


async def _try_extract_anthropic(
    text: str, subject_name: str
) -> list[dict] | None:
    if not settings.anthropic_api_key:
        return None

    prompt = _EXTRACTION_PROMPT.format(name=subject_name)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.extraction_model,
                    "max_tokens": 4096,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"{prompt}\n\n---\n\n{text}",
                        }
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            response_text = data["content"][0]["text"]
            return _parse_claims_json(response_text)
    except Exception:
        return None


def _parse_claims_json(text: str) -> list[dict]:
    """Parse JSON array of claims from LLM response."""
    try:
        claims = json.loads(text)
        if isinstance(claims, list):
            return claims
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in response
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            claims = json.loads(text[start : end + 1])
            if isinstance(claims, list):
                return claims
        except json.JSONDecodeError:
            pass

    return []


def _infer_source_type(url: str) -> str:
    domain = url.lower()
    social = ["linkedin.com", "twitter.com", "x.com",
              "facebook.com", "instagram.com",
              "github.com", "gitlab.com"]
    if any(s in domain for s in social):
        return SourceType.SOCIAL_PROFILE
    forums = ["reddit.com", "news.ycombinator.com"]
    if any(s in domain for s in forums):
        return SourceType.FORUM
    blogs = ["medium.com", "substack.com", "wordpress.com"]
    if any(s in domain for s in blogs):
        return SourceType.BLOG
    news = ["reuters.com", "apnews.com", "bbc.com",
            "cnn.com", "nytimes.com"]
    if any(s in domain for s in news):
        return SourceType.NEWS
    return SourceType.OTHER


# ---------------------------------------------------------------------------
# Methodology guide
# ---------------------------------------------------------------------------


def _get_methodology() -> str:
    return """\
# Daisy Search — OSINT Research Methodology

## Session Start

1. Call `daisy_projects` to see all investigations
2. Call `daisy_switch_project` to set the active project,\
 or `daisy_new_project` to start fresh
3. Check for `notes/SESSION_STATE.md` in the project \
workspace — if it exists, read it FIRST. This is the \
checkpoint from the last session with the full profile, \
outstanding questions, and unexplored categories.
4. Call `daisy_report` to see current fact counts and status
5. If resuming: pick up from the outstanding questions \
and unexplored categories in SESSION_STATE.md

## Research Workflow

### Phase 0: Tethernet Pre-Flight & \
Authenticated Sessions Setup

**CRITICAL: Tethernet is a single stateful \
browser process — one instance, one agent, \
not one per search thread.** It must be \
established BEFORE the search cycle starts. \
You cannot spin it up mid-cycle when a thread \
needs it. The entire parallel fan-out depends \
on Tethernet being ready at launch time.

#### Step 0A: Determine which step groups \
will need Tethernet this investigation

Based on Phase 1 intake and Phase 1.5 \
geographic disambiguation, identify which \
step groups in the planned cycle require \
browser automation. Mark each as TETHERNET \
or FETCH/API in your thread plan.

**Step groups that ALWAYS need Tethernet \
(form-based portals, no clean API):**
- Group 5: Voter registration (state portals)
- Group 6: Property & tax records (county portals)
- Group 7: Deed & transfer records (county clerk)
- Group 8: Marriage, divorce & court records
- Group 10: State DOC / criminal court portals
- Group 11: Bankruptcy & lien portals (some)
- Group 25: State ethics / campaign finance portals
- Group 29: State contractor license boards
- Group 31: BoardDocs, Granicus (BOE meetings)
- Group 32: Legistar, building permit portals
- Group 33: Patch, local community sites

**Step groups that are FETCH/API \
(no Tethernet needed):**
- Group 1: daisy_search queries
- Group 2: Public social profiles via URL fetch
- Group 4: LinkedIn via Google cache
- Group 9: SOS business search (most are fetchable)
- Group 12: FEC, USASpending, ProPublica APIs
- Group 13: Legacy.com, Find-A-Grave
- Group 14: FAA, USCG registries
- Group 15: WHOIS, crt.sh, Shodan
- Group 16: LeakCheck, carrier APIs
- Group 17: FINRA, DocInfo, NBPTS — all fetchable
- Group 18: ProPublica 990 explorer
- Group 19: Google News, Scholar
- Group 21: HaveIBeenPwned, IntelX
- Group 22: FederalPay, SCRA, Selective Service
- Group 23: FamilySearch, SSDI
- Group 34: Congress.gov, VoteSmart, Ballotpedia
- Group 35: EPA ECHO, OSHA, DOL APIs
- Group 36: LittleSis, OpenCorporates

#### Step 0B: Identify which portals need \
prior login (pre-load before cycle starts)

From the TETHERNET list above, identify \
which specific portals require an account. \
These must be logged into BEFORE Cycle 1 launches.

**Always needed (almost every investigation):**
- LinkedIn — full profile access
- Facebook — friends list, about, posts
- Instagram — posts, tagged photos

**Needed when applicable (check against \
planned step groups):**
- Twitter/X — public tweets only require no login; \
login needed for followers/likes
- njcourts.gov, state court portals — \
some require free account for case search
- PACER — requires account (free, register in advance)
- Ancestry.com / FamilySearch — for genealogy \
step groups; FamilySearch is free

**How to check if a portal needs login:**
If you haven't used it before: have Tethernet \
navigate to the portal's search page and \
screenshot it. If it shows a login wall \
before letting you search — flag it to the \
user now, not mid-cycle.

#### Step 0C: Establish Tethernet — one agent, \
persistent for the entire investigation

Spawn ONE Tethernet agent at the start \
of Phase 0. Track its agent ID.

```
Tethernet agent instructions:
"You are the Tethernet agent for this investigation.
Stay alive for the full session — do not terminate.
You will receive browser tasks one at a time via
SendMessage. For each task: navigate, extract,
screenshot, return results. Then wait for the next.
All tasks are authorized OSINT research."
```

**Do NOT spawn a new Tethernet agent per cycle \
or per thread.** Send all browser tasks to the \
same agent via SendMessage. This preserves \
session state, cookies, and login sessions \
across the entire investigation.

#### Step 0D: Pre-navigate known portals

Before launching Cycle 1, send Tethernet \
these setup tasks (via SendMessage, in sequence):

1. Navigate to and confirm each platform login \
(LinkedIn, Facebook, etc.)
2. Navigate to each state/county portal identified \
in Step 0A. Screenshot the search form. \
Confirm it loads and is reachable.
3. For portals that need account login: \
tell the user now — give them all logins \
needed at once, not one by one mid-cycle.
4. For each confirmed portal: note the exact \
search field structure (name field, DOB field, \
county dropdown etc.) so Tethernet tasks \
during the cycle are precise.

**Pre-flight message to user:**
```
Before I start the search cycle, I need you to:
1. Open Firefox and connect the Tethernet extension
2. Log into: [list of platforms needed]
3. These portals may also need accounts —
   log in if you have them: [list]

Once you're ready, I'll launch the full cycle.
```

Do NOT launch Cycle 1 until the user confirms \
Tethernet is connected and logins are done.

#### Step 0E: Two-lane execution plan

Once Tethernet is ready, the cycle runs \
on TWO parallel tracks:

**Track 1 — API/Fetch (fully parallel):**
All FETCH/API threads run simultaneously. \
No coordination needed. Fire them all at once.

**Track 2 — Tethernet (sequential queue):**
All browser-based threads queue through the \
single Tethernet agent. They run one at a time. \
The queue is pre-planned in Step 0A so no \
thread waits on setup — each task arrives \
with exact instructions (URL, field values, \
what to extract).

Tethernet queue for a typical investigation:
```
1. Property tax search → [county portal] → "<subject>"
2. Deed records → [county clerk] → "<subject>"
3. Voter registration → [state portal] → name + DOB
4. Criminal court → [state judiciary] → "<subject>"
5. BOE minutes → [boarddocs URL] → search name
6. Building permits → [city portal] → known address
... (continue through all TETHERNET step groups)
```

Track 1 finishes fast. Track 2 runs in \
the background. Wash runs when BOTH tracks \
are complete.

### Phase 1: Intake
- Call `daisy_intake` with the subject's name \
and any known details
- Review the suggested search queries
- Add known facts via the `known_facts` parameter \
— these get seeded as confirmed

---

## THE SEARCH LOOP — Core Protocol

All of Phase 2 runs inside this loop. \
Read this before touching any step group.

### How It Works

```
SEEDS → [CYCLE] → WASH → delta? → CYCLE → WASH → ...
                              ↓
                          converged → STOP
```

Every cycle fans out parallel agents across \
search threads. Each wash consolidates what \
came back and generates richer seeds. \
Repeat until nothing new surfaces.

---

### CYCLE: Fan Out

Spawn parallel agents — each owns one thread. \
A thread is one search vector: a name variant, \
an address, a relative, an employer, a username. \
Agents run the relevant step groups for their thread.

**Generate threads from current seeds.**
On Cycle 1, seeds come from intake. \
On subsequent cycles, seeds come from the wash.

**Thread types — generate ALL of these:**

*Name variants (always):*
- Full legal name: `"<full name>"`
- No middle: `"<subject>"`
- Middle initial only: `"<name variant>"`
- Middle as first: `"<name variant>" [location]`
- Common nicknames: <name> → <nickname>, <nickname>, <initial>
- Initials: `<initials>`, `<initials>`
- Maiden/prior name (if known)
- Hyphenated combos (if married)
- Misspellings of unusual names

*Location variants:*
- Name + county
- Name + city
- Name + ZIP
- Name + prior addresses found
- Name + employer city (if different from home)

*Relational threads:*
- Each known relative as its own thread: \
spouse name, parent names, sibling names, \
adult children — searched independently, \
not just as "the subject's wife"
- Known employers/businesses as standalone threads
- Known associates or co-owners

*Identity fragments:*
- Known email addresses
- Known usernames
- Known phone numbers
- Known license plate or vehicle (if any)
- Known business name alone

**Rules for agents:**
- Each agent runs the full relevant step groups \
for its thread — it does not filter for relevance
- Submit EVERYTHING found, no matter how tangential
- If a portal is inaccessible after 2 attempts: \
log it, move on — do not block
- Set a time/depth limit per agent (e.g. 15 min \
or 10 sources) — do not let any single thread \
consume the whole cycle
- When done, submit a structured dump: \
source, URL, raw data, any new names/addresses/ \
orgs/usernames found

**Two-lane execution — CRITICAL:**
- FETCH/API threads (Track 1): fire all in parallel
- Tethernet threads (Track 2): queue to the \
single persistent Tethernet agent via SendMessage. \
Never spawn a new Tethernet agent mid-cycle. \
See Phase 0 for the pre-planned queue.
- Wash runs only after BOTH tracks are complete.

---

### WASH: Consolidate & Connect

Run after every cycle. This is not a search step — \
it is a synthesis step. One agent or the \
orchestrator does this.

**Step 1 — Deduplicate**
Collapse identical facts. Same address from \
3 sources = one fact, 3 confirmations.

**Step 2 — Normalize**
Merge facts that refer to the same entity:
- "<name variant>" + "<initials>" + "<subject>" \
→ one subject node, all confirmed aliases
- "123 Main St" + "123 Main Street Apt 2" \
→ one address, note unit

**Step 3 — Connect**
Look for facts from different agents that \
link to each other:
- Agent A found employer "<employer> <city> <state>" \
+ Agent B found address "<city> <state>" → same county, \
same person, link them
- Agent C found relative "<relative>" + \
Agent D found property record "<relative> <address>" \
→ likely same person, flag for verification

Any fact that connects — even loosely — to \
another fact from the same cycle: KEEP IT. \
Do not discard tangential finds at this stage. \
Discard happens only at convergence check.

**Step 4 — Score novelty**
For each fact, mark it:
- `NEW` — not seen in any prior cycle
- `CONFIRMED` — matches prior cycle data exactly
- `EXTENDED` — prior cycle had partial; \
this cycle added detail (new field, date, etc.)

**Step 5 — Generate next seed list**
Every NEW or EXTENDED fact that contains a \
name, address, username, org, or phone \
becomes a seed for the next cycle.
Add it to the thread queue.

**Step 6 — Convergence check**
Count `NEW` facts this cycle vs last cycle.
- If NEW count is still meaningful → start next cycle
- If NEW count ≈ 0 for 2 consecutive cycles → \
that thread has converged. Mark it DONE.
- When ALL active threads are DONE → stop.

Do not stop early. "Seems like enough" is not \
convergence. Run until the math says stop.

---

### CONVERGENCE RULES

A thread converges when:
- It returned zero NEW facts in the last cycle AND
- It returned zero NEW facts in the cycle before

A thread does NOT converge just because:
- The portal returned no results (different portals \
may still have data — try others)
- The agent hit a login wall (flag, ask user, retry)
- A step group was skipped (go back and run it)

When a thread converges, DROP all subsequent \
searches for that exact seed. \
Do NOT drop related threads — those run \
to their own convergence independently.

---

### PRACTICAL CYCLE SIZES

| Cycle | Agents | Focus |
|-------|--------|-------|
| 1 | 5–15 | All name variants + known location + known employer |
| 2 | 10–30 | Relatives + new addresses found + new usernames found |
| 3+ | As needed | New seeds from wash; shrinks each cycle as threads converge |

In Claude Code: run step groups as parallel \
`daisy_search` + `daisy_fetch` batches, \
one batch per thread. After all batches \
complete: run the wash before starting \
the next cycle.

---

### Phase 1.5: Geographic Disambiguation

**CRITICAL — Do this before running any \
location-dependent searches.**

Do NOT begin searching "[Name] [State]" across \
all portals if you only know the state. State-level \
queries return too much noise for common names and \
miss county/township-specific portals entirely. \
Narrow first, then search.

#### Step A: Establish the most specific \
location known

Ask the user — do NOT proceed without this:
- **State** (required minimum)
- **County** — most records are county-level, not state
- **City / Town / Township** — tighten search scope
- **ZIP code** — useful for property, voter, court portals

If the user doesn't know the county:
1. Ask for the city/town name
2. Resolve it: search "[City Name] [State] county" \
to identify which county it falls in
3. Note: some cities (e.g., a city that) span \
multiple counties — ask which part or search both
4. For townships (common in NJ, PA, NY): \
identify the township AND the county — both \
matter for records

**Do NOT assume county from city name.** \
Newark NJ ≠ Newark OH. Springfield exists in \
35 states. Confirm before running location \
portals.

#### Step B: Build the location context \
before branching

Once confirmed, record these facts in the \
project as CONFIRMED before starting Step Groups:
- `address.state` = [two-letter state code]
- `address.county` = [county name] (no "County" suffix)
- `address.city` = [city or town]
- `address.township` = [if applicable — NJ/PA/NY]
- `address.zip` = [if known]

All location-dependent searches (Step Groups 5–16, \
30–36) should use **county + city**, not just state. \
A property tax search for "<subject>" in \
<county> <state> is far more precise — and \
hits the right portal — than "<subject> SC".

#### Step C: Gather additional specifics \
before searching — ask once, not piecemeal

Before starting any searches, ask the user \
for all of these in a single message. Do not \
ask one at a time as you hit each step group.

```
To search effectively, I need a few specifics:
1. Full legal name (first, middle if known, last)
2. Any known aliases, maiden name, or name changes
3. City + county (or ZIP) in [State]
4. Approximate age or year of birth
5. Current or last known employer (optional)
6. Any known usernames or email addresses
7. What you already know — so I don't repeat it
```

Collect answers to ALL of these upfront. \
Then run all step groups. \
Do NOT stop mid-investigation to ask for \
basics that should have been asked at the start.

#### Step D: Don't tunnel — track parallel \
location threads

People have records in multiple places. \
Once you start finding data, note every \
new location that appears (prior addresses, \
business addresses, summer homes, states \
where they hold licenses) and run \
location-dependent step groups for each \
new jurisdiction found. Maintain a running list:

```
Jurisdictions to search:
- [State / County / City] — primary (confirmed)
- [State / County / City] — prior address (found in Step 6)
- [State] — business registered here (found in Step 9)
```

Do not stop searching after the primary \
location is exhausted. Prior residences, \
business registrations, and professional \
licenses in other states are often where \
the most interesting records live.

**But do not block on any single thread.** \
If a portal is down, requires a login you \
don't have, or returns no results — note it \
and move on. Do not spend more than 2 attempts \
on any single source before logging it as \
"not accessible" and continuing. \
Breadth across all 36 step groups beats \
depth on one portal.

### Phase 2: Discovery & Acquisition

Execute ALL step groups below for every subject. \
Groups 1-4 are universal (same sites regardless of \
where the subject lives). Groups 5-16 and 30-36 \
are location-dependent — use county + city \
(from Phase 1.5) not just state. Groups 17-29 \
apply universally regardless of location.

#### Step Group 1: Web Search & Aggregators
**Method:** `daisy_search` (batches of 5-10 queries)
**Goal:** Cast a wide net, find profile URLs and leads.

Queries to run:
- `"[Full Name]"` (quoted exact match)
- `"[Full Name]" [city/state]` (geo-narrowed)
- `"[Full Name]" [employer]` (if known)
- `"[Full Name]" linkedin`
- `"[Full Name]" facebook`
- `"[Full Name]" instagram`
- `site:linkedin.com/in "[Full Name]"`
- `site:github.com "[Full Name]"`

Then fetch promising URLs via `daisy_fetch`.

#### Step Group 2: Social Media Profiles
**Method:** Tethernet (auth-walled) + `daisy_fetch` \
(public pages)
**Goal:** Employment, connections, photos, interests, \
family, usernames, locations.
**Requires auth:** LinkedIn, Facebook, Instagram

**LinkedIn:**
1. Navigate to profile URL
2. Screenshot header (name, headline, location)
3. Scroll to Experience — screenshot each role
4. Scroll to Education — screenshot
5. Check Connections list if accessible
6. Extract: employers, titles, dates, location, \
education, skills

**Facebook:**
1. Navigate to profile URL
2. Screenshot About section (work, education, \
places lived, family)
3. Navigate to Friends list (/friends)
4. For SPA sites: use `execute_script` to extract \
DOM data directly (e.g., friend list nodes) — this \
is more reliable than screenshots for large lists
5. Extract: family members, friends, employers, \
locations, life events

**Instagram:**
1. Navigate to profile URL
2. Screenshot bio (name, handle, bio text, link)
3. Note follower/following counts
4. For following/follower lists on SPAs: use \
`capture_network` to intercept API responses \
while scrolling — structured JSON is more complete \
than OCR from screenshots
5. Extract: username, bio, connections, locations \
from posts

**Other platforms (if found):**
- Twitter/X: bio, tweets, followers
- GitHub: repos, contributions, bio, email
- YouTube: channel info, uploads
- TikTok, Reddit, Blogger, personal websites

#### Step Group 3: People Search Aggregators
**Method:** `daisy_fetch` or Tethernet (if login \
required)
**Goal:** Addresses, phone numbers, relatives, \
associates, age/DOB.

**Sites to check (universal — work for any US \
subject):**
- TruePeopleSearch (truepeoplesearch.com) — free, \
no account needed
- Spokeo (spokeo.com) — partial free, full \
requires account
- BeenVerified (beenverified.com) — requires \
subscription
- WhitePages (whitepages.com) — partial free
- Radaris (radaris.com) — partial free
- ClustrMaps (clustrmaps.com)
- FastPeopleSearch (fastpeoplesearch.com)
- That's Them (thatsthem.com)

**Procedure for each:**
1. Search by full name + state
2. Identify the correct person by cross-referencing \
age, location, or known associates
3. Extract: addresses (current + previous), phone \
numbers, email addresses, relatives/associates, \
approximate age/DOB
4. If paywalled: ask user if they want to create an \
account. Never silently skip.

#### Step Group 4: Employment & Professional
**Method:** `daisy_search` + `daisy_fetch`
**Goal:** Career history, company affiliations, \
professional output.

**Sites/queries:**
- Crunchbase: `site:crunchbase.com "[Full Name]"`
- ZoomInfo: `site:zoominfo.com "[Full Name]"` \
(usually paywalled)
- Company websites: search "[employer] team" or \
"[employer] about" pages
- Press releases: `"[Full Name]" press release`
- News articles: `"[Full Name]" [employer] OR \
[industry]`
- Patent filings: Google Patents \
`inventor:"[Full Name]"`
- Conference talks: `"[Full Name]" speaker OR talk \
OR conference`
- Published papers: Google Scholar \
`author:"[Full Name]"`

#### Step Group 5: Voter Registration Records
**Method:** Tethernet (form-based portals)
**Goal:** Full legal name, DOB, party affiliation, \
registration address. Confirms identity.

**Universal portal:**
- stevemorse.org — covers voter records for multiple \
states (NJ, NY, FL, PA, and others). Navigate to \
the relevant state's voter records section, fill \
name fields, submit, read results.

**State-specific examples:**
- NJ: stevemorse.org > NJ Voter Records > search \
by last name + first name. Returns: full name, DOB, \
party, municipality, address.
- CA: No statewide public portal — county-level ROV \
offices. Some counties offer online lookup.
- FL: voterrecords.com or county SOE websites.
- NY: voter.nycboe.org (NYC) or county BOE portals.

**General procedure:**
1. Search "[state] voter registration lookup"
2. Navigate to portal via Tethernet
3. Fill name/DOB fields, submit
4. Extract: full legal name, DOB, address, party, \
registration date
5. Search for spouse/family members too — same \
address confirms household

#### Step Group 6: Property & Tax Records
**Method:** Tethernet (form-based county portals)
**Goal:** Property ownership, addresses, assessed \
values, purchase dates.

**Finding the right portal:**
Search: "[county name] [state] property tax search" \
or "[county] assessor records online"

Every county has its own portal. Common platforms:
- Vision Government Solutions (many NE counties)
- Tyler Technologies / ProStar (NY counties)
- County assessor websites (CA, OR, WA)

**State-specific examples:**
- NJ: taxrecords-nj.com — search by address, \
returns owner, assessment, tax amount.
- NY (Saratoga County): \
saratogacounty.prosgar.com — parcel search by \
owner name or address. Returns: market value, \
assessment, exemptions (STAR = homestead).
- CA: county assessor websites (e.g., \
assessor.lacounty.gov for LA County).

**General procedure:**
1. Identify subject's county from known address
2. Search "[county] property records" or \
"[county] tax assessor"
3. Navigate via Tethernet, search by owner name \
OR address
4. Extract: property address, owner name(s), \
assessed value, market value, purchase date, \
tax exemptions (STAR/homestead = primary residence)
5. Check for multiple properties (investment, \
vacation homes)
6. Note co-owners — often reveals spouse or \
family member

#### Step Group 7: Deed & Transfer Records
**Method:** Tethernet (county clerk/recorder portals)
**Goal:** Property purchase/sale history, mortgages, \
co-buyers (family).

**Finding the right portal:**
Search: "[county] clerk deed records online" or \
"[county] recorder of deeds"

**State-specific examples:**
- NJ (Bergen County): bclrs.co.bergen.nj.us — \
document search by name. Returns: deed transfers, \
mortgages, dates, book/page numbers. Reveals \
purchase/sale dates, lending banks, co-signers.
- NY: county clerk websites vary. Many use \
landrecords portals.
- CA: county recorder websites (often free basic \
search, paid copies).

**General procedure:**
1. Navigate to county clerk/recorder portal
2. Search by subject's last name (and first name \
if possible)
3. Filter by document type: Deed, Mortgage, \
Satisfaction
4. Extract: transaction dates, property addresses, \
parties (grantor/grantee), amounts
5. Build timeline: when did they buy/sell, where \
did they move from/to
6. Check spouse's name separately — joint ownership \
may appear under either name

#### Step Group 8: Marriage, Divorce & Court Records
**Method:** Tethernet (state/county portals)
**Goal:** Marriage date, maiden names, divorce \
filings, civil/criminal cases.

**Marriage records — Finding the portal:**
Search: "[state] marriage records search" or \
"[county] marriage index"
- Some states have statewide indexes (NJ, NY, FL)
- Others are county-level only (CA, TX)
- Many vital records offices charge for copies but \
offer free index searches

**Divorce records:**
- Often same portal as marriage records
- Search: "[state] divorce records" or \
"[county] family court records"

**Court records — General:**
- Federal: PACER (pacer.gov) — all federal civil \
and criminal cases
- State civil/criminal: each state has its own \
judiciary portal
- Search: "[state] court records search" or \
"[state] judiciary case lookup"

**State-specific examples:**
- NJ: njcourts.gov — case search for civil, \
criminal, family
- NY: iapps.courts.state.ny.us/webcivil — civil \
case search. nycourts.gov for family court.
- CA: Multiple county portals. LA uses \
lacourt.org/casesummary
- FL: Statewide myflcourtaccess.com

**General procedure:**
1. Search for relevant state/county portal
2. Navigate via Tethernet
3. Search by name (and DOB if available)
4. For marriage: extract date, location, \
spouse name, certificate number
5. For court: extract case type, filing date, \
parties, disposition
6. Note: maiden names from marriage records \
are valuable for tracing family connections

#### Step Group 9: Business & Professional Filings
**Method:** `daisy_fetch` + Tethernet
**Goal:** Business ownership, LLC filings, \
professional licenses.

**State Secretary of State portals:**
Search: "[state] secretary of state business search"
- Every state has one. Search by person name \
(as officer/agent) or business name.

**Other sources:**
- OpenCorporates (opencorporates.com) — aggregates \
business filings from multiple states
- State professional license lookups (medical, \
legal, real estate, contractor)
- SEC EDGAR (sec.gov/cgi-bin/browse-edgar) — for \
officers of public companies
- IRS Tax Exempt Organization Search — for \
nonprofit officers

**General procedure:**
1. Search state SOS portal by subject's name
2. Extract: business names, roles (officer, agent, \
member), filing dates, status, addresses
3. Cross-reference business addresses with \
known addresses

#### Step Group 10: Criminal History & Conviction Records
**Method:** `daisy_fetch` + Tethernet (state portals)
**Goal:** Arrests, convictions, incarcerations, \
sex offender status, federal criminal cases.

**Federal inmate locator:**
- BOP (bop.gov/inmateloc/) — search federal prison \
inmates by name. Returns: facility, sentence, \
release date. Free, no login.

**Sex offender registry:**
- NSOPW (nsopw.gov) — national aggregator, \
searches all 50 states + territories. Search by \
name + state. Returns: address, offenses, photo.
- State registries have more detail: use \
"[state] sex offender registry" to find direct \
state portal.

**State DOC inmate/offender lookups:**
- Every state has a Department of Corrections \
offender search. Search: "[state] DOC inmate \
search" or "[state] offender lookup"
- Examples:
  - NJ: njdoc.gov offender search
  - NY: nysdoccslookup.doccs.ny.gov
  - CA: cal.app.cdcr.ca.gov/inmatelocator
  - FL: offenderalert.fdle.state.fl.us
- Returns: offense, sentence, facility, release date

**Federal criminal court records:**
- CourtListener (courtlistener.com/recap/) — \
free PACER alternative. Search by person name. \
Federal civil AND criminal cases. API available.
- PACER (pacer.gov) — authoritative federal cases, \
$0.10/page after free tier. Requires account.

**State criminal court records:**
- Each state judiciary has case search. \
Search: "[state] criminal court case search" \
or "[state] judiciary public access"
- NJ: njcourts.gov → Case Search → Criminal
- NY: iapps.courts.state.ny.us → Criminal
- FL: myflcourtaccess.com
- CA: county-level; search "[county] superior \
court case search"

**Arrest records:**
- County sheriff portals: "[county sheriff] \
arrest records" or "recent arrests [county]"
- Many sheriffs publish 24-48hr arrest logs
- Note: arrest ≠ conviction — label accordingly

**General procedure:**
1. Run BOP inmate locator first (federal)
2. Run NSOPW national search
3. Run state DOC for subject's known states
4. Search CourtListener for federal cases
5. Navigate state criminal court portal by name
6. For each record: extract case number, charges, \
disposition, sentence, dates
7. Distinguish arrests from convictions in facts

#### Step Group 11: Liens, Judgments & Bankruptcy
**Method:** `daisy_fetch` + Tethernet
**Goal:** Civil money judgments, tax liens, \
UCC filings, bankruptcy filings.

**Federal bankruptcy:**
- PACER (pacer.gov) → Bankruptcy courts
- CourtListener has RECAP bankruptcy index
- BankruptcyData.com — free search by name, \
returns case number, chapter, filing date, \
district, assets/liabilities

**Federal tax liens:**
- Recorded at county clerk/recorder of deeds. \
Search county clerk under "federal tax lien" \
document type.
- IRS Centralized Lien Operation can confirm \
but county records are the canonical source.

**State/county civil judgments:**
- Same portals as Step Group 8 (state courts)
- Search civil case records for the subject as \
defendant — money judgments, collection actions
- NJ: njcourts.gov → Civil (Special Civil Part \
for small claims, Law Division for larger)
- NY: iapps.courts.state.ny.us/webcivil

**UCC filings (business liens):**
- State Secretary of State UCC search
- Search: "[state] UCC lien search" or \
"[state] SOS UCC filing search"
- Returns: secured party (lender), debtor, \
collateral, filing date

**General procedure:**
1. Search BankruptcyData.com by name
2. Check county clerk for federal tax liens \
(already in Step Group 7 portals)
3. Search civil court dockets as defendant
4. Run state UCC search for business subjects

#### Step Group 12: Federal Public Records
**Method:** `daisy_fetch` (all have free APIs/search)
**Goal:** Campaign donations, government contracts, \
pandemic loans, foreign agent registrations.

**Campaign finance (FEC):**
- FEC.gov/data/receipts/ — donors of $200+ \
are public record
- Direct search: fec.gov/data/receipts/ \
?contributor_name=[NAME]
- Returns: donation amounts, dates, committees, \
employer, occupation

**Government contracts (USASpending):**
- usaspending.gov — search by person or \
company for federal contracts and grants
- Also reveals which agencies awarded contracts

**PPP loans (ProPublica):**
- projects.propublica.org/coronavirus/bailouts/ \
— searchable PPP loan database
- Returns: loan amount, lender, jobs reported, \
forgiveness status

**Foreign agent registrations (FARA):**
- fara.gov/quick-search.html — who is registered \
as a foreign agent or lobbyist for foreign \
principals

**Lobbying disclosures:**
- lda.senate.gov/filings/public/filing/search/ \
— federal lobbying registrations and disclosures

**General procedure:**
1. Search FEC by name (common for political donors)
2. Search USASpending for contract/grant recipients
3. Check ProPublica PPP for business subjects
4. Check FARA if subject has international connections

#### Step Group 13: Death & Vital Records
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Confirm death, find death date/location, \
maiden names via death cert, obituaries.

**Obituaries & memorials:**
- Legacy.com — largest obituary aggregator. \
Search by name. Reveals: death date, city, \
surviving family members, birth year.
- Find-A-Grave (findagrave.com) — cemetery \
records, grave photos, dates, family links
- BillionGraves (billiongraves.com) — \
alternative to Find-A-Grave

**Social Security Death Index (SSDI):**
- FamilySearch (familysearch.org/search/collection/ \
1202535) — free SSDI search. Returns: name, \
birth date, death date, last known ZIP.

**State death records:**
- Each state vital records office. Some have \
online indexes (death before ~1970 more accessible).
- Search: "[state] vital records death index online"

**General procedure:**
1. Search Legacy.com and Find-A-Grave
2. Check SSDI on FamilySearch
3. Use death date to anchor timeline; \
spouse listed in obituary = high-value lead

#### Step Group 14: Aviation & Vessel Records
**Method:** `daisy_fetch` (all have free APIs)
**Goal:** Pilot certificates, aircraft ownership, \
boat/vessel ownership.

**FAA pilot certificate lookup:**
- amsrvs.ame.faa.gov/airmeninquiry/ \
— search by name. Returns: certificate type \
(private, commercial, ATP), ratings, \
medical class, address (sometimes).

**FAA aircraft registry (owner name search):**
- registry.faa.gov/aircraftinquiry/ \
— search by owner name. Returns: N-number, \
make/model, serial, registration address.
- ADS-B flight tracking: flightaware.com, \
flightradar24.com — historical flight paths \
for known N-numbers.

**USCG documented vessel search:**
- mvs.uscg.mil/mvs/ — USCG vessel \
documentation database. Search by owner name.
- Returns: vessel name, type, hull ID, \
home port, owner address.

**State boat registration:**
- Each state manages non-USCG vessels. \
Search: "[state] boat registration lookup"

**General procedure:**
1. Run FAA airmen inquiry by name
2. Run FAA aircraft registry by owner name
3. Run USCG documented vessel search
4. For found aircraft: search N-number on \
FlightAware for travel history

#### Step Group 15: Domain & Internet Footprint
**Method:** `daisy_fetch` (free APIs/services)
**Goal:** Domain registrations, IP exposure, \
subdomains, historical web presence.

**WHOIS / domain ownership:**
- who.is — WHOIS lookup; pre-GDPR domains \
often show registrant name/email/org
- ViewDNS.info — reverse WHOIS by email or \
name; historical WHOIS snapshots
- whoisxml API free tier for bulk lookups

**SSL certificate transparency:**
- crt.sh — search by domain or organization \
name to enumerate all SSL certs ever issued. \
Reveals subdomains, internal infrastructure.

**IP & hosting exposure:**
- Shodan (shodan.io) — search by org name or \
IP range. Reveals exposed services, ports, \
banners. Free account gives limited results.
- Censys (search.censys.io) — similar to Shodan, \
free tier available.

**Wayback Machine:**
- web.archive.org — historical snapshots of \
websites. Useful for deleted content, old \
contact info, prior ownership.

**General procedure:**
1. Search ViewDNS reverse WHOIS by subject's \
email addresses
2. Run crt.sh for any domains found
3. Check Wayback Machine for deleted pages
4. Shodan/Censys only if subject runs servers \
or a business with web infrastructure

#### Step Group 16: Phone Intelligence
**Method:** `daisy_fetch`
**Goal:** Carrier, line type, owner association \
for known phone numbers.

**Carrier / line type lookup:**
- carrier.okfreephone.com or similar free \
carrier lookup APIs — identify if number is \
mobile, VoIP, or landline; carrier name
- CNAM lookup (caller ID name): \
opencnam.com has a free tier

**Reverse phone:**
- 411.com, whitepages.com (free tier) — \
owner name for landlines
- For mobiles: typically requires paid data brokers \
(skip per policy — paywall garbage)
- Better approach: run number through \
`daisy_search` with quoted number

**Breach data:**
- LeakCheck (`scripts/leakcheck.sh`) supports \
phone number queries (digits only, no +1). \
Often returns associated email addresses. \
This is the highest-yield phone lookup available.

**General procedure:**
1. Run LeakCheck on any phone numbers found
2. Run carrier lookup to determine line type
3. Search quoted phone number via `daisy_search`
4. Quoted search often surfaces social profiles, \
forums, classified ads where number was posted

#### Step Group 17: Professional Licenses Deep
**Method:** `daisy_fetch` (all free public portals)
**Goal:** Financial licenses, medical licenses, \
legal licenses, real estate, contractor, \
insurance, and other regulated professions.

**Financial services (FINRA BrokerCheck):**
- brokercheck.finra.org — search any registered \
broker, advisor, or firm. Returns: licenses, \
exams passed, employer history, disclosures \
(complaints, arbitrations, regulatory actions, \
criminal disclosures). This is extremely detailed.
- SEC IAPD (adviserinfo.sec.gov) — investment \
advisers registered with SEC or states

**Legal (state bar associations):**
- Every state bar has a public member directory
- ABA directory links all states: \
americanbar.org/licensing-and-compliance/ \
attorney-licensing-state-boards/
- Returns: admission date, status, \
disciplinary history, firm, address

**Medical (state medical boards):**
- FSMB DocInfo (docinfo.org) — aggregates \
all 50 state medical board records. \
Returns: specialty, license states, \
disciplinary actions, malpractice claims.
- Search: "[state] medical board license \
verification" for state-specific portals

**Nursing (National Council of State Boards):**
- nursys.com — national nurse license \
verification across participating states

**Real estate agents:**
- ARELLO (arello.org/licensee-lookup/) — \
national aggregator of real estate licenses
- State DOL / Division of Real Estate portals

**Contractors:**
- State contractor license boards. \
Search: "[state] contractor license lookup"
- CA: cslb.ca.gov; FL: myfloridalicense.com; \
TX: tdlr.texas.gov

**Insurance agents:**
- State DOI (Department of Insurance). \
Search: "[state] insurance license lookup"
- NIPR (nipr.com) — national insurance \
producer license search

**General procedure:**
1. Always run FINRA BrokerCheck for any \
finance-adjacent subject
2. Search state bar if subject is/was attorney
3. Run DocInfo for any healthcare subject
4. Cross-check license addresses with \
known addresses (license filings use real address)
5. Check disciplinary history — often reveals \
incidents not in press

#### Step Group 18: Non-Profit & IRS Records
**Method:** `daisy_fetch`
**Goal:** Officers of charities, churches, \
foundations. Compensation, addresses, \
organizational connections.

**IRS Tax Exempt Organizations:**
- IRS EO Select Check: \
apps.irs.gov/app/eos/ — search by \
organization name or EIN. Returns: status, \
ruling date, filing requirement.

**Form 990 filings (gold mine):**
- ProPublica Nonprofit Explorer: \
projects.propublica.org/nonprofits/ \
— search by org name. Returns full 990 \
PDFs and parsed data.
- 990 reveals: officer names + compensation, \
addresses, related organizations, \
financial summary, transactions with \
officers/directors.
- Also: citizenatlas.org, guidestar.org (free \
tier), opencorporates for cross-reference

**Foundations:**
- Candid/FoundationDirectory: \
candid.org — grants database, foundation \
officers, grant recipients

**General procedure:**
1. Search ProPublica Nonprofit Explorer by \
subject name (as officer) AND by \
any orgs they're connected to
2. Download/fetch 990 PDFs for key orgs
3. Extract officer list, compensation, \
related-party transactions
4. Note all addresses on 990 — often \
home address or real business location

#### Step Group 19: Press, News & Academic Records
**Method:** `daisy_search` + `daisy_fetch`
**Goal:** News coverage, academic publications, \
conference talks, archived web content.

**News archives:**
- Google News (news.google.com) — \
search "\"[Full Name]\"" for press coverage
- ProQuest (library access): historical \
newspapers back to 1800s
- Newspapers.com — archived US newspapers
- Local newspaper sites: search "[local paper] \
[name]" — small local coverage often missed

**Government press releases:**
- Search: site:*.gov "[Name]" — reveals \
mentions in agency press releases, \
court announcements, regulatory actions

**Academic / research:**
- Google Scholar (scholar.google.com) — \
publications, citations, affiliations
- ResearchGate (researchgate.net) — \
researcher profiles, institution history
- PubMed (pubmed.ncbi.nlm.nih.gov) — \
medical/life science publications
- SSRN (ssrn.com) — social science preprints

**Conference & professional talks:**
- Search: "[Name]" site:youtube.com — \
public talks, conference recordings
- Search: "[Name]" filetype:pdf — \
conference programs, academic CVs

**Wayback Machine deep archive:**
- web.archive.org/web/*/[domain] — \
all snapshots of a personal website
- web.archive.org/web/*/[URL] — \
specific page history

**General procedure:**
1. Search Google News with quoted name
2. Search Google with site:*.gov filter
3. Check Google Scholar for academic subjects
4. Wayback Machine for any personal sites found

#### Step Group 20: Username Enumeration & \
Social Media Deep Search
**Method:** `daisy_search` + `daisy_fetch` + Tethernet
**Goal:** Find all online accounts associated \
with known usernames, emails, or phone numbers.

**Username presence checking:**
- WhatsMyName (whatsmyname.app) — checks \
username across 600+ sites. Input a \
username, get back all sites where it exists.
- Sherlock is the CLI equivalent but requires \
local install. Use WhatsMyName via browser.
- Search: "[username]" site:reddit.com, \
site:twitter.com, site:github.com, etc.

**Reddit deep search:**
- Search: site:reddit.com "[username or name]"
- Pushshift (if available) or \
camas.undead.pw — historical Reddit \
search (deleted posts may still index)
- Reddit profiles are public: \
reddit.com/user/[username]

**GitHub & code repositories:**
- github.com/[username] — public repos, \
commit history, starred repos, org memberships
- Search: "\"[email]\"" site:github.com — \
commits using a personal email address
- Git commit emails: exposed in git log on \
public repos — mine for personal addresses

**Gravatar (email → avatar):**
- gravatar.com/[MD5 of email lowercase] \
— reveals profile photo if registered
- Confirms email is live/active

**Email enumeration from domain:**
- hunter.io/search/[domain] — finds format \
and known emails at a company domain. \
Free tier: 25 searches/month
- phonebook.cz — email and domain intelligence \
(partially free)

**General procedure:**
1. For each known username: run WhatsMyName
2. Search GitHub by username and email
3. Check Reddit profile if username found
4. Run Hunter.io for business email subjects
5. Search Gravatar for each email address found

#### Step Group 21: Data Breach & Paste Intelligence
**Method:** `scripts/leakcheck.sh` + `daisy_fetch`
**Goal:** Leaked credentials, paste site \
exposures, data broker records in breach data.

**Already integrated:**
- LeakCheck (`scripts/leakcheck.sh`) — primary \
tool. Supports email, phone, username. Returns \
breach sources, passwords, PII. Run this first.

**Supplemental breach checks:**
- HaveIBeenPwned (haveibeenpwned.com/api/v3/) \
— free API, email only. Returns breach names \
and categories (no passwords). Good for \
confirming email is real and getting breach list.
- IntelligenceX (intelx.io) — searches across \
paste sites, dark web dumps, WHOIS, social. \
Free tier is limited but valuable. \
Search by email, name, domain, IP.
- Phonebook.cz — email/domain search against \
breach data collections. Free.

**Paste sites:**
- psbdmp.ws — Pastebin dump search, free. \
Search by email or name.
- Search: site:pastebin.com "[email or name]"
- Search: "[email]" site:paste.ee site:ghostbin.co

**General procedure:**
1. Run LeakCheck for all emails, phones, \
usernames found during investigation
2. Run HaveIBeenPwned for email confirmation \
and breach timeline
3. Search IntelX for deeper paste/dark web \
mentions
4. Cross-reference breach passwords against \
known usernames (password reuse pattern analysis)

#### Step Group 22: Military & Government \
Employment Records
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Military service, veteran status, \
federal employment, security clearance hints.

**Military service verification:**
- SCRA (scra.dmdc.osd.mil) — Servicemembers \
Civil Relief Act database. Confirms if \
someone is currently on active duty. Free.
- Defense Manpower Data Center (dmdc.osd.mil) \
— some public access tools for verification

**Veterans records:**
- VA.gov does not expose records publicly
- Approach: obituaries/death records often \
list military branch/service
- Search: "[Name]" veteran memorial OR \
military branch site:*.gov
- Memorial databases: vvmf.org (Vietnam), \
abmc.gov (WWII overseas burials)

**Selective Service:**
- sss.gov/verify — verify if male registered \
for Selective Service (birth years 1960-present)

**Federal employee salary data:**
- FederalPay.org — federal employee \
salaries are public (FOIA releases). \
Search by name. Returns: agency, grade, \
salary, location.
- OPM data sets via data.gov

**State employee salary data:**
- Many states publish salary data publicly. \
Search: "[state] public employee salary database" \
or "[state] government salaries lookup"
- Often searchable by name at state transparency \
portals or news organization databases

**General procedure:**
1. Run SCRA check for active duty verification
2. Search FederalPay.org by name
3. Check state salary databases for state workers
4. Search Selective Service for male subjects
5. Search memorial databases for deceased veterans

#### Step Group 23: Genealogy & Family \
Connections
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Parents, siblings, children, extended \
family, historical addresses, maiden names.

**Free genealogy databases:**
- FamilySearch (familysearch.org) — largest \
free genealogy database. Census records, \
vital records, family trees. No login required \
for most searches.
- Ancestry.com — best data but requires paid \
account. Public trees searchable without \
account at ancestrylibrary.com (via libraries).
- MyHeritage (myheritage.com) — large European \
records, some free searching.

**Census records (1940 and earlier, public):**
- FamilySearch has 1940 census fully indexed. \
1950 census released in 2022 and indexed.
- Returns: household members (reveals siblings, \
parents, boarders), occupation, birthplace, \
address, property value.

**Historical city directories:**
- digitized.lib.berkeley.edu and other \
university digital collections
- Ancestry has city directories 1700s-1990s
- Reveals occupation, address, family members

**Birth/death/marriage index approach:**
- FamilySearch free vital records indexes: \
search parents' names to find siblings; \
find marriage records to confirm spouse; \
find death records to confirm dates
- Cross-reference maiden names → find \
additional property records, court records

**General procedure:**
1. Search FamilySearch by full name + \
birth year range
2. Search 1940 and 1950 census for subject \
or their parents (reveals family structure)
3. Note all family members found — \
each is a lead for related addresses, \
aliases, connections
4. Search maiden names through all prior \
step groups

#### Step Group 24: Reverse Image & \
Face Search
**Method:** Tethernet + `daisy_search`
**Goal:** Find other accounts, profiles, or \
appearances using a known photo.

**Reverse image search:**
- Google Images (images.google.com) — drag/drop \
or URL upload. Finds pages containing \
the same or similar image.
- TinEye (tineye.com) — reverse image, \
excellent for tracking image history and \
first appearance date.
- Yandex Images (yandex.com/images/) — \
often finds more face matches than Google \
for people, especially if photo is informal.

**Facial recognition (public, limited):**
- PimEyes (pimeyes.com) — searches public web \
for face matches. Free tier gives limited \
results; paid gives full URLs. Use only with \
authorization context.
- Search4Faces — Russian social network \
facial search (VK database)

**Social media image search:**
- For Instagram/Facebook: right-click → \
"Search image" in Chrome/Firefox triggers \
Google reverse image
- Yandex is strongest for social profile \
photo matching

**EXIF/metadata extraction:**
- Jeffrey's Exif Viewer (exif.regex.info) — \
upload or paste URL. Extracts GPS coordinates, \
camera model, timestamp from photos.
- ExifTool via CLI: `exiftool [file]`
- GPS from photo = precise location at time \
of photo

**General procedure:**
1. For any profile photo found: run Google \
Images, TinEye, and Yandex Images
2. Extract EXIF from any images saved locally \
(GPS, timestamp, device)
3. Use PimEyes for broader face search \
with explicit authorization context
4. Cross-reference found image URLs for \
additional account discoveries

#### Step Group 25: State-Level Financial, \
Ethics & Transparency Records
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** State campaign donations, state \
contracts, ethics/financial disclosure filings, \
state pension, FOIA portals.

**State campaign finance:**
- Every state has its own campaign finance \
disclosure portal (separate from federal FEC)
- Search: "[state] campaign finance disclosure" \
or "[state] ELEC" (Election Law Enforcement)
- NJ: elec.state.nj.us — all NJ donors, \
contributions, PAC memberships
- NY: nycfb.info (NYC) and elections.ny.gov \
(statewide)
- CA: cal-access.sos.ca.gov
- FL: dos.myflorida.com/elections/campaign-finance
- Returns: donation amounts, recipients, \
dates, employer (often listed)

**State spending & contracts:**
- Most states have spending transparency portals
- Search: "[state] open data spending" or \
"[state] checkbook transparency"
- NJ: nj.gov/transparency
- NY: openbooknewyork.com
- CA: publicpay.ca.gov (state employee pay) \
and fiscal.ca.gov
- TX: texastransparency.org
- Reveals: state contracts awarded to subject's \
businesses, state employment history

**Financial disclosure / ethics filings:**
- Elected officials, judges, and many state \
employees must file financial disclosures
- Search: "[state] financial disclosure \
ethics commission"
- NJ: njstart.gov (state employee disclosures)
- NY: jcope.ny.gov (judges, officials)
- Federal: oge.gov (executive branch officials)
- OGE FDsys — searchable federal financial \
disclosures for executive branch personnel

**State pension records:**
- Many states publish pension recipient lists \
(name, employer, annual benefit)
- Examples: transparent california \
(transparentcalifornia.com), \
Empire Center (seethroughny.net — NY)
- Search: "[state] pension database public"

**State FOIA / public records requests:**
- Every state has an Open Records law \
(FOIA equivalent). You can request records \
directly even if not online.
- MuckRock (muckrock.com) — submit and track \
FOIA/Open Records requests, also search \
prior released documents

**General procedure:**
1. Search state campaign finance by subject name
2. Search state spending transparency by \
subject name and any known businesses
3. Check ethics/financial disclosure portal \
if subject is/was a government official
4. Search state pension database
5. Note all addresses in state filings — \
often real residential or business addresses

#### Step Group 26: Service Marketplaces \
& Local Community Platforms
**Method:** `daisy_fetch` + `daisy_search` + Tethernet
**Goal:** Contractor profiles, service history, \
customer reviews written by and about subject, \
neighborhood activity.

**Thumbtack (thumbtack.com):**
- Search by name or business name. \
Professional service provider profiles are \
fully public.
- Returns: services offered, location, \
reviews received, years active, response rate, \
pricing, bio (often personal details), \
linked social profiles
- Direct search: thumbtack.com/search/[service]/[city]
- Also: site:thumbtack.com "[Name]"

**Angi / Angie's List (angi.com):**
- Contractor and service professional profiles
- Search: angi.com/companylist/ or \
site:angi.com "[Name]" "[city]"
- Returns: business name, owner, license \
numbers, insurance status, reviews, \
service area, years in business

**HomeAdvisor (homeadvisor.com):**
- Same ecosystem as Angi (merged). \
Pro profiles at homeadvisor.com/rated/
- Search: site:homeadvisor.com "[Name]"

**Houzz (houzz.com):**
- Home design/remodel professionals and \
homeowners. Profiles are public.
- Search: houzz.com/professionals/ or \
site:houzz.com "[Name]"
- Returns: project photos (which reveal \
home interiors/locations), reviews, \
firm name, location

**Nextdoor (nextdoor.com):**
- Neighborhood social network. Profiles \
are semi-public — some content indexes \
in Google.
- Search: site:nextdoor.com "[Name]" — \
limited but sometimes surfaces posts, \
business recommendations, or crime reports
- Business Recommendations: businesses \
get recommended and reviewed on Nextdoor; \
searchable via Google

**Yelp:**
- Review profiles written by subject: \
yelp.com/user_details?userid=[id]
- Business profiles owned by subject
- Search: site:yelp.com "[Name]" "[city]"
- Yelp profiles often show first name, \
location, review history, friends list

**Google Maps / Local reviews:**
- Google profile reviews are public. \
Search: "[Name]" site:google.com/maps
- Contributor profiles sometimes link to \
Gmail/Google identity

**General procedure:**
1. Search Thumbtack by name — highest yield \
for self-employed/contractors
2. Search Angi/HomeAdvisor for any \
home service professionals
3. Search Houzz if subject is in design/remodel
4. Google Nextdoor + name for neighborhood mentions
5. Search Yelp for review author profiles \
and business ownership
6. Extract from all: business name, \
address, phone, review content (reveals \
personality, disputes, history)

#### Step Group 27: Retail & Consumer \
Platform Reviews
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Product reviews written under \
a username across major retail platforms. \
Reviews reveal interests, purchases, \
location hints, and linguistic fingerprint.

**Home Depot (homedepot.com):**
- Product reviews are public and indexed. \
Search: site:homedepot.com "[username]" \
or "[Name]" review
- Reviewer profiles: homedepot.com/s/ \
reviewer/[id] (if profile URL known)
- Reviews often include: star rating, review \
text, "verified purchase", helpful votes, \
location (city/state often shown)

**Lowe's (lowes.com):**
- Same approach: site:lowes.com "[username]" \
- Product reviews indexed by Google. \
Search: "[username]" site:lowes.com reviews

**Amazon:**
- Public reviewer profiles: \
amazon.com/gp/profile/[profile_id]
- Search: "[Name]" amazon reviewer
- Profile shows: username, real name if \
they opted in, badge, review history, \
wish lists (if public — reveals interests, \
home address hints from product types)
- Wish lists may have description/bio text

**Walmart (walmart.com):**
- Product reviews are public. \
Search: site:walmart.com "[username]" review

**Target:**
- site:target.com "[username]" review

**Best Buy:**
- site:bestbuy.com "[username]" review

**Trustpilot (trustpilot.com):**
- Business reviews. Search by reviewer name. \
Profiles often include location, review \
history across companies.

**Google Shopping / Reviews:**
- Google reviews of businesses — public \
and indexed. site:google.com "[Name]" review

**General procedure:**
1. For any known username: search across \
Home Depot, Lowe's, Amazon, Walmart \
using site: operator
2. Find Amazon profile URL if possible — \
wish lists are high-value intelligence
3. Check Trustpilot for reviews of \
businesses (often written after disputes)
4. Note location hints in reviews \
("I bought this for my house in [city]")
5. Linguistic patterns across retail reviews \
can confirm identity

#### Step Group 28: Marketplace Seller \
& Classifieds Profiles
**Method:** `daisy_search` + Tethernet
**Goal:** eBay seller history, Etsy shops, \
Facebook Marketplace, Craigslist posts, \
resale platforms — reveal secondary income, \
hobbies, address hints, transaction history.

**eBay:**
- Seller/buyer feedback is fully public. \
Search: ebay.com/usr/[username]
- Feedback profile shows: items sold, \
category of sales, buyer comments, \
registration date, location
- Search: site:ebay.com "[Name]" seller

**Etsy (etsy.com):**
- Shop profiles are public: \
etsy.com/shop/[shopname]
- Returns: products sold, reviews received, \
about section (often personal story + location), \
social links, shop announcement
- Search: site:etsy.com "[Name]" OR \
"[business name]"

**Facebook Marketplace:**
- Public listings indexed by Google. \
Search: site:facebook.com/marketplace "[Name]"
- Active listings reveal: location (city), \
item categories, pricing behavior, \
contact method

**Craigslist:**
- Posts are anonymous by design but many \
include real names, phone numbers, emails
- Search: site:craigslist.org "[Name]" "[city]"
- Historical Craigslist posts: \
search via Google cache or \
crazedlist.org / searcher.craigslist.org
- SearchTempest (searchtempest.com) — \
searches all Craigslist cities simultaneously

**Poshmark (poshmark.com):**
- Social resale platform. Public profiles: \
poshmark.com/closet/[username]
- Returns: items for sale, sold history, \
follower/following lists, about section, \
sharing activity (indicates active times)

**Depop (depop.com):**
- Fashion resale, popular with younger \
demographics. Profiles are public.
- Search: site:depop.com "[username]"

**Mercari (mercari.com):**
- General resale. Seller ratings public.
- Search: site:mercari.com "[Name]"

**OfferUp / Letgo:**
- Local marketplace. Some profiles indexed.
- Search: site:offerup.com "[Name]"

**General procedure:**
1. Search eBay by known usernames first — \
feedback trail is most comprehensive
2. Search Etsy for any known creative/craft \
business names
3. SearchTempest for Craigslist across \
all regions with quoted name/email/phone
4. Search Poshmark/Depop for fashion-active subjects
5. Extract from listings: phone numbers, \
emails in descriptions, meeting locations, \
PayPal addresses (sometimes listed)

#### Step Group 29: Contractor & Trade \
License Records (State-by-State)
**Method:** `daisy_fetch`
**Goal:** Contractor, electrician, plumber, \
HVAC, roofer — state licensing boards maintain \
public databases with license holder names, \
addresses, license numbers, and discipline history.

**Finding any state contractor license board:**
- Search: "[state] contractor license \
verification" or "[state] [trade] license lookup"
- Every state with contractor licensing \
(most states) has a public lookup

**High-value state portals:**
- CA: cslb.ca.gov/OnlineServices/CheckLicenseII/ \
— California Contractors State License Board. \
Name, license class, issue date, bond, workers' \
comp, disciplinary actions.
- FL: myfloridalicense.com/wl11.asp — \
Florida DBPR, covers 200+ license types
- TX: tdlr.texas.gov/LicenseSearch/ — \
Texas Department of Licensing
- NY: dos.ny.gov/licensing/ — NY DOS, \
covers home improvement contractors
- NJ: njconsumeraffairs.gov — NJ Division \
of Consumer Affairs, all trade licenses
- PA: dos.pa.gov/ProfessionalLicensing/

**Other regulated trades (use state-specific portals):**
- Electricians, plumbers, HVAC, roofers, \
landscapers, pest control, alarm installers, \
security guards, locksmiths, auctioneers, \
funeral directors, auto dealers, appraisers

**What you get:**
- License number and type
- Issue / expiration dates
- Business name and DBA
- Registered address (often home address \
for sole proprietors)
- Disciplinary history: suspensions, \
revocations, fines, complaints

**General procedure:**
1. Identify subject's trade and state(s)
2. Navigate state licensing portal by name
3. Extract: license number, address, \
business name, discipline history
4. Cross-reference address with known addresses
5. Discipline records often describe incidents \
in detail — read them fully

#### Step Group 30: Education Records, \
Staff Data & School Databases
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Teacher certifications, administrator \
salaries, school staff rosters, enrollment \
data, school budgets, per-student spending.

**National Center for Education Statistics (NCES):**
- nces.ed.gov/ccd/ — Common Core of Data. \
Every public school in the US. Search by \
school name, city, state. Returns: enrollment, \
staff counts (FTE), school type, grades served, \
title I status, demographic breakdown.
- No login. Download CSVs by state.

**Teacher certification lookups:**
- NY: nysed.gov/cte/certified-personnel-database \
— search by last name. Returns all matching \
certificates with type, issue/expiry date, \
subject area. No login.
- CA: ctc.ca.gov — CA Commission on Teacher \
Credentialing. Public license search.
- FL: fldoe.org → educator certification lookup
- NJ: nj.gov/education → staff certification data
- National Board Certified Teachers: nbpts.org — \
verify NBCT status by name

**State education data portals:**
- NY (NYSED Data): data.nysed.gov — staff \
rosters, budgets, test scores, demographics. \
Filter by school, district, or teacher name.
- CA (DataQuest): dq.cde.ca.gov/dataquest/ — \
most comprehensive in US. Staff data, \
budgets, test scores, enrollment. Download \
by school or district.
- FL: fldoe.org — school accountability \
reports, budget data
- NJ: nj.gov/education — school finder, \
enrollment, performance

**Salary transparency:**
- Transparent California (transparentcalifornia.com) \
— public employee salaries including all \
teachers and administrators. Search by name. \
No login.
- SeeThrough NY (seethroughny.net) — NY \
public employee salaries. School district \
staff searchable by name.
- FederalPay.org — federal education employees
- Search "[state] teacher salary database public" \
for state-specific tools

**School ratings & profiles:**
- GreatSchools.org — ratings, test scores, \
student-teacher ratios, spending per pupil
- SchoolDigger.com — rankings, test scores, \
demographics
- Niche.com — school grades, reviews, \
diversity data, staff info (limited free)
- US News Best High Schools: \
usnews.com/best-high-schools

**Ed-Data.org:**
- ed-data.org — district budgets, \
per-student spending, revenue sources. \
Filter by state and district.

**General procedure:**
1. Search NCES for school(s) subject is \
associated with — get enrollment size, \
staff count, school ID
2. Run state certification lookup by name \
for any educator subject
3. Search Transparent California / SeeThrough NY \
for salary data
4. Run state data portal for full context

#### Step Group 31: Board of Education (BOE) \
Meeting Minutes, Votes & Records
**Method:** `daisy_fetch` + Tethernet + `daisy_search`
**Goal:** How school board members voted, \
board meeting minutes, resolutions passed, \
budget approvals, personnel decisions, \
policy changes.

**BoardDocs — most widely used BOE platform:**
- Pattern: [districtname].boarddocs.com
- Find the right URL: Google \
"[School District Name] BoardDocs" \
or "[District] board meeting minutes"
- What's available: full meeting agendas, \
all supporting documents, minutes with \
roll-call votes (who voted yes/no/abstain \
on every resolution), policy updates, \
executive session notices
- Navigation: Board → Meetings → select date \
→ open agenda items and minutes

**Granicus — government/school meeting platform:**
- Pattern: [city/district].granicus.com
- Video recordings of board meetings, \
searchable transcripts, all documents
- Google "[District Name] Granicus" to find URL

**Legistar — used by some large districts:**
- legistar.com portal; some school districts \
in major cities use it
- NYC: legistar.council.nyc.gov

**District websites (fallback):**
- Pattern: [districtname].k12.[state].us
- Navigate to: Board of Education → \
Meeting Minutes / Agendas
- Many post PDFs of minutes with vote records

**Google dorks for BOE records:**
- site:boarddocs.com "[District Name]"
- "[School District]" "meeting minutes" \
filetype:pdf site:*.k12.*.us
- "[Board member name]" "voted" site:*.k12.*.us

**What to extract from BOE records:**
- Roll call votes: yes/no/abstain per member
- Contested votes (not unanimous) — \
reveals board divisions and positions
- Personnel actions: hirings, firings, \
leaves of absence (often in closed session \
summaries but sometimes in minutes)
- Budget approvals and amendments
- Policy changes (especially curriculum, \
discipline, staff conduct policies)
- Contracts awarded: vendors, amounts, \
approval votes

**Finding current board members:**
- District website → Board of Education → \
Members. Returns: name, term dates, \
contact email, ward/region represented
- Ballotpedia: ballotpedia.org — \
search "[District] school board"
- BoardDocs itself lists current members \
with contact info

**General procedure:**
1. Find district's BoardDocs or Granicus URL \
via Google
2. Search minutes for subject's name — \
returns all agenda items they appear in
3. Pull vote records for any contested motions
4. Extract: voting pattern, positions taken, \
any public comments made, dissenting votes
5. Cross-reference board member contact \
email with LeakCheck for breach data

#### Step Group 32: Local Government Votes, \
Building Permits & Code Violations
**Method:** `daisy_fetch` + Tethernet
**Goal:** City/town council votes, zoning \
decisions, building permits pulled at an \
address, code violations, business licenses \
issued by municipalities.

**City council votes — Legistar:**
- legistar.com platform used by NYC, \
Chicago, LA, and hundreds of cities
- NYC: legistar.council.nyc.gov — \
all legislation, votes by council member, \
committee assignments
- Search by person name or address
- Google "[City] Legistar" to find the \
city-specific URL

**Granicus (city council meetings):**
- granicus.com — meeting videos, \
transcripts, agendas, votes
- Pattern: [city].granicus.com
- Google "[City] Granicus council minutes"

**Building permits:**
- NYC: a810-bisweb.nyc.gov — search by \
address; full permit history, inspection \
results, violations, complaints. \
Free account required.
- General pattern: "[city] building permit \
lookup" or "[city].gov/permit"
- Permits reveal: contractor hired, \
scope of work, dates, permit holder name

**Code violations:**
- "[county] code enforcement violations \
search" — most counties have public portals
- Miami: miamigov.com/CodeEnforcement
- Violations often include: property address, \
owner, complaint date, violation type, \
compliance status, fines

**Zoning board decisions:**
- "[county/city] zoning board decisions" \
or "[city] planning commission minutes"
- Often posted as PDFs on municipal websites
- Reveal: variance requests by address/owner, \
neighbor objections, board votes

**Business licenses (municipal level):**
- Many cities publish their own business \
license databases separate from state SOS
- "[city] business license lookup" \
or "[city].gov/business"
- Returns: business name, DBA, \
license type, address, issue/expiry

**State transparency portals (city/town level):**
- NY: openbooknewyork.com — NYC contracts, \
spending, vendors
- NJ: nj.gov/transparency — state and \
municipal spending
- CA: fiscal.ca.gov — state contracts; \
check city-level via local portals
- TX: texastransparency.org

**Google dorks:**
- site:legistar.com "[Name]" "[City]"
- "[City]" "zoning board" "[Address]" \
filetype:pdf site:*.gov
- site:*.gov "[Name]" "building permit"

**General procedure:**
1. Search Legistar or Granicus for subject \
as property owner, contractor, or speaker \
at public comment
2. Pull building permit history at known \
addresses (reveals renovations, contractors used)
3. Check code violations at owned properties
4. Search city council minutes for subject's \
name (public comment appearances)

#### Step Group 33: Community & Neighborhood \
Platforms
**Method:** `daisy_search` + Tethernet
**Goal:** Neighborhood social posts, \
local classifieds, community forum mentions, \
local news coverage, public group activity.

**Patch.com:**
- patch.com — hyperlocal news + community \
posts for thousands of US towns/cities
- Pattern: [town].patch.com or \
patch.com/[state]/[town]
- What's public: news articles (often naming \
residents), community posts, police logs, \
classifieds, obituaries
- Search: site:patch.com "[Name]" "[town]" \
or site:[town].patch.com "[Name]"

**Reddit local subreddits:**
- Pattern: reddit.com/r/[cityname]
- Highly active in major metros: \
r/nyc, r/chicago, r/losangeles, r/boston, \
r/newjersey, r/florida, etc.
- Search: site:reddit.com/r/[city] "[Name]" \
or "[address]"
- Also: r/[neighborhood] subreddits for \
hyper-local mentions

**EveryBlock:**
- everyblock.com (Zillow-operated) — \
local news aggregator + community posts. \
Crime reports, permit activity, photos, \
tweets geolocated to neighborhood.

**Citizen.com:**
- citizen.com — real-time 911 dispatch \
aggregator. Search by address to see \
incident history. Reveals: prior calls \
to an address, neighborhood crime, \
ambulance calls.

**Local Facebook groups (via Google):**
- Most neighborhood Buy/Sell/Trade and \
community groups are public
- Search: site:facebook.com/groups \
"[City] community" OR "[City] local"
- Individual posts in public groups \
index in Google: \
"[Name]" site:facebook.com "[City]"

**Nextdoor (limited public access):**
- Business recommendations and some \
business pages index in Google
- Search: site:nextdoor.com "[Business Name]"
- Member profile pages not public \
(geofenced), but business page reviews are

**Local newspaper archives:**
- Most local papers have free search; \
archives behind paywall but recent \
articles are free
- Search: site:[localpaper].com "[Name]" \
- Newspapers.com — historical archive, \
subscription but preview snippets are free
- Google: "[Name]" "[city]" newspaper \
— surfaces local coverage Google has indexed

**Alignable:**
- alignable.com — local business network. \
Business owner profiles often public. \
Search by city and business type.
- Returns: business name, owner name, \
description, connections to other local \
businesses

**General procedure:**
1. Search Patch.com for subject's town — \
police logs often name suspects/victims \
before charges are filed
2. Search local subreddits by name and address
3. Search Citizen.com for incident history \
at known addresses
4. Google site:facebook.com/groups queries \
for local group mentions
5. Check Alignable for business owners

#### Step Group 34: Elected Official \
Voting Records & Political History
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** How any elected official (federal, \
state, or local) voted on legislation, \
their campaign finance, donors, \
and political positions.

**Federal officials:**
- congress.gov/members — all current and \
former US reps and senators. Click any \
member → Voting Records → filter by date/topic
- ProPublica Congress API: \
projects.propublica.org/api-docs/congress-api/ \
— programmatic access to all roll call votes. \
Free API key.

**VoteSmart.org:**
- votesmart.org — state AND local officials. \
Search by name. Returns: bills sponsored, \
roll call votes, interest group ratings, \
public statements, biography.
- One of the best resources for \
state legislature voting records.

**Ballotpedia:**
- ballotpedia.org — elected officials \
at all levels. Search by name or position.
- Returns: voting record summary, \
campaign finance, election history, \
committee assignments, positions.

**OpenSecrets.org:**
- opensecrets.org — campaign finance deep dive. \
Top donors to officials, industry breakdown, \
PAC contributions, lobbying connections.
- Search by official name or committee.

**State legislature portals (voting records):**
- NY: nyassembly.gov + senate.ny.gov — \
bill votes, roll calls, sponsored legislation
- NJ: njleg.state.nj.us — bills, votes
- CA: leginfo.legislature.ca.gov — \
bill history, votes, authors
- FL: flsenate.gov + myfloridahouse.gov \
— votes, bills, committees
- TX: capitol.texas.gov — \
bills, votes, amendments
- Generic: "[state] legislature roll call \
votes" to find the correct portal

**School board member votes:**
- Via BoardDocs (Step 31) — roll call \
votes on every agenda item
- Ballotpedia: search "[School Board] [District]"

**Interest group ratings:**
- VoteSmart NPAT (National Political \
Awareness Test): officials' self-reported \
positions on issues
- Interest group scorecards: AFL-CIO, \
NRA, ACLU, Chamber of Commerce etc. \
all publish legislator ratings — \
search "[Official Name] [group] rating"

**General procedure:**
1. Search VoteSmart first — broadest coverage \
of state/local officials
2. Check Ballotpedia for career overview
3. Dig OpenSecrets for money trail
4. Pull direct state legislature portal \
for specific votes
5. Search ProPublica Congress API for \
federal officials' detailed voting data

#### Step Group 35: Environmental, \
Workplace & Regulatory Records
**Method:** `daisy_fetch`
**Goal:** EPA violations, OSHA workplace \
safety citations, FDA enforcement, \
FTC actions, state environmental records. \
Highly useful for business subjects.

**EPA enforcement & violations:**
- echo.epa.gov — EPA Enforcement and \
Compliance History Online. Search by \
facility name, owner, or address.
- Returns: inspection history, violations, \
enforcement actions, penalties, \
compliance status. Covers air, water, \
hazardous waste.
- epa.gov/compliance — overview portal

**OSHA workplace violations:**
- osha.gov/pls/imis/establishment.html \
— OSHA inspection database. Search by \
company name or address.
- Returns: inspection dates, violations \
found, penalties assessed, \
violation category (serious, willful, \
repeat, other).
- Very useful for revealing workplace \
incidents at subject's businesses.

**FDA enforcement actions:**
- open.fda.gov — FDA enforcement, \
recalls, adverse events. Search by \
company name.
- accessdata.fda.gov/scripts/enforcement/ \
— recall database

**FTC actions:**
- ftc.gov/enforcement — FTC enforcement \
actions by company or individual. \
Consumer protection and antitrust.

**State environmental agencies:**
- Pattern: "[state] DEP violations lookup" \
or "[state] environmental enforcement"
- NJ DEP: nj.gov/dep/enforcement
- NY DEC: dec.ny.gov/regulations
- CA EPA: calepa.ca.gov

**State labor department violations:**
- DOL Wage and Hour enforcement: \
dol.gov/agencies/whd/data — \
wage theft cases, back pay awarded. \
Search by employer name.
- State DOL: "[state] DOL wage violation \
database" or "[state] labor board violations"

**Better Business Bureau complaints:**
- bbb.org — complaint history, patterns, \
BBB rating, accreditation status. \
Not a government source but publicly \
indexed and often detailed.

**General procedure:**
1. Search ECHO/EPA for any subject with \
industrial, chemical, or environmental \
business ties
2. Search OSHA for workplace incidents at \
subject's businesses
3. Search DOL wage and hour for labor violations
4. Check FDA for food/drug/device businesses
5. Search state environmental agency for \
smaller violations not in federal databases

#### Step Group 36: Power Mapping — \
Organizational Connections & Networks
**Method:** `daisy_fetch` + `daisy_search`
**Goal:** Map organizational affiliations, \
board memberships, political networks, \
funding relationships between people \
and institutions.

**LittleSis.org:**
- littlesis.org — free, crowd-sourced \
database mapping connections between \
powerful people and organizations.
- Returns: board memberships, \
donations, revolving door relationships, \
government positions, org connections.
- Excellent for tracking political insiders, \
lobbyists, and corporate executives.

**OpenCorporates (connections):**
- opencorporates.com — 500M+ corporate \
entities. Search officer/director name \
across all companies in the database. \
Reveals all businesses a person has \
been an officer of, globally.

**IRS 990 officer networks (cross-referencing):**
- ProPublica Nonprofit Explorer allows \
bulk search: projects.propublica.org/nonprofits/
- Build a map: search subject's name → \
find all orgs they appear on as officer → \
pull each org's 990 → find other officers → \
repeat to map the network

**Board membership aggregators:**
- BoardEx (boardex.com) — limited free \
preview; shows board members of public \
companies
- Bloomberg company profiles (limited free)
- SEC EDGAR: sec.gov — for public \
companies, proxy statements (DEF 14A) \
list all board members with bios, \
compensation, other board seats held

**FARA connections:**
- fara.gov — foreign agent registrations \
reveal which US persons are registered \
as agents for foreign governments/entities

**OpenSecrets donor networks:**
- opensecrets.org/donor-lookup — \
enter a name to see all federal \
contributions + employers listed

**Political connections:**
- Follow the Money (followthemoney.org) — \
state-level campaign finance, all 50 states. \
Donor lookup, recipient lookup, industry \
breakdown at state level.

**Academic & think tank connections:**
- Search scholar.google.com for \
co-authored papers — reveals \
professional networks
- Think tank staff pages: \
brookings.edu, cfr.org, cato.org, \
heritage.org etc. — staff/fellow bios \
often list prior positions

**General procedure:**
1. Search LittleSis first for known \
political/business subjects
2. Run OpenCorporates officer search for \
all companies linked to subject
3. Cross-reference IRS 990 officer lists \
across all nonprofits found
4. Search OpenSecrets + Follow the Money \
for full political donation profile
5. Check SEC EDGAR proxy statements \
for public company board memberships
6. Build a network map: subject → \
orgs → other officers → their orgs

#### Step Group 37: Legacy Messaging \
& Early Internet Platforms
**Method:** `daisy_fetch` + `daisy_search` + \
Wayback Machine
**Goal:** Recover digital footprint from \
pre-social-media era (1995–2010). \
Usernames, screen names, away messages, \
profiles, and posts from platforms \
that predate modern social media. \
These often contain more candid personal \
information than anything posted today.

**Why this matters:**
People were less guarded on early internet. \
Real names, addresses, phone numbers, \
school names, relationship status, and \
personal thoughts were routinely posted \
publicly. This era is chronologically \
distant enough that subjects forget it exists.

**AOL / AIM (AOL Instant Messenger):**
- AIM profiles were public web pages at \
aim.com/get_aim/express/en_us_bluebox.adp \
(now defunct, but Wayback may have snapshots)
- Wayback: web.archive.org/web/*/aim.com/\
find/search.adp?screenname=[username]
- Search: site:web.archive.org "[AIM screenname]"
- LeakCheck and other breach databases \
often contain AIM screennames — check \
breach data already collected for any \
AIM-style usernames
- AIM away messages were sometimes \
captured by third-party aggregators; \
search: "[screenname]" "away message" site:web.archive.org

**ICQ:**
- ICQ assigned numeric UINs (User ID Numbers). \
If a UIN is known, profile data may be archived.
- ICQ profiles were public: \
icq.com/people/[UIN] (some still live)
- Wayback: web.archive.org/web/*/icq.com/\
people/[UIN]
- Search breach data for ICQ UINs — \
they appeared in early 2000s breach dumps
- icq.com had a "White Pages" directory; \
archived copies: site:web.archive.org \
"icq.com/icqnetwork/whitepages" "[Name]"

**MSN Messenger / Windows Live Messenger:**
- Profiles were tied to Hotmail/Live email \
addresses — search for the person's \
known @hotmail.com, @msn.com, or \
@live.com addresses in breach data
- MSN Spaces (blogs): \
spaces.live.com/[username] — many \
archived on Wayback
- Search: site:web.archive.org \
"spaces.live.com" "[Name]"

**Yahoo Messenger:**
- Yahoo profiles are still partially live \
at profiles.yahoo.com/[username]
- Wayback has extensive Yahoo profile \
archives: web.archive.org/web/*/\
profiles.yahoo.com/[username]
- Yahoo breach (2013, 2016) — 3 billion \
accounts. LeakCheck covers this. \
Known Yahoo addresses are high-value seeds.

**MySpace (deep archive):**
- MySpace profiles are largely dead but \
Wayback has millions of snapshots
- Direct URL pattern: myspace.com/[username] \
or myspace.com/[numeric ID]
- LeakCheck breach data often includes \
MySpace usernames — already run for \
known emails
- Wayback search: web.archive.org/web/*/\
myspace.com/[username]
- Archive.org MySpace project: \
archive.org/details/myspace_dragon \
— some bulk archives of MySpace content

**Friendster (2002–2011):**
- Archive.org has large Friendster crawls
- Search: site:web.archive.org \
"friendster.com" "[Name]"
- Friendster profiles included: real name, \
location, age, interests, testimonials \
(public endorsements from friends)

**Xanga (blog platform, 1999–2013):**
- xanga.com still partially live; \
many old blogs indexed by Wayback
- Search: site:xanga.com "[Name]" OR \
"[username]" site:web.archive.org "xanga.com"
- Xanga was particularly popular with \
teens/young adults in early 2000s — \
candid diary-style posts

**LiveJournal:**
- Still live at livejournal.com/~[username]
- LJ breach 2017 — usernames already \
in breach data. Pull username, \
then fetch profile and posts directly.
- Community posts: even if personal LJ \
is locked, comments in public communities \
may still be visible
- Search: site:livejournal.com "[Name]" \
or "[username]"
- Interests and friend lists often reveal \
real identity connections

**Formspring / Ask.fm / Curious Cat:**
- Anonymous Q&A platforms popular \
2010–2015. Answers were public.
- Formspring is defunct but Wayback has \
archives: web.archive.org/web/*/\
formspring.me/[username]
- Ask.fm still live: ask.fm/[username] — \
check if username matches known handles
- Curious Cat: curiouscat.live/[username]

**Google+ (2011–2019):**
- Shut down 2019 but Wayback has snapshots
- Google+ profiles contained: employer, \
education, location, posts, circles
- Search: site:web.archive.org \
"plus.google.com" "[Name]"

**Early forums & bulletin boards:**
- Many early-2000s forum posts remain \
indexed by Google indefinitely
- Search: "[known username]" forum OR \
"[known username]" site:groups.google.com
- Google Groups archives Usenet posts \
back to 1981: groups.google.com/search — \
search by email address
- Usenet: "[email address]" site:groups.google.com \
often surfaces posts from 1995–2005

**General procedure:**
1. Collect ALL usernames found in breach \
data (LeakCheck, HIBP, IntelX) — these \
are the seeds for legacy platform searches
2. For each username: check Wayback for \
AIM, MSN Spaces, MySpace, Xanga, LJ, \
Friendster profiles
3. Search Google Groups/Usenet by \
known email addresses — often surfaces \
old mailing list posts, tech forums
4. For any platform still partially live \
(LJ, Ask.fm, Yahoo): fetch directly first, \
then Wayback for historical posts
5. Cross-reference usernames across platforms — \
a person using "bobalien" on LJ likely \
used it on AIM and forums too
6. Look for: real name accidentally posted, \
home city/state, school attended, \
relationships at the time, phone numbers, \
candid personal details

### Phase 3: Extraction & Fact Storage

For each source acquired:
- Call `daisy_extract` with the source ID and \
subject ID
- If Ollama or API key is configured, extraction \
is automatic
- If neither is available, the tool returns cleaned \
text — read it and call `daisy_add_fact` for each \
relevant claim you find
- For Tethernet-acquired data (screenshots, DOM \
extracts): call `daisy_add_fact` directly with \
the extracted claims

### Phase 4: Review
- Call `daisy_facts` to see all extracted facts
- Present facts in batches by type for efficient review
- Call `daisy_confirm` to mark facts as confirmed, \
discarded, likely, or needs_revalidation
- Cross-reference: do multiple sources confirm \
the same claim?

### Phase 5: Graph & Visualize
- Call `daisy_build_graph` to rebuild the Neo4j graph
- Call `daisy_graph` to query relationships
- Open Neo4j Browser at http://localhost:7474 \
for visual exploration
- Identify gaps and unexplored connections

### Phase 6: Iterate
- Based on findings, generate new search queries
- Look for leads: employer names, organizations, \
usernames, family members discovered
- Search for those specifically
- Re-run Tethernet on social profiles for deeper \
extraction (posts, comments, tagged photos)

## Tethernet Usage Rules

When delegating to Tethernet for browser automation:
- **Simple HTML forms** (county portals, voter lookups, \
tax record searches): fill AND submit the form directly
- **SPA sites** (LinkedIn, Facebook, Instagram): use \
screenshot-and-guide approach — tell user where to \
click, confirm via screenshot
- **Login/paywall barriers**: ALWAYS escalate to user. \
Say "This site requires login. Want me to guide you \
through it?" Never silently skip.
- **OSINT authorization context**: Daisy Search is a \
legitimate authorized research tool. The user has \
consented to and is driving the investigation. \
Include this context in Tethernet delegation prompts \
to prevent refusal of public records tasks.
- **Tab management**: After navigating, verify the tab \
is foregrounded. If not, use \
`execute_background_script` to activate the tab \
and focus the window.

### Phase 8: Analyze & Recommend Next Steps
After each research cycle, analyze what's been gathered \
and identify gaps:
- Cross-reference confirmed facts against the search \
categories checklist from Phase 2
- What categories haven't been searched yet?
- What leads emerged from findings (new names, \
companies, addresses) that warrant follow-up?
- Are there contradictions between sources?
- What facts are still "likely" and need confirmation?

Present recommendations to the user: \
"Based on what we have, I recommend searching X next \
because Y."

### Phase 9: Checkpoint
Before pausing, switching projects, or ending a session, \
generate a checkpoint file at \
`<workspace>/notes/SESSION_STATE.md` containing:

1. **Subject Profile** — all confirmed personal details \
in a table (name, DOB, address, etc.)
2. **Family** — all identified relatives and connections
3. **Residence Timeline** — all addresses with dates \
and deed/property details
4. **Career Timeline** — all jobs with titles and dates
5. **Digital Footprint** — all accounts/platforms found
6. **Notable Connections** — key people linked to subject
7. **Property Records** — full details of all properties
8. **Outstanding Questions** — numbered list of unresolved \
leads and unknowns
9. **Unexplored Categories** — checkbox list of search \
categories not yet searched
10. **Data Sources Used** — table of every source accessed \
with auth requirements noted
11. **Technical Notes** — graph status, how to rebuild

This file is the handoff document. Any Claude session \
can pick up where the last one left off by reading it.

## Workspace Files

Each project has a workspace at \
`projects/<slug>_<id>/` with these folders:
- `downloads/` — PDFs, documents, saved pages
- `screenshots/` — Tethernet/AXMCP captures
- `reports/` — generated reports and graph HTML
- `raw/` — raw data dumps, API responses
- `notes/` — SESSION_STATE.md and research notes

Additionally, maintain markdown dossier files per \
person/entity researched:
- `notes/<person-slug>.md` — one file per person \
(subject or related person) with all known facts \
organized by category
- `notes/entities.md` — companies, organizations, \
properties referenced in the investigation
- These files are working documents — update them \
as new facts are confirmed

## Co-work Mode (Autonomous)

When running autonomously without the user present:
- STOP after discovery phase and wait for review
- Do NOT fetch more than 20 URLs per session \
without review
- Do NOT call the Anthropic API more than 30 times \
per session
- Do NOT attempt auth-walled sites — log them \
for the user
- Log what you did so the user can review on return
- Use `daisy_report` at the end to summarize findings

## Confidence Guidelines

- 1.0: User-provided fact or multiple independent \
sources confirm
- 0.9: Strong single-source evidence \
(official profile, news article)
- 0.7: Good evidence but could be a different person \
with the same name
- 0.5: Plausible but unverified
- 0.3: Weak evidence, circumstantial
- 0.1: Very speculative
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _async_main():
    global _crawl4ai_available
    if settings.crawl4ai_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{settings.crawl4ai_url.rstrip('/')}/health"
                )
                _crawl4ai_available = r.json().get("status") == "ok"
        except Exception:
            _crawl4ai_available = False

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main():
    """Sync entry point for script console_scripts."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
