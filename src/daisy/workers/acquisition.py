"""Acquisition worker - fetches and stores raw content snapshots."""

import hashlib
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import WorkerType
from daisy.models.source import Source
from daisy.workers.base import BaseWorker


class AcquisitionWorker(BaseWorker):
    """Fetches web pages and stores raw content for extraction.

    Input:
        subject_id: str (UUID)
        url: str
        title: str (optional, from discovery)
        snippet: str (optional, from discovery)

    Output:
        source_id: str
        url: str
        content_type: str
        content_length: int
        content_hash: str
        http_status: int
        extraction_job_id: str
    """

    worker_type: str = WorkerType.ACQUISITION

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        from daisy.queue.dispatcher import enqueue_job

        input_data = await self._load_input(job_id, session_factory)
        subject_id_str: str = input_data["subject_id"]
        url: str = input_data["url"]

        # Fetch the page
        raw_content, http_status, content_type = await _fetch_url(url)

        content_hash = hashlib.sha256(raw_content.encode()).hexdigest()

        # Check for duplicate content
        from sqlalchemy import select

        async with session_factory() as session:
            existing = await session.execute(
                select(Source.id).where(Source.content_hash == content_hash)
            )
            if existing.scalar_one_or_none():
                return {
                    "source_id": None,
                    "url": url,
                    "skipped": True,
                    "reason": "duplicate_content",
                    "content_hash": content_hash,
                }

        # Store the source
        async with session_factory() as session:
            async with session.begin():
                source = Source(
                    url=url,
                    raw_content=raw_content,
                    content_hash=content_hash,
                    content_type=content_type,
                    http_status=http_status,
                )
                session.add(source)
                await session.flush()
                source_id = source.id

        # Enqueue extraction
        import uuid as uuid_mod

        subject_uuid = uuid_mod.UUID(subject_id_str)
        extraction_job_id = await enqueue_job(
            worker_type=WorkerType.EXTRACTION,
            input_data={
                "subject_id": subject_id_str,
                "source_id": str(source_id),
            },
            parent_job_id=job_id,
            subject_id=subject_uuid,
        )

        return {
            "source_id": str(source_id),
            "url": url,
            "content_type": content_type,
            "content_length": len(raw_content),
            "content_hash": content_hash,
            "http_status": http_status,
            "extraction_job_id": str(extraction_job_id),
        }


async def _fetch_url(url: str) -> tuple[str, int, str]:
    """Fetch a URL and return (raw_content, http_status, content_type).

    Respects a 30-second timeout and follows redirects.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DaisySearch/0.1; research bot)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, max_redirects=5
    ) as client:
        resp = await client.get(url, headers=headers)
        content_type = (
            resp.headers.get("content-type", "text/html")
            .split(";")[0]
            .strip()
        )
        return resp.text, resp.status_code, content_type
