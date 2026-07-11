"""Review worker - prepares facts for human validation."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import WorkerType
from daisy.workers.base import BaseWorker


class ReviewWorker(BaseWorker):
    """Prepares ambiguous or low-confidence facts for human review."""

    worker_type: str = WorkerType.REVIEW

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        # Phase 2 implementation
        raise NotImplementedError("ReviewWorker.execute not yet implemented")
