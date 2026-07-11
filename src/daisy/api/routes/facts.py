"""Fact query routes."""

from fastapi import APIRouter

router = APIRouter()

# TODO: Fact query endpoints
# - GET / — list facts with filtering (by subject, lifecycle_state, claim_type)
# - GET /{fact_id} — get fact details with events
# - GET /{fact_id}/events — get fact event history
