"""Discovery worker - runs exploratory searches and returns candidate URLs."""

from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import WorkerType
from daisy.workers.base import BaseWorker


class DiscoveryWorker(BaseWorker):
    """Executes search queries and collects candidate URLs for acquisition.

    Input:
        subject_id: str (UUID)
        queries: list[str]

    Output:
        candidates: list[dict] — each with url, title, snippet, query
        acquisition_job_ids: list[str]
    """

    worker_type: str = WorkerType.DISCOVERY

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        from daisy.config import settings
        from daisy.queue.dispatcher import enqueue_job

        input_data = await self._load_input(job_id, session_factory)
        subject_id_str: str = input_data["subject_id"]
        queries: list[str] = input_data["queries"]

        candidates: list[dict] = []
        seen_urls: set[str] = set()

        for query in queries:
            results = await _search(query, settings)
            for r in results:
                url = r["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    candidates.append({**r, "query": query})

        # Enqueue acquisition for each candidate URL
        acquisition_job_ids = []
        import uuid as uuid_mod

        subject_uuid = uuid_mod.UUID(subject_id_str)
        for candidate in candidates:
            child_id = await enqueue_job(
                worker_type=WorkerType.ACQUISITION,
                input_data={
                    "subject_id": subject_id_str,
                    "url": candidate["url"],
                    "title": candidate.get("title", ""),
                    "snippet": candidate.get("snippet", ""),
                },
                parent_job_id=job_id,
                subject_id=subject_uuid,
            )
            acquisition_job_ids.append(str(child_id))

        return {
            "candidates": candidates,
            "candidate_count": len(candidates),
            "acquisition_job_ids": acquisition_job_ids,
        }


async def _search(query: str, settings: object) -> list[dict]:
    """Execute a search query and return results.

    Uses Brave Search API if BRAVE_SEARCH_API_KEY is set,
    otherwise falls back to a SearXNG instance if SEARXNG_URL is set.
    """
    brave_key = getattr(settings, "brave_search_api_key", None)
    searxng_url = getattr(settings, "searxng_url", None)

    if brave_key:
        return await _search_brave(query, brave_key)
    elif searxng_url:
        return await _search_searxng(query, searxng_url)
    else:
        # No search backend configured — return empty
        return []


async def _search_brave(query: str, api_key: str) -> list[dict]:
    """Search via Brave Search API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 10},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", []):
        results.append({
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("description", ""),
        })
    return results


async def _search_searxng(query: str, searxng_url: str) -> list[dict]:
    """Search via a self-hosted SearXNG instance."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{searxng_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "pageno": 1},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "url": item["url"],
            "title": item.get("title", ""),
            "snippet": item.get("content", ""),
        })
    return results
