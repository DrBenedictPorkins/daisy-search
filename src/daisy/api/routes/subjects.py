"""Subject management routes."""

from fastapi import APIRouter

router = APIRouter()

# TODO: CRUD endpoints for subjects
# - POST / — create a new subject (triggers intake worker)
# - GET / — list subjects
# - GET /{subject_id} — get subject details with aliases
# - PATCH /{subject_id} — update subject notes
