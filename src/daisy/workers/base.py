"""Abstract base class for all pipeline workers."""

from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class BaseWorker(ABC):
    """Base class that all pipeline workers must extend."""

    worker_type: str

    @abstractmethod
    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        """Run the job and return an output_data dict.

        Args:
            job_id: The UUID of the job being executed.
            session_factory: Async session factory for DB access.

        Returns:
            A dict of output data to store on the job record.
        """
        ...

    async def _load_input(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        """Load input_data from the job record."""
        from sqlalchemy import select

        from daisy.models.job import Job

        async with session_factory() as session:
            result = await session.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one()
            return job.input_data
