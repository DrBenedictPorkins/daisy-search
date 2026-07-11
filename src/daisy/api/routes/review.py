"""Human review routes."""

from fastapi import APIRouter

router = APIRouter()

# TODO: Review endpoints
# - GET /queue — get next batch of facts needing review
# - POST /{fact_id}/decide — submit a human decision on a fact
