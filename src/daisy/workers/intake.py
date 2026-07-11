"""Intake worker - entry point for new research subjects."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import AliasType, ClaimType, LifecycleState, WorkerType
from daisy.models.fact import Fact
from daisy.models.subject import Subject, SubjectAlias
from daisy.workers.base import BaseWorker


class IntakeWorker(BaseWorker):
    """Expands a raw name into search queries and enqueues discovery jobs.

    Input:
        name: str (required)
        location: str (optional)
        occupation: str (optional)
        age_range: str (optional, e.g. "30-40")
        known_usernames: list[str] (optional)
        known_emails: list[str] (optional)
        notes: str (optional)

    Output:
        subject_id: str
        name_variants: list[str]
        username_guesses: list[str]
        search_queries: list[str]
        discovery_job_ids: list[str]
    """

    worker_type: str = WorkerType.INTAKE

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        from daisy.queue.dispatcher import enqueue_job

        # This is set by the runner before calling execute
        input_data = await self._load_input(job_id, session_factory)

        name: str = input_data["name"]
        locations: list[str] = input_data.get("locations", [])
        occupations: list[str] = input_data.get("occupations", [])
        known_usernames: list[str] = input_data.get("known_usernames", [])
        known_emails: list[str] = input_data.get("known_emails", [])
        known_facts: list[str] = input_data.get("known_facts", [])
        notes: str | None = input_data.get("notes")

        # --- Generate variants ---
        name_variants = _generate_name_variants(name)
        username_guesses = _generate_username_guesses(name, known_usernames)
        search_queries = _build_search_queries(
            name, name_variants, locations, occupations,
            known_facts,
        )

        # --- Persist subject + aliases ---
        async with session_factory() as session:
            async with session.begin():
                subject = Subject(name=name, notes=notes)
                session.add(subject)
                await session.flush()

                # Add name variants as aliases
                for variant in name_variants:
                    if variant.lower() != name.lower():
                        session.add(
                            SubjectAlias(
                                subject_id=subject.id,
                                alias_type=AliasType.NAME_VARIANT,
                                alias_value=variant,
                            )
                        )

                # Add username guesses
                for uname in username_guesses:
                    session.add(
                        SubjectAlias(
                            subject_id=subject.id,
                            alias_type=AliasType.USERNAME,
                            alias_value=uname,
                            confidence=0.3,
                        )
                    )

                # Add known usernames with higher confidence
                for uname in known_usernames:
                    if uname not in username_guesses:
                        session.add(
                            SubjectAlias(
                                subject_id=subject.id,
                                alias_type=AliasType.USERNAME,
                                alias_value=uname,
                                confidence=0.9,
                            )
                        )

                # Add known emails
                for email in known_emails:
                    session.add(
                        SubjectAlias(
                            subject_id=subject.id,
                            alias_type=AliasType.EMAIL,
                            alias_value=email,
                            confidence=0.9,
                        )
                    )

                # Seed known facts as confirmed
                for fact_text in known_facts:
                    session.add(
                        Fact(
                            subject_id=subject.id,
                            claim_text=fact_text,
                            claim_type=ClaimType.OTHER,
                            source_url="user_input",
                            source_type="user_input",
                            raw_snippet=fact_text,
                            local_confidence=1.0,
                            lifecycle_state=LifecycleState.CONFIRMED,
                        )
                    )

                subject_id = subject.id

        # --- Enqueue discovery jobs ---
        discovery_job_ids = []
        # Batch queries into groups to avoid too many jobs
        batch_size = 5
        for i in range(0, len(search_queries), batch_size):
            batch = search_queries[i : i + batch_size]
            child_id = await enqueue_job(
                worker_type=WorkerType.DISCOVERY,
                input_data={"subject_id": str(subject_id), "queries": batch},
                parent_job_id=job_id,
                subject_id=subject_id,
            )
            discovery_job_ids.append(str(child_id))

        return {
            "subject_id": str(subject_id),
            "name_variants": name_variants,
            "username_guesses": username_guesses,
            "search_queries": search_queries,
            "discovery_job_ids": discovery_job_ids,
        }

    async def _load_input(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        from sqlalchemy import select

        from daisy.models.job import Job

        async with session_factory() as session:
            result = await session.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one()
            return job.input_data


# ---------------------------------------------------------------------------
# Name variant generation (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _generate_name_variants(name: str) -> list[str]:
    """Generate plausible name variants from a full name."""
    parts = name.strip().split()
    variants: set[str] = {name}

    if len(parts) < 2:
        return list(variants)

    first = parts[0]
    last = parts[-1]
    middles = parts[1:-1]

    # Basic orderings
    variants.add(f"{first} {last}")
    variants.add(f"{last}, {first}")
    variants.add(f"{last} {first}")

    # With middle names/initials
    if middles:
        mid_str = " ".join(middles)
        mid_initials = " ".join(m[0] + "." for m in middles)
        variants.add(f"{first} {mid_str} {last}")
        variants.add(f"{first} {mid_initials} {last}")

    # First initial + last
    variants.add(f"{first[0]}. {last}")
    variants.add(f"{first[0]} {last}")

    # Common diminutives — keep it simple, just initials for now
    # (full nickname DB is a Phase 2 enhancement)

    return sorted(variants)


def _generate_username_guesses(
    name: str, known_usernames: list[str] | None = None
) -> list[str]:
    """Generate common username patterns from a name."""
    parts = name.strip().lower().split()
    if len(parts) < 2:
        return [parts[0]] if parts else []

    first = parts[0]
    last = parts[-1]
    fi = first[0]  # first initial

    guesses: set[str] = set()

    # Common patterns
    patterns = [
        f"{first}{last}",
        f"{first}.{last}",
        f"{first}_{last}",
        f"{first}-{last}",
        f"{fi}{last}",
        f"{first}{last[0]}",
        f"{last}{first}",
        f"{last}.{first}",
        f"{last}_{first}",
        f"{first}",
        f"{last}",
    ]
    guesses.update(patterns)

    # Add year suffixes for common patterns (recent decades)
    base_patterns = [f"{first}{last}", f"{fi}{last}", f"{first}.{last}"]
    for base in base_patterns:
        for suffix in ["1", "2", "01", "99", "00"]:
            guesses.add(f"{base}{suffix}")

    # Don't duplicate known usernames
    if known_usernames:
        guesses -= set(known_usernames)

    return sorted(guesses)


def _build_search_queries(
    name: str,
    name_variants: list[str],
    locations: list[str] | None = None,
    occupations: list[str] | None = None,
    known_facts: list[str] | None = None,
) -> list[str]:
    """Build search engine queries from name + context hints."""
    queries: list[str] = []

    # Exact name queries
    queries.append(f'"{name}"')

    # Name + each location
    for location in locations or []:
        queries.append(f'"{name}" {location}')
        for variant in name_variants[:3]:
            if variant != name:
                queries.append(f'"{variant}" {location}')

    # Name + each occupation (cross with each location)
    for occupation in occupations or []:
        queries.append(f'"{name}" {occupation}')
        for location in locations or []:
            queries.append(
                f'"{name}" {occupation} {location}'
            )

    # Name + known facts (extract key terms)
    for fact in known_facts or []:
        queries.append(f'"{name}" {fact}')

    # Social media specific queries
    social_platforms = [
        "linkedin", "twitter", "facebook",
        "instagram", "github",
    ]
    for platform in social_platforms:
        queries.append(f'"{name}" site:{platform}.com')

    # Public records style queries
    queries.append(f'"{name}" resume OR CV')
    queries.append(f'"{name}" biography OR bio OR about')

    # Top name variants without duplicating the primary name
    for variant in name_variants[:3]:
        if variant != name:
            queries.append(f'"{variant}"')

    return queries
