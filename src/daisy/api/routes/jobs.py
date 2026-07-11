"""Job monitoring routes."""

from fastapi import APIRouter

router = APIRouter()

# TODO: Job monitoring endpoints
# - GET / — list jobs with filtering (by status, worker_type, subject_id)
# - GET /{job_id} — get job details
# - GET /{job_id}/children — get child jobs
# - POST /{job_id}/cancel — cancel a pending/running job
