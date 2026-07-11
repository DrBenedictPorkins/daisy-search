"""Scoring worker - calculates confidence scores and updates fact lifecycle."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import WorkerType
from daisy.workers.base import BaseWorker


class ScoringWorker(BaseWorker):
    """Calculates and updates confidence scores for facts."""

    worker_type: str = WorkerType.SCORING

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        # Phase 2 implementation
        raise NotImplementedError("ScoringWorker.execute not yet implemented")
