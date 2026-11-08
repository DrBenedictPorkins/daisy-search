"""Centralized constants for Daisy Search.

All magic strings and default values live here. Workers, models, and config
reference these instead of inline literals. Where a value should be
overridable at runtime, the Settings class in config.py exposes an env var
that defaults to the constant defined here.
"""

from enum import StrEnum

# ---------------------------------------------------------------------------
# Project status
# ---------------------------------------------------------------------------


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


PROJECT_STATUSES = tuple(s.value for s in ProjectStatus)
DEFAULT_PROJECT_STATUS = ProjectStatus.ACTIVE


# ---------------------------------------------------------------------------
# Fact lifecycle
# ---------------------------------------------------------------------------

class LifecycleState(StrEnum):
    UNCONFIRMED = "unconfirmed"
    LIKELY = "likely"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"
    NEEDS_REVALIDATION = "needs_revalidation"


LIFECYCLE_STATES = tuple(s.value for s in LifecycleState)
DEFAULT_LIFECYCLE_STATE = LifecycleState.UNCONFIRMED


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------

class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_STATUSES = tuple(s.value for s in JobStatus)
DEFAULT_JOB_STATUS = JobStatus.PENDING


# ---------------------------------------------------------------------------
# Worker types
# ---------------------------------------------------------------------------

class WorkerType(StrEnum):
    INTAKE = "intake"
    DISCOVERY = "discovery"
    ACQUISITION = "acquisition"
    EXTRACTION = "extraction"
    RESOLUTION = "resolution"
    SCORING = "scoring"
    REVIEW = "review"


WORKER_TYPES = tuple(w.value for w in WorkerType)


# ---------------------------------------------------------------------------
# Claim types (extensible — new values don't require a migration)
# ---------------------------------------------------------------------------

class ClaimType(StrEnum):
    EMPLOYMENT = "employment"
    EDUCATION = "education"
    LOCATION = "location"
    ACCOUNT = "account"
    RELATIONSHIP = "relationship"
    PUBLICATION = "publication"
    LEGAL = "legal"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Source types
# ---------------------------------------------------------------------------

class SourceType(StrEnum):
    SEARCH_RESULT = "search_result"
    SOCIAL_PROFILE = "social_profile"
    NEWS = "news"
    PUBLIC_RECORD = "public_record"
    FORUM = "forum"
    BLOG = "blog"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Alias types
# ---------------------------------------------------------------------------

class AliasType(StrEnum):
    NAME_VARIANT = "name_variant"
    USERNAME = "username"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"


# ---------------------------------------------------------------------------
# Queue names
# ---------------------------------------------------------------------------

QUEUE_PREFIX = "daisy"


def queue_name(worker_type: str) -> str:
    """Return the RQ queue name for a worker type."""
    return f"{QUEUE_PREFIX}-{worker_type}"


# ---------------------------------------------------------------------------
# Default confidence / scoring values
# ---------------------------------------------------------------------------

DEFAULT_ALIAS_CONFIDENCE = 0.5
DEFAULT_FACT_CONFIDENCE = 0.0
DEFAULT_RESOLUTION_SCORE = 0.0


# ---------------------------------------------------------------------------
# LLM model identifiers — overridable via env vars in Settings
# ---------------------------------------------------------------------------

DEFAULT_EXTRACTION_MODEL = "claude-sonnet-5-5"
DEFAULT_RESOLUTION_MODEL = "claude-haiku-5-5"
DEFAULT_TRIAGE_MODEL = "claude-haiku-5-5"

# Thinking is always on for these models; effort controls its depth and cost.
DEFAULT_LLM_EFFORT = "low"
# Thinking counts toward max_tokens, so leave room beyond the reply itself.
DEFAULT_LLM_MAX_TOKENS = 16000

# Ollama defaults
DEFAULT_OLLAMA_EXTRACTION_MODEL = "mistral:7b"

# Neo4j defaults
DEFAULT_NEO4J_URL = "bolt://localhost:7687"
DEFAULT_NEO4J_AUTH_USER = "neo4j"
DEFAULT_NEO4J_AUTH_PASS = "daisy_dev"
