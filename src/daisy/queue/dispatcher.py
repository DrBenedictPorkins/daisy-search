"""Job dispatch via Redis Queue."""

import uuid as uuid_mod

from redis import Redis
from rq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daisy.config import settings
from daisy.constants import DEFAULT_JOB_STATUS, WorkerType, queue_name
from daisy.db import async_session_factory
from daisy.models.job import Job


def _get_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def _get_worker_registry() -> dict:
    """Lazy import to avoid circular deps."""
    from daisy.workers.acquisition import AcquisitionWorker
    from daisy.workers.discovery import DiscoveryWorker
    from daisy.workers.extraction import ExtractionWorker
    from daisy.workers.intake import IntakeWorker
    from daisy.workers.resolution import ResolutionWorker
    from daisy.workers.review import ReviewWorker
    from daisy.workers.scoring import ScoringWorker

    return {
        WorkerType.INTAKE: IntakeWorker,
        WorkerType.DISCOVERY: DiscoveryWorker,
        WorkerType.ACQUISITION: AcquisitionWorker,
        WorkerType.EXTRACTION: ExtractionWorker,
        WorkerType.RESOLUTION: ResolutionWorker,
        WorkerType.SCORING: ScoringWorker,
        WorkerType.REVIEW: ReviewWorker,
    }


def _make_session_factory():
    """Create a fresh async engine + session factory.

    Each asyncio.run() call gets its own event loop, so we need
    a fresh engine per invocation to avoid cross-loop errors.
    """
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _create_job_record(
    session: AsyncSession,
    worker_type: str,
    input_data: dict,
    parent_job_id: uuid_mod.UUID | None = None,
    subject_id: uuid_mod.UUID | None = None,
) -> uuid_mod.UUID:
    """Insert a Job row and return its id."""
    job = Job(
        worker_type=worker_type,
        input_data=input_data,
        parent_job_id=parent_job_id,
        subject_id=subject_id,
        status=DEFAULT_JOB_STATUS,
    )
    session.add(job)
    await session.flush()
    return job.id


async def enqueue_job(
    worker_type: str,
    input_data: dict,
    parent_job_id: uuid_mod.UUID | None = None,
    subject_id: uuid_mod.UUID | None = None,
) -> uuid_mod.UUID:
    """Create a Job record and enqueue it for processing via RQ.

    Returns the UUID of the newly created job.
    """
    async with async_session_factory() as session:
        async with session.begin():
            job_id = await _create_job_record(
                session,
                worker_type,
                input_data,
                parent_job_id,
                subject_id,
            )

    # Enqueue the RQ job
    redis = _get_redis()
    q = Queue(queue_name(worker_type), connection=redis)
    q.enqueue(_run_job, str(job_id), worker_type)

    return job_id


async def _run_job_async(
    job_id_str: str, worker_type: str
) -> None:
    """Async implementation of job execution."""
    import datetime

    from daisy.constants import JobStatus

    # Fresh engine for this event loop
    sf = _make_session_factory()

    job_id = uuid_mod.UUID(job_id_str)
    registry = _get_worker_registry()
    worker_cls = registry[worker_type]
    worker = worker_cls()

    async with sf() as session:
        async with session.begin():
            result = await session.execute(
                select(Job).where(Job.id == job_id)
            )
            job = result.scalar_one()
            job.status = JobStatus.RUNNING
            job.started_at = datetime.datetime.now(
                datetime.UTC
            )

    try:
        output = await worker.execute(job_id, sf)
        async with sf() as session:
            async with session.begin():
                result = await session.execute(
                    select(Job).where(Job.id == job_id)
                )
                job = result.scalar_one()
                job.status = JobStatus.COMPLETED
                job.output_data = output
                job.completed_at = datetime.datetime.now(
                    datetime.UTC
                )
    except Exception as exc:
        async with sf() as session:
            async with session.begin():
                result = await session.execute(
                    select(Job).where(Job.id == job_id)
                )
                job = result.scalar_one()
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.completed_at = datetime.datetime.now(
                    datetime.UTC
                )
        raise


def _run_job(job_id_str: str, worker_type: str) -> None:
    """Sync wrapper called by RQ."""
    import asyncio

    asyncio.run(_run_job_async(job_id_str, worker_type))
