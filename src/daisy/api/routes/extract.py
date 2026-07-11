"""Generic HTML extraction endpoint — receives raw HTML, returns clean markdown via trafilatura."""

import trafilatura
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class ExtractRequest(BaseModel):
    url: str
    html: str
    filter: str = "fit"


class ExtractResponse(BaseModel):
    markdown: str
    url: str
    success: bool


@router.post("/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest) -> ExtractResponse:
    markdown = trafilatura.extract(
        req.html,
        url=req.url,
        include_comments=False,
        output_format="markdown",
    )

    if not markdown:
        raise HTTPException(422, "No extractable content in provided HTML")

    return ExtractResponse(
        url=req.url, markdown=markdown, success=True
    )
