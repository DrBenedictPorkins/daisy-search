"""Extraction worker - extracts structured claims from content."""

import datetime
import json
import uuid as uuid_mod
from uuid import UUID

import httpx
import trafilatura
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daisy.constants import (
    DEFAULT_FACT_CONFIDENCE,
    DEFAULT_LLM_EFFORT,
    DEFAULT_LLM_MAX_TOKENS,
    WorkerType,
)
from daisy.models.fact import Fact
from daisy.models.source import Source
from daisy.workers.base import BaseWorker

EXTRACTION_PROMPT = """\
You are an OSINT fact extraction assistant. Given text content from a web page, \
extract all claims that could relate to a person named "{name}".

For each claim, return a JSON object with:
- "claim_text": a concise statement of the claim
- "claim_type": one of: employment, education, location, account, relationship, \
publication, legal, other
- "event_date": date if mentioned (YYYY-MM-DD format), or null
- "confidence": your confidence this claim relates to the named person (0.0-1.0)
- "raw_snippet": the exact text passage supporting this claim (max 500 chars)

Return a JSON array of claim objects. If no relevant claims found, return [].
Only return the JSON array, no other text.
"""


class ExtractionWorker(BaseWorker):
    """Extracts structured fact claims from raw content using LLM.

    Input:
        subject_id: str (UUID)
        source_id: str (UUID)

    Output:
        fact_ids: list[str]
        claim_count: int
    """

    worker_type: str = WorkerType.EXTRACTION

    async def execute(
        self, job_id: UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        from daisy.config import settings

        input_data = await self._load_input(job_id, session_factory)
        subject_id_str: str = input_data["subject_id"]
        source_id_str: str = input_data["source_id"]
        source_id = uuid_mod.UUID(source_id_str)
        subject_id = uuid_mod.UUID(subject_id_str)

        # Load source content
        async with session_factory() as session:
            result = await session.execute(
                select(Source).where(Source.id == source_id)
            )
            source = result.scalar_one()

        # Load subject name for context
        from daisy.models.subject import Subject

        async with session_factory() as session:
            result = await session.execute(
                select(Subject.name).where(Subject.id == subject_id)
            )
            subject_name = result.scalar_one()

        # Clean HTML to readable text
        clean_text = _extract_text(source.raw_content, source.url)
        if not clean_text or len(clean_text.strip()) < 50:
            return {
                "fact_ids": [],
                "claim_count": 0,
                "skipped": True,
                "reason": "no_content",
            }

        # Truncate to avoid huge LLM calls
        max_chars = 15000
        if len(clean_text) > max_chars:
            clean_text = clean_text[:max_chars] + "\n[... truncated]"

        # Extract claims via LLM
        claims = await _extract_claims_llm(
            clean_text, subject_name, settings.extraction_model
        )

        # Persist facts
        fact_ids = []
        async with session_factory() as session:
            async with session.begin():
                for claim in claims:
                    fact = Fact(
                        subject_id=subject_id,
                        claim_text=claim["claim_text"],
                        claim_type=claim.get("claim_type", "other"),
                        source_url=source.url,
                        source_type=_infer_source_type(source.url),
                        raw_snippet=claim.get("raw_snippet", "")[:500],
                        event_date=_parse_date(claim.get("event_date")),
                        local_confidence=claim.get(
                            "confidence", DEFAULT_FACT_CONFIDENCE
                        ),
                    )
                    session.add(fact)
                    await session.flush()
                    fact_ids.append(str(fact.id))

        return {"fact_ids": fact_ids, "claim_count": len(fact_ids)}


def _extract_text(raw_html: str, url: str) -> str | None:
    """Convert raw HTML to clean readable text."""
    return trafilatura.extract(raw_html, url=url, include_comments=False)


def _infer_source_type(url: str) -> str:
    """Guess source type from URL domain."""
    from daisy.constants import SourceType

    domain = url.lower()
    if any(s in domain for s in ["linkedin.com"]):
        return SourceType.SOCIAL_PROFILE
    social_domains = [
        "twitter.com", "x.com", "facebook.com", "instagram.com",
    ]
    if any(s in domain for s in social_domains):
        return SourceType.SOCIAL_PROFILE
    code_domains = [
        "github.com", "gitlab.com", "bitbucket.org",
    ]
    if any(s in domain for s in code_domains):
        return SourceType.SOCIAL_PROFILE
    forum_domains = [
        "reddit.com", "news.ycombinator.com", "forum",
    ]
    if any(s in domain for s in forum_domains):
        return SourceType.FORUM
    blog_domains = [
        "medium.com", "substack.com", "wordpress.com", "blogspot",
    ]
    if any(s in domain for s in blog_domains):
        return SourceType.BLOG
    news_domains = [
        "reuters.com", "apnews.com", "bbc.com",
        "cnn.com", "nytimes.com",
    ]
    if any(s in domain for s in news_domains):
        return SourceType.NEWS
    return SourceType.OTHER


def _parse_date(date_str: str | None) -> datetime.date | None:
    """Parse a YYYY-MM-DD date string, returning None on failure."""
    if not date_str:
        return None
    try:
        return datetime.date.fromisoformat(date_str)
    except ValueError:
        return None


async def _extract_claims_llm(
    text: str, subject_name: str, model: str
) -> list[dict]:
    """Call Claude API to extract structured claims from text.

    Reads ANTHROPIC_API_KEY from settings (.env).
    """
    from daisy.config import settings

    api_key = settings.anthropic_api_key
    if not api_key:
        return []

    prompt = EXTRACTION_PROMPT.format(name=subject_name)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": DEFAULT_LLM_MAX_TOKENS,
                "output_config": {"effort": DEFAULT_LLM_EFFORT},
                "messages": [
                    {
                        "role": "user",
                        "content": f"{prompt}\n\n---\n\n{text}",
                    }
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("stop_reason") == "refusal":
        return []
    # Parse the response; it can open with a thinking block, so read by type.
    response_text = next(
        (b["text"] for b in data["content"] if b.get("type") == "text"),
        "",
    )
    try:
        claims = json.loads(response_text)
        if not isinstance(claims, list):
            return []
        return claims
    except json.JSONDecodeError:
        # Try to find JSON array in the response
        start = response_text.find("[")
        end = response_text.rfind("]")
        if start != -1 and end != -1:
            try:
                return json.loads(response_text[start : end + 1])
            except json.JSONDecodeError:
                return []
        return []
