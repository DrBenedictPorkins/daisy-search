"""Read-only web dashboard routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.responses import Response

from daisy.api.deps import get_db
from daisy.constants import ClaimType, LifecycleState
from daisy.models.fact import Fact
from daisy.models.project import Project
from daisy.models.source import Source
from daisy.models.subject import Subject

router = APIRouter()

Db = Annotated[AsyncSession, Depends(get_db)]


async def _projects_nav(db: AsyncSession) -> list[Project]:
    """Fetch all projects for the navigation dropdown."""
    result = await db.execute(
        select(Project).order_by(Project.name)
    )
    return list(result.scalars().all())


@router.get("/")
async def index() -> RedirectResponse:
    """Redirect root to projects list."""
    return RedirectResponse(url="/projects", status_code=302)


@router.get("/projects")
async def projects_list(request: Request, db: Db) -> Response:
    """List all projects with subject and fact counts."""
    from daisy.api.app import templates

    # Subquery for subject counts
    subject_count_sq = (
        select(
            Subject.project_id,
            func.count(Subject.id).label("subject_count"),
        )
        .group_by(Subject.project_id)
        .subquery()
    )

    # Subquery for fact counts
    fact_count_sq = (
        select(
            Fact.project_id,
            func.count(Fact.id).label("fact_count"),
        )
        .group_by(Fact.project_id)
        .subquery()
    )

    stmt = (
        select(
            Project,
            func.coalesce(subject_count_sq.c.subject_count, 0).label(
                "subject_count"
            ),
            func.coalesce(fact_count_sq.c.fact_count, 0).label(
                "fact_count"
            ),
        )
        .outerjoin(
            subject_count_sq,
            Project.id == subject_count_sq.c.project_id,
        )
        .outerjoin(
            fact_count_sq,
            Project.id == fact_count_sq.c.project_id,
        )
        .order_by(Project.created_at.desc())
    )

    result = await db.execute(stmt)
    rows = result.all()

    projects = []
    for row in rows:
        project = row[0]
        project.subject_count = row[1]
        project.fact_count = row[2]
        projects.append(project)

    projects_nav = await _projects_nav(db)

    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "projects": projects,
            "projects_nav": projects_nav,
        },
    )


@router.get("/projects/{project_id}")
async def project_detail(
    request: Request, project_id: uuid.UUID, db: Db
) -> Response:
    """Show a single project with summary stats."""
    from daisy.api.app import templates

    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one()

    # Fact counts by lifecycle state
    state_result = await db.execute(
        select(
            Fact.lifecycle_state,
            func.count(Fact.id),
        )
        .where(Fact.project_id == project_id)
        .group_by(Fact.lifecycle_state)
    )
    state_counts: dict[str, int] = {}
    total_facts = 0
    for state_val, count in state_result.all():
        state_counts[state_val] = count
        total_facts += count

    # Ensure all states are present (even if zero)
    for state in LifecycleState:
        if state.value not in state_counts:
            state_counts[state.value] = 0

    projects_nav = await _projects_nav(db)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "state_counts": state_counts,
            "total_facts": total_facts,
            "projects_nav": projects_nav,
            "current_project_id": project_id,
        },
    )


_CLAIM_TYPE_ORDER = [
    "location", "employment", "education",
    "account", "legal", "publication", "other",
]


def _group_by_type(facts: list) -> list[tuple[str, list]]:
    """Return (claim_type, facts) pairs in display order."""
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for f in facts:
        buckets[f.claim_type].append(f)
    ordered = [(ct, buckets[ct]) for ct in _CLAIM_TYPE_ORDER if ct in buckets]
    for ct, fl in buckets.items():
        if ct not in _CLAIM_TYPE_ORDER:
            ordered.append((ct, fl))
    return ordered


@router.get("/projects/{project_id}/facts")
async def project_facts(
    request: Request,
    project_id: uuid.UUID,
    db: Db,
    claim_type: str | None = Query(default=None),
    lifecycle_state: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None),
    subject_id: uuid.UUID | None = Query(default=None),
) -> Response:
    """HTMX partial: filterable fact list for a project."""
    from daisy.api.app import templates

    stmt = (
        select(Fact)
        .where(Fact.project_id == project_id)
        .order_by(Fact.local_confidence.desc())
    )

    if claim_type:
        stmt = stmt.where(Fact.claim_type == claim_type)
    if lifecycle_state:
        stmt = stmt.where(Fact.lifecycle_state == lifecycle_state)
    if min_confidence is not None:
        stmt = stmt.where(Fact.local_confidence >= min_confidence / 100.0)
    if subject_id is not None:
        stmt = stmt.where(Fact.subject_id == subject_id)

    result = await db.execute(stmt)
    facts = list(result.scalars().all())

    is_subject = lambda f: f.claim_type != "relationship"  # noqa: E731
    is_related = lambda f: f.claim_type == "relationship"  # noqa: E731

    confirmed = [f for f in facts if f.lifecycle_state == "confirmed" and is_subject(f)]
    likely = [f for f in facts if f.lifecycle_state == "likely" and is_subject(f)]
    other = [f for f in facts if f.lifecycle_state not in ("confirmed", "likely") and is_subject(f)]
    possibly_related = [f for f in facts if is_related(f)]

    return templates.TemplateResponse(
        request,
        "facts_partial.html",
        {
            "facts": facts,
            "confirmed_groups": _group_by_type(confirmed),
            "likely_groups": _group_by_type(likely),
            "other_facts": other,
            "possibly_related": possibly_related,
            "project_id": project_id,
            "claim_types": [ct.value for ct in ClaimType],
            "lifecycle_states": [ls.value for ls in LifecycleState],
            "filters": {
                "claim_type": claim_type,
                "lifecycle_state": lifecycle_state,
                "min_confidence": min_confidence,
                "subject_id": subject_id,
            },
        },
    )


@router.get("/projects/{project_id}/sources")
async def project_sources(
    request: Request,
    project_id: uuid.UUID,
    db: Db,
) -> Response:
    """HTMX partial: sources referenced by facts in this project."""
    from daisy.api.app import templates

    # Get unique source URLs from facts in this project
    url_stmt = (
        select(Fact.source_url)
        .where(Fact.project_id == project_id)
        .distinct()
    )
    url_result = await db.execute(url_stmt)
    source_urls = [row[0] for row in url_result.all()]

    sources_with_size = []
    if source_urls:
        source_stmt = (
            select(Source)
            .where(Source.url.in_(source_urls))
            .order_by(Source.fetch_date.desc())
        )
        result = await db.execute(source_stmt)
        for s in result.scalars().all():
            size = len(s.raw_content) if s.raw_content else 0
            sources_with_size.append({"source": s, "size": size})

    return templates.TemplateResponse(
        request,
        "sources_partial.html",
        {
            "sources": sources_with_size,
            "project_id": project_id,
        },
    )


@router.get("/projects/{project_id}/subjects")
async def project_subjects(
    request: Request,
    project_id: uuid.UUID,
    db: Db,
) -> Response:
    """HTMX partial: subject list for a project."""
    from daisy.api.app import templates

    result = await db.execute(
        select(Subject)
        .where(Subject.project_id == project_id)
        .options(selectinload(Subject.aliases))
        .order_by(Subject.name)
    )
    subjects = list(result.scalars().all())

    # Build subject dicts with counts
    subject_cards = []
    if subjects:
        subject_ids = [s.id for s in subjects]
        fact_count_result = await db.execute(
            select(
                Fact.subject_id,
                func.count(Fact.id).label("fact_count"),
            )
            .where(Fact.subject_id.in_(subject_ids))
            .group_by(Fact.subject_id)
        )
        fact_counts = {
            row[0]: row[1] for row in fact_count_result.all()
        }
        for s in subjects:
            subject_cards.append({
                "subject": s,
                "fact_count": fact_counts.get(s.id, 0),
                "alias_count": len(s.aliases),
            })

    return templates.TemplateResponse(
        request,
        "subjects_partial.html",
        {
            "subjects": subject_cards,
            "project_id": project_id,
        },
    )
