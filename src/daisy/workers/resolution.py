"""Resolution worker - clusters identity matches and handles name collisions."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import WorkerType
from daisy.workers.base import BaseWorker


class ResolutionWorker(BaseWorker):
    """Resolves whether facts belong to the target subject or a namesake."""

    worker_type: str = WorkerType.RESOLUTION

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        # Phase 2 implementation
        raise NotImplementedError("ResolutionWorker.execute not yet implemented")
