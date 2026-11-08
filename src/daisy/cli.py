"""CLI entry point for Daisy Search."""

import argparse
import asyncio
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="daisy",
        description="Daisy Search - Personal identity exposure mapping",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- intake command ---
    intake_parser = subparsers.add_parser(
        "intake", help="Start research on a person"
    )
    intake_parser.add_argument(
        "name", help="Full name of the person to research"
    )
    intake_parser.add_argument(
        "--location",
        action="append",
        dest="locations",
        default=[],
        help="Known location (can be repeated)",
    )
    intake_parser.add_argument(
        "--occupation",
        action="append",
        dest="occupations",
        default=[],
        help="Known occupation or title (can be repeated)",
    )
    intake_parser.add_argument(
        "--age-range",
        help="Approximate age range (e.g. 30-40)",
    )
    intake_parser.add_argument(
        "--username",
        action="append",
        dest="usernames",
        default=[],
        help="Known username (can be repeated)",
    )
    intake_parser.add_argument(
        "--email",
        action="append",
        dest="emails",
        default=[],
        help="Known email (can be repeated)",
    )
    intake_parser.add_argument(
        "--fact",
        action="append",
        dest="known_facts",
        default=[],
        help="Known fact, e.g. 'works at Paramount' (can be repeated)",
    )
    intake_parser.add_argument("--notes", help="Additional notes")
    intake_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without creating jobs",
    )

    # --- status command ---
    status_parser = subparsers.add_parser(
        "status", help="Check job status for a subject"
    )
    status_parser.add_argument(
        "subject_id", help="Subject UUID to check"
    )

    # --- report command ---
    report_parser = subparsers.add_parser(
        "report",
        help="Show full report of findings for a subject",
    )
    report_parser.add_argument(
        "subject_id", help="Subject UUID to report on"
    )
    report_parser.add_argument(
        "--show-snippets",
        action="store_true",
        help="Include raw text snippets in output",
    )

    # --- subjects command ---
    subparsers.add_parser(
        "subjects", help="List all research subjects"
    )

    # --- new-search command ---
    new_search_parser = subparsers.add_parser(
        "new-search",
        help="Create a project and scaffold its case workspace",
    )
    new_search_parser.add_argument("name", help="Subject or case name")
    new_search_parser.add_argument(
        "description",
        nargs="?",
        default="investigation",
        help="Short description of the case",
    )

    # --- worker command ---
    worker_parser = subparsers.add_parser(
        "worker", help="Start an RQ worker process"
    )
    worker_parser.add_argument(
        "--queues",
        nargs="+",
        help="Queue names to listen on (default: all)",
    )

    args = parser.parse_args()

    if args.command == "intake":
        asyncio.run(_cmd_intake(args))
    elif args.command == "status":
        asyncio.run(_cmd_status(args))
    elif args.command == "report":
        asyncio.run(_cmd_report(args))
    elif args.command == "subjects":
        asyncio.run(_cmd_subjects())
    elif args.command == "new-search":
        asyncio.run(_cmd_new_search(args))
    elif args.command == "worker":
        _cmd_worker(args)
    else:
        parser.print_help()
        sys.exit(1)


async def _cmd_new_search(args: argparse.Namespace) -> None:
    """Create the project row and scaffold its workspace in one step."""
    from pathlib import Path

    from sqlalchemy import select

    from daisy.config import settings
    from daisy.db import async_session_factory
    from daisy.models.project import Project
    from daisy.workspace import (
        resolve_workspace_dir,
        scaffold_workspace,
        slugify,
    )

    async with async_session_factory() as session:
        async with session.begin():
            project = Project(name=args.name, description=args.description)
            session.add(project)
            await session.flush()
            project_id = str(project.id)

        result = await session.execute(
            select(Project.workspace_path).where(
                Project.workspace_path.is_not(None),
                Project.id != project.id,
            )
        )
        claimed = [row[0] for row in result.all()]

    projects_root = Path(settings.projects_dir).resolve()
    project_dir = resolve_workspace_dir(
        projects_root, slugify(args.name), claimed
    )
    scaffold_workspace(
        project_dir,
        subject_name=args.name,
        project_id=project_id,
        description=args.description,
    )

    async with async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(Project).where(Project.id == project.id)
            )
            result.scalar_one().workspace_path = str(project_dir)

    print(f"Project:   {args.name}")
    print(f"ID:        {project_id}")
    print(f"Workspace: {project_dir}")
    print()
    print("To start researching:")
    print(f"  cd {project_dir} && claude")


async def _cmd_intake(args: argparse.Namespace) -> None:
    """Run the intake command."""
    from daisy.workers.intake import (
        _build_search_queries,
        _generate_name_variants,
        _generate_username_guesses,
    )

    name = args.name
    name_variants = _generate_name_variants(name)
    username_guesses = _generate_username_guesses(
        name, args.usernames
    )
    search_queries = _build_search_queries(
        name, name_variants, args.locations, args.occupations,
        args.known_facts,
    )

    print(f"Subject: {name}")
    print(f"Name variants ({len(name_variants)}):")
    for v in name_variants:
        print(f"  - {v}")
    print(f"Username guesses ({len(username_guesses)}):")
    for u in username_guesses:
        print(f"  - {u}")
    if args.known_facts:
        print(f"Known facts ({len(args.known_facts)}):")
        for f in args.known_facts:
            print(f"  - {f}")
    print(f"Search queries ({len(search_queries)}):")
    for q in search_queries:
        print(f"  - {q}")

    if args.dry_run:
        print("\n[dry run] No jobs created.")
        return

    from daisy.constants import WorkerType
    from daisy.queue.dispatcher import enqueue_job

    input_data = {
        "name": name,
        "locations": args.locations,
        "occupations": args.occupations,
        "known_usernames": args.usernames,
        "known_emails": args.emails,
        "known_facts": args.known_facts,
        "notes": args.notes,
    }

    job_id = await enqueue_job(
        worker_type=WorkerType.INTAKE,
        input_data=input_data,
    )
    print(f"\nIntake job created: {job_id}")
    print(
        "Run `uv run python -m daisy.cli worker`"
        " to start processing."
    )


async def _cmd_subjects() -> None:
    """List all research subjects."""
    from sqlalchemy import func, select

    from daisy.db import async_session_factory
    from daisy.models.fact import Fact
    from daisy.models.subject import Subject, SubjectAlias

    async with async_session_factory() as session:
        result = await session.execute(
            select(Subject).order_by(Subject.created_at.desc())
        )
        subjects = result.scalars().all()

        if not subjects:
            print("No subjects found.")
            return

        print(f"{'ID':<38} {'Name':<30} {'Created'}")
        print("-" * 85)
        for s in subjects:
            # Count aliases
            alias_result = await session.execute(
                select(func.count()).where(
                    SubjectAlias.subject_id == s.id
                )
            )
            alias_count = alias_result.scalar()

            # Count facts
            fact_result = await session.execute(
                select(func.count()).where(
                    Fact.subject_id == s.id
                )
            )
            fact_count = fact_result.scalar()

            created = s.created_at.strftime("%Y-%m-%d %H:%M")
            print(
                f"{s.id}  {s.name:<30}"
                f" {created}"
                f"  ({alias_count} aliases,"
                f" {fact_count} facts)"
            )


async def _cmd_status(args: argparse.Namespace) -> None:
    """Show job status for a subject."""
    import uuid as uuid_mod

    from sqlalchemy import func, select

    from daisy.db import async_session_factory
    from daisy.models.fact import Fact
    from daisy.models.job import Job

    subject_id = uuid_mod.UUID(args.subject_id)

    async with async_session_factory() as session:
        # Job counts by status
        result = await session.execute(
            select(Job.status, func.count())
            .where(Job.subject_id == subject_id)
            .group_by(Job.status)
        )
        job_counts = dict(result.all())

        result = await session.execute(
            select(func.count()).where(
                Job.subject_id == subject_id,
            )
        )
        total_jobs = result.scalar()

        # Job counts by worker type
        result = await session.execute(
            select(
                Job.worker_type, Job.status, func.count()
            )
            .where(Job.subject_id == subject_id)
            .group_by(Job.worker_type, Job.status)
        )
        worker_breakdown = {}
        for wtype, status, count in result.all():
            worker_breakdown.setdefault(wtype, {})[status] = count

        # Fact counts by lifecycle
        result = await session.execute(
            select(Fact.lifecycle_state, func.count())
            .where(Fact.subject_id == subject_id)
            .group_by(Fact.lifecycle_state)
        )
        fact_counts = dict(result.all())

    print(f"Subject: {subject_id}")
    print(f"\nJobs ({total_jobs} total):")
    for status, count in sorted(job_counts.items()):
        print(f"  {status}: {count}")

    if worker_breakdown:
        print("\nJobs by worker:")
        for wtype in sorted(worker_breakdown):
            statuses = worker_breakdown[wtype]
            parts = ", ".join(
                f"{s}: {c}" for s, c in sorted(statuses.items())
            )
            print(f"  {wtype}: {parts}")

    total_facts = sum(fact_counts.values()) if fact_counts else 0
    print(f"\nFacts ({total_facts} total):")
    for state, count in sorted(fact_counts.items()):
        print(f"  {state}: {count}")


async def _cmd_report(args: argparse.Namespace) -> None:
    """Show full report of findings for a subject."""
    import uuid as uuid_mod

    from sqlalchemy import select

    from daisy.db import async_session_factory
    from daisy.models.fact import Fact
    from daisy.models.job import Job
    from daisy.models.source import Source
    from daisy.models.subject import Subject, SubjectAlias

    subject_id = uuid_mod.UUID(args.subject_id)
    show_snippets = args.show_snippets

    async with async_session_factory() as session:
        # Load subject
        result = await session.execute(
            select(Subject).where(Subject.id == subject_id)
        )
        subject = result.scalar_one_or_none()
        if not subject:
            print(f"Subject {subject_id} not found.")
            return

        # Load aliases
        result = await session.execute(
            select(SubjectAlias)
            .where(SubjectAlias.subject_id == subject_id)
            .order_by(
                SubjectAlias.alias_type,
                SubjectAlias.confidence.desc(),
            )
        )
        aliases = result.scalars().all()

        # Load facts
        result = await session.execute(
            select(Fact)
            .where(Fact.subject_id == subject_id)
            .order_by(
                Fact.local_confidence.desc(),
                Fact.created_at.desc(),
            )
        )
        facts = result.scalars().all()

        # Load sources referenced by facts
        source_urls = {f.source_url for f in facts}
        result = await session.execute(
            select(Source).where(Source.url.in_(source_urls))
        )
        sources = result.scalars().all()

        # Load jobs
        result = await session.execute(
            select(Job)
            .where(Job.subject_id == subject_id)
            .order_by(Job.created_at)
        )
        jobs = result.scalars().all()

    # --- Print report ---
    print("=" * 70)
    print(f"REPORT: {subject.name}")
    print(f"Subject ID: {subject.id}")
    print(
        f"Created: "
        f"{subject.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if subject.notes:
        print(f"Notes: {subject.notes}")
    print("=" * 70)

    # Aliases
    print(f"\nALIASES ({len(aliases)})")
    print("-" * 40)
    if aliases:
        by_type: dict[str, list] = {}
        for a in aliases:
            by_type.setdefault(a.alias_type, []).append(a)
        for atype, items in sorted(by_type.items()):
            print(f"  {atype}:")
            for a in items:
                conf = f" ({a.confidence:.0%})" if a.confidence else ""
                print(f"    {a.alias_value}{conf}")
    else:
        print("  (none)")

    # Facts grouped by type
    print(f"\nFACTS ({len(facts)})")
    print("-" * 40)
    if facts:
        by_type: dict[str, list] = {}
        for f in facts:
            by_type.setdefault(f.claim_type, []).append(f)
        for ctype in sorted(by_type):
            items = by_type[ctype]
            print(f"\n  [{ctype.upper()}] ({len(items)})")
            for f in items:
                conf = f"{f.local_confidence:.0%}"
                state = f.lifecycle_state
                date_str = ""
                if f.event_date:
                    date_str = f" ({f.event_date})"
                print(
                    f"    [{state}] [{conf}]"
                    f" {f.claim_text}{date_str}"
                )
                print(f"      source: {f.source_url}")
                if show_snippets and f.raw_snippet:
                    snippet = f.raw_snippet[:200]
                    if len(f.raw_snippet) > 200:
                        snippet += "..."
                    print(f"      snippet: {snippet}")
    else:
        print("  (none found)")

    # Sources
    print(f"\nSOURCES ({len(sources)})")
    print("-" * 40)
    if sources:
        for s in sources:
            status = f"HTTP {s.http_status}" if s.http_status else ""
            ctype = s.content_type or ""
            size = ""
            if s.raw_content:
                size = f"{len(s.raw_content):,} chars"
            print(f"  {s.url}")
            print(f"    {status} | {ctype} | {size}")
    else:
        print("  (none fetched)")

    # Job summary
    from collections import Counter

    status_counts = Counter(j.status for j in jobs)
    type_counts = Counter(j.worker_type for j in jobs)
    failed = [j for j in jobs if j.status == "failed"]

    print(f"\nJOBS ({len(jobs)})")
    print("-" * 40)
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")
    print("  by worker:")
    for wtype, count in sorted(type_counts.items()):
        print(f"    {wtype}: {count}")

    if failed:
        print(f"\n  FAILED JOBS ({len(failed)}):")
        for j in failed:
            err = (j.error or "unknown")[:100]
            print(f"    {j.worker_type} ({j.id}): {err}")

    print("\n" + "=" * 70)


def _cmd_worker(args: argparse.Namespace) -> None:
    """Start an RQ worker."""
    import platform

    from redis import Redis
    from rq import SimpleWorker, Worker

    from daisy.config import settings
    from daisy.constants import WORKER_TYPES, queue_name

    redis = Redis.from_url(settings.redis_url)

    if args.queues:
        queue_names = args.queues
    else:
        queue_names = [queue_name(wt) for wt in WORKER_TYPES]

    # macOS fork() crashes with Obj-C runtime + SSL libs.
    # Use SimpleWorker (no fork) on macOS.
    if platform.system() == "Darwin":
        worker_cls = SimpleWorker
    else:
        worker_cls = Worker

    print(
        "Starting worker listening on:"
        f" {', '.join(queue_names)}"
    )
    worker = worker_cls(queue_names, connection=redis)
    worker.work()


if __name__ == "__main__":
    main()
